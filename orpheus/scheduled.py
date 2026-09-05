"""Work the deployment does on a clock, and the actor it is done as.

**Why this is not a crontab**, in two parts, and the plain one first: *the
shipped deployment has no cron daemon.* `deploy/Dockerfile` builds one process
on `python:3.11-slim` -- `datasette serve` -- with no cron installed and nothing
to install it into. A scheduled task there has nowhere to run at all.

The second part is what happens where a crontab does exist. `orpheus wiki
propose` opens the store as a **second writing process**, and -- checked, not
assumed -- nothing refuses it: the advisory lock is taken by `Store(mode=
"write")`, and Datasette never takes it, because it borrows a connection it
already opened. Which makes an unsupervised second writer worse rather than
better. A `mode="write"` open *applies migrations*, so a command run from cron
after an upgrade can move the schema under a server that has already loaded it
-- `assert_current()` exists because that mismatch is real. And its writes
contend with the server's at the SQLite level, with a five-second `busy_timeout`
standing between the corpus and `database is locked`.

What the lock does refuse is a second *CLI* writer, which is the shape a
crontab and a maintenance script overlapping actually takes:

    REFUSED: The single-writer lock on orpheus.sqlite is held by pid 2497.
    Only one process may write to the store -- send writes through the
    running Datasette instead of opening a second writer.

The error says the remedy, and this module is it. Anything scheduled belongs
*inside* the process that owns the database, which is what `datasette-cron`
provides -- and why the tasks below are core functions a plugin calls on
Datasette's own write thread rather than commands a shell runs.

Three things this module is careful about, in the order they matter.

**Nobody is present.** Every write carries an actor, and a schedule has no
person behind it. Borrowing the administrator who configured the schedule would
put their name on months of rows they never saw. So there is a machine actor,
filed under an idp that says what it is, and `auth.create_token` refuses to mint
it a credential -- it is a label on work, not an account.

**Nobody is present, part two: nothing may leave the building.** The cloud gate
asks two questions -- has the organisation enabled cloud processing, and did
this person opt in -- and the second cannot be answered honestly by a clock.
So every scheduled run is wrapped in `llm.no_cloud()`, which refuses any cloud
call inside it whatever the policy says. That is enforcement rather than a
convention: a task added later that reaches for a model is refused by the gate,
not by a reviewer noticing.

**A long job must not hold the connection the server answers pages on.** A
corpus verify is minutes of disk reads. Tasks are therefore expressed in
chunks: `chunks()` is one cheap read that says what the work is, `run_chunk()`
does one unit of it, and `combine()` adds the units up without touching the
store. A caller inside Datasette awaits one chunk at a time and lets go of the
connection between them; a caller at a terminal runs them in a loop and cannot
tell the difference. `run()` is that loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import auth
from . import entities as entities_mod
from . import ingest as ingest_mod
from . import llm
from . import obligations as obligations_mod
from . import search as search_mod
from .store import Store
from .utils import OrpheusError

SCHEDULED_IDP = "orpheus-scheduled"
SCHEDULED_EXTERNAL_ID = "cron"
SCHEDULED_DISPLAY_NAME = "Scheduled task"

CLOUD_REFUSAL = (
    "A scheduled run may not send anything to a cloud model. The opt-in the "
    "gate asks for is a person deciding to send this document to a third "
    "party, taken each time -- and a clock cannot take it. Run the pass from "
    "the surface or the command line, where somebody is present to opt in."
)

# How many documents one verify chunk covers. Small enough that a chunk is a
# short hold on a read connection, large enough that the thread hop between
# chunks is not the cost of the pass.
VERIFY_BATCH = 25


class TaskFailed(OrpheusError):
    """The task ran, and what it found is something an operator must see.

    Distinct from a crash on purpose, and reported the same way on purpose:
    `orpheus verify` and `orpheus calendar` both exit non-zero when they have
    something to say, precisely so a schedule can be silent when they do not.
    A scheduled run keeps that behaviour -- the run is recorded failed and the
    headline is the message -- because the alternative is a green row nobody
    opens and a finding nobody reads.

    `result` carries the full answer, so a caller that wants the detail behind
    the headline does not have to run the task again to get it.
    """

    def __init__(self, message: str, result: dict | None = None):
        super().__init__(message)
        self.result = result or {}


@dataclass(frozen=True)
class Task:
    """One scheduled job, in the three pieces a chunked runner needs.

    `writes` is not decoration: it is what a deployment reads to decide whether
    this task needs Datasette's write thread, and what makes the answer to
    "which of these could change my store" a fact about the code rather than a
    claim in a document.
    """

    name: str
    summary: str
    writes: bool
    # The cadence this task is *for*, in the form a scheduler takes: a cron
    # expression, or `{"interval": seconds}`. A recommendation and nothing
    # more -- no deployment gets a schedule it did not ask for -- but it is
    # written here rather than in a document because the reason a verify wants
    # three in the morning is a property of the verify.
    default_schedule: str | dict
    # Extras the task needs installed. Reported rather than assumed, so a
    # deployment without them is told which install is missing instead of
    # meeting an ImportError at three in the morning.
    needs: tuple[str, ...] = ()
    chunks: Callable[[Store, dict], list] = lambda store, config: [None]
    run_chunk: Callable[..., dict] = field(default=lambda **_: {})
    combine: Callable[[list[dict], dict], dict] = field(
        default=lambda results, config: results[0] if results else {})

    def available(self) -> bool:
        return all(_EXTRA_AVAILABLE[extra]() for extra in self.needs)


# ---------------------------------------------------------------------------
# The machine actor
# ---------------------------------------------------------------------------

def scheduled_actor(store: Store) -> str:
    """The actor id a scheduled write is attributed to, created if it is new.

    Keyed like any other external identity, on `(idp, external_id)`, so it is
    one row for the life of the store and the wiki pages a schedule proposed
    over six months all point at it.

    Never an administrator. The tasks here call core functions directly -- the
    same way the CLI does -- so the flag buys them nothing, and a machine actor
    that carried it would be an administrator nobody logs in as and nobody
    watches. `create_token` refuses it a credential for the same reason.
    """
    store.assert_writable()
    return auth.upsert_actor(store, SCHEDULED_IDP, SCHEDULED_EXTERNAL_ID,
                             SCHEDULED_DISPLAY_NAME, is_admin=False)


def is_scheduled(actor: dict | None) -> bool:
    """Whether this actor row is the scheduler rather than a person.

    The audit trail's answer to "who confirmed this". Read from the row's idp
    rather than from a flag somewhere else, so it stays true for rows written
    before anything thought to ask.
    """
    return bool(actor) and actor.get("idp") == SCHEDULED_IDP


# ---------------------------------------------------------------------------
# The tasks
# ---------------------------------------------------------------------------

def _verify_chunks(store: Store, config: dict) -> list[list[str]]:
    ids = ingest_mod.audit_document_ids(store)
    size = max(1, int(config.get("batch") or VERIFY_BATCH))
    return [ids[i:i + size] for i in range(0, len(ids), size)] or [[]]


def _verify_chunk(store: Store, config: dict, chunk: list[str]) -> dict:
    return {"documents": ingest_mod.audit_entries(store, chunk, verify=True)}


def _verify_combine(results: list[dict], config: dict) -> dict:
    documents = [entry for result in results for entry in result["documents"]]
    audit = ingest_mod.summarise_audit(documents, verify=True)
    if audit["n_unavailable"]:
        raise TaskFailed(audit["headline"], audit)
    return audit


def _flag(config: dict, key: str) -> bool:
    """A boolean out of config, which reaches here from two different places.

    A YAML block gives a real `True`; `--config rebuild=false` gives the string
    `"false"`, which is truthy. Reading it with `bool()` would make the one
    spelling a person is most likely to type mean the opposite of what it says.
    """
    value = config.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _search_chunk(store: Store, config: dict, chunk) -> dict:
    built = search_mod.enable_search(store, rebuild=_flag(config, "rebuild"))
    states = sorted(set(built.values())) or ["nothing to index"]
    return {"indexes": built,
            "headline": f"Full-text index: {', '.join(states)}."}


def _propose_chunk(store: Store, config: dict, chunk) -> dict:
    result = entities_mod.propose_entities(
        store, type_id=config.get("type_id") or None,
        actor_id=scheduled_actor(store))
    result.setdefault("attached", 0)
    result["headline"] = _propose_headline(store, result)
    return result


def _propose_headline(store: Store, result: dict) -> str:
    """What a quiet run means, which is two different things.

    "Nothing to propose" is the answer when every mention is already on a page.
    It is also what an empty corpus says, and an empty corpus is the case worth
    telling apart -- a schedule reporting nothing to do every night on a store
    nothing has been extracted from is reporting that the extraction never ran,
    and that reads as reassurance unless it says so.
    """
    if result["proposed"] or result["linked"] or result["attached"]:
        return (f"{result['proposed']} page(s) proposed, {result['attached']} "
                f"mention group(s) attached to pages that already existed, "
                f"{result['linked']} mention(s) linked. Every one is "
                f"unconfirmed and waiting for a person.")
    extracted = store.scalar("SELECT COUNT(*) FROM instance_index") or 0
    if not extracted:
        return ("Nothing to propose, because nothing has been extracted from "
                "this corpus. A wiki is built out of mentions, and there are "
                "none.")
    return (f"Nothing to propose: all {extracted} extracted instance(s) are "
            f"already on a page.")


def _calendar_chunk(store: Store, config: dict, chunk) -> dict:
    return obligations_mod.upcoming(
        store, within_days=int(config.get("within_days")
                               or obligations_mod.DEFAULT_WINDOW),
        limit=int(config.get("limit") or 200))


def _calendar_combine(results: list[dict], config: dict) -> dict:
    result = results[0]
    if result["n_overdue"]:
        raise TaskFailed(result["headline"], result)
    return result


TASKS: dict[str, Task] = {
    "verify": Task(
        name="verify",
        summary=("Re-read every original and check it against the digest "
                 "recorded at ingest. Fails when one cannot be served, which "
                 "is the point: a database and a storage/ from two different "
                 "moments look healthy from the inside."),
        writes=False,
        # Nightly, and at an hour when holding a read connection for minutes
        # costs nobody anything.
        default_schedule="0 3 * * *",
        chunks=_verify_chunks,
        run_chunk=_verify_chunk,
        combine=_verify_combine,
    ),
    "search-index": Task(
        name="search-index",
        summary=("Make sure the full-text index exists. A no-op once it does "
                 "-- the FTS triggers keep it in step with the tables by "
                 "themselves, measured rather than assumed -- so this is a "
                 "reconcile, not a rebuild: it exists so no deployment is "
                 "left with a chat that cannot search because nobody ran "
                 "`orpheus search --build`. Set `rebuild: true` to make it "
                 "one."),
        writes=True,
        default_schedule="10 3 * * *",
        needs=("search",),
        run_chunk=_search_chunk,
    ),
    "wiki-propose": Task(
        name="wiki-propose",
        summary=("Group unlinked mentions and propose an entity page each, so "
                 "the queue is populated as documents land rather than after "
                 "somebody remembers. Everything it makes is unconfirmed and "
                 "linked on the weakest basis there is; it decides nothing."),
        writes=True,
        default_schedule="0 4 * * *",
        run_chunk=_propose_chunk,
    ),
    "calendar-digest": Task(
        name="calendar-digest",
        summary=("What falls due, and how much of the corpus can speak to "
                 "that. Fails when something is past its date, so a weekly "
                 "run is silent unless there is something to say."),
        writes=False,
        default_schedule="0 8 * * 1",
        run_chunk=_calendar_chunk,
        combine=_calendar_combine,
    ),
}

_EXTRA_AVAILABLE: dict[str, Callable[[], bool]] = {
    "search": search_mod.available,
}


def get(name: str) -> Task:
    task = TASKS.get(name)
    if task is None:
        known = ", ".join(sorted(TASKS))
        raise OrpheusError(f"No scheduled task named {name!r}. Known: {known}.")
    return task


def catalogue() -> list[dict]:
    """Every task, what it does, and whether this install can run it."""
    return [{"name": task.name,
             "summary": task.summary,
             "writes": task.writes,
             "needs": list(task.needs),
             "available": task.available(),
             "default_schedule": task.default_schedule}
            for task in TASKS.values()]


# ---------------------------------------------------------------------------
# Running one
# ---------------------------------------------------------------------------

def _guard(task: Task) -> None:
    if not task.available():
        missing = ", ".join(f"orpheus[{extra}]" for extra in task.needs
                            if not _EXTRA_AVAILABLE[extra]())
        raise OrpheusError(
            f"The {task.name!r} task needs {missing}, which is not installed.")


def plan(store: Store, name: str, config: dict | None = None) -> list:
    """The chunks this run will cover. One cheap read; takes no lock."""
    task = get(name)
    _guard(task)
    return list(task.chunks(store, config or {}))


def run_chunk(store: Store, name: str, chunk: Any,
              config: dict | None = None) -> dict:
    """One unit of a task's work, refused a cloud model for its whole duration.

    The refusal is installed here rather than around the caller because this is
    the function that actually runs on the store's thread, and a ContextVar set
    on the event loop does not cross `run_in_executor`. Wrapping the unit of
    work is the only placement that holds for both callers.
    """
    task = get(name)
    _guard(task)
    with llm.no_cloud(CLOUD_REFUSAL):
        return task.run_chunk(store=store, config=config or {}, chunk=chunk)


def combine(name: str, results: list[dict], config: dict | None = None) -> dict:
    """Add the chunk results up. Pure -- no store, no clock, no model."""
    task = get(name)
    return task.combine(results, config or {})


def run(store: Store, name: str, config: dict | None = None) -> dict:
    """The whole task, chunk by chunk, on one store.

    What the CLI and the tests use. A caller that has to yield between chunks
    -- the Datasette plugin -- drives `plan`, `run_chunk` and `combine` itself
    and gets the same answer, which is what makes the terminal and the
    scheduler agree about what a corpus looks like.
    """
    config = config or {}
    results = [run_chunk(store, name, chunk, config)
               for chunk in plan(store, name, config)]
    return combine(name, results, config)
