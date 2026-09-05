"""Orpheus tasks on a schedule, through `datasette-cron`.

**Why a plugin and not a crontab.** The shipped container runs one process and
has no cron daemon, so a scheduled task has nowhere to run at all; and where a
crontab does exist, `orpheus wiki propose` opens the store as an unsupervised
second writing process that applies migrations under a live server and contends
with it for the write lock. `orpheus/scheduled.py` has the full account.
`datasette-cron` runs in-process and hands its handlers the `Datasette`
instance, which means a task goes through `execute_write_fn` — the same write
thread every other Orpheus write already goes through.

**No SQL is written here**, the same invariant `orpheus_datasette.py` keeps.
Every task is a core function in `orpheus.scheduled`, which is where the
machine actor, the cloud refusal and the chunking live; this file is the wiring
that turns each one into something a clock can call.

**A long task is run in chunks.** A corpus verify is minutes of disk reads, and
Datasette answers pages from a small pool of read connections. So each chunk is
its own `execute_fn`, awaited in turn, and the connection is let go between
them. A nightly verify is then a sequence of short holds rather than one long
one — which is the difference between a background job and an outage at 3am.

**The scheduler starts on the first HTTP request**, not at startup — that is
`datasette-cron`'s design, not something this plugin can change. A deployment
that receives no traffic overnight runs no tasks. If nothing else pings it,
point a health check at `/-/orpheus/api/health`; `docs/deployment.md` says so.

Optional, like the agent plugin: without `datasette-cron` installed nothing here
registers and the rest of Orpheus is unaffected.
"""

from __future__ import annotations

import logging

from datasette import hookimpl

try:  # optional: `pip install 'orpheus[cron]'`
    import datasette_cron  # noqa: F401
    HAVE_CRON = True
except ImportError:  # pragma: no cover - exercised by not installing the extra
    HAVE_CRON = False

from orpheus import scheduled
from orpheus.store import Store
from orpheus.utils import OrpheusError

PLUGIN = "orpheus-cron"
# The plugin config the Datasette-facing plugin already uses. Read rather than
# duplicated, so a deployment names its database and storage root once.
ORPHEUS_PLUGIN = "orpheus-datasette"
DEFAULT_DATABASE = "orpheus"

# `datasette-cron` derives the handler prefix from the module name with a
# leading `datasette_` stripped, so handlers registered from this module are
# addressed as `orpheus_cron:<task>`. Written out rather than inferred: a task
# row stores the ref as text, and a rename that silently changed it would
# disable every task a deployment had configured.
HANDLER_PREFIX = "orpheus_cron"

logger = logging.getLogger("orpheus.cron")


def _database(datasette):
    config = datasette.plugin_config(ORPHEUS_PLUGIN) or {}
    name = config.get("database", DEFAULT_DATABASE)
    try:
        return datasette.get_database(name)
    except KeyError:
        return datasette.get_database()


def handler_ref(name: str) -> str:
    return f"{HANDLER_PREFIX}:{name}"


async def run_task(datasette, name: str, config: dict | None = None) -> dict:
    """One scheduled task, a chunk at a time, on the right thread for it.

    A writing task goes through `execute_write_fn` so Datasette's write thread
    serialises it against every other write; a read-only one goes through
    `execute_fn` so it never queues behind an upload. Which is which comes from
    the task's own `writes` flag rather than from a list kept here, so a task
    added later cannot be wired to the wrong thread by omission.
    """
    config = config or {}
    task = scheduled.get(name)
    database = _database(datasette)
    owns = not task.writes

    def on_store(fn):
        def inner(conn):
            store = Store.adopt(conn, path=database.path, owns_transaction=owns)
            store.assert_current()
            return fn(store)
        return inner

    async def call(fn):
        wrapped = on_store(fn)
        if task.writes:
            return await database.execute_write_fn(wrapped)
        return await database.execute_fn(wrapped)

    chunks = await call(lambda store: scheduled.plan(store, name, config))
    results = []
    for chunk in chunks:
        # One await per chunk, and the connection is released at each one. This
        # loop is the whole reason `scheduled` is expressed in chunks at all.
        results.append(await call(
            lambda store, chunk=chunk: scheduled.run_chunk(
                store, name, chunk, config)))
    return scheduled.combine(name, results, config)


def _make_handler(name: str):
    async def handler(datasette, config):
        try:
            result = await run_task(datasette, name, config or {})
        except scheduled.TaskFailed as failed:
            # Recorded as a failed run on purpose. `orpheus verify` and
            # `orpheus calendar` both exit non-zero when they have something to
            # say, so a schedule is silent when they do not; a red row in the
            # cron UI with the headline on it is the same contract. It is not a
            # crash, and the message is written so an operator can tell.
            logger.warning("orpheus %s: %s", name, failed)
            raise
        logger.info("orpheus %s: %s", name,
                    result.get("headline", "done"))
        return result
    handler.__name__ = f"orpheus_{name.replace('-', '_')}"
    handler.__doc__ = scheduled.get(name).summary
    return handler


if HAVE_CRON:
    @hookimpl
    def cron_register_handlers(datasette):
        """Offer every task this install can actually run.

        A task whose extra is missing is left out rather than registered to
        fail: `datasette-cron` disables a task whose handler cannot be found and
        says so in the log, which is a better answer than a run that raises
        ImportError every night.
        """
        handlers = {}
        for entry in scheduled.catalogue():
            if not entry["available"]:
                logger.info("orpheus cron: %s not offered, needs %s",
                            entry["name"], ", ".join(entry["needs"]))
                continue
            handlers[entry["name"]] = _make_handler(entry["name"])
        return handlers

    @hookimpl
    def startup(datasette):
        """Create the tasks this deployment asked for, and only those.

        Schedules are declared in config, not invented here. Creating four
        tasks on every startup because the code knows four exist would be a
        deployment deciding for itself that it verifies its corpus nightly —
        and `add_task` is an upsert, so it would also overwrite a schedule
        somebody had changed.

            plugins:
              orpheus-cron:
                tasks:
                  verify: "0 3 * * *"
                  calendar-digest:
                    schedule: "0 8 * * 1"
                    config: {within_days: 30}

        A bare string is a cron expression, `{interval: seconds}` and
        `{rrule: ...}` are the other two `datasette-cron` accepts, and
        `default` takes the task's own recommended schedule. Config is the source of truth for the tasks it
        names — a schedule edited in the cron UI is reset on the next restart —
        so a deployment that would rather manage them there should leave this
        empty and add them from `/-/cron`, where the handlers are registered
        either way. `enabled` is never written by an upsert, so a task paused
        in the UI stays paused.
        """
        async def inner():
            declared = (datasette.plugin_config(PLUGIN) or {}).get("tasks") or {}
            if not declared:
                return
            scheduler = getattr(datasette, "_cron_scheduler", None)
            if scheduler is None:  # pragma: no cover - cron startup ordering
                logger.error("orpheus cron: datasette-cron did not start; "
                             "no tasks were registered.")
                return
            for name, spec in declared.items():
                try:
                    schedule, config, overlap = _read_spec(name, spec)
                    await scheduler.add_task(
                        name=f"orpheus-{name}", handler=handler_ref(name),
                        schedule=schedule, config=config, overlap=overlap)
                except Exception as exc:
                    # One bad entry is one task that does not exist, not a
                    # startup that fails: the other three should still run, and
                    # an unparseable cron expression should say so by name.
                    logger.error("orpheus cron: %s not scheduled: %s", name, exc)
                    continue
                logger.info("orpheus cron: %s scheduled (%s)", name, schedule)

        return inner


def _read_spec(name: str, spec) -> tuple[dict, dict, str]:
    """Turn one config entry into what `add_task` wants, or say what is wrong.

    Validated here rather than at fire time because a typo in a cron expression
    is a task that silently never runs, and the log line at startup is the only
    moment anybody is looking.
    """
    task = scheduled.get(name)          # raises on an unknown task name
    if isinstance(spec, str):
        spec = {"schedule": spec}
    if not isinstance(spec, dict):
        raise OrpheusError(
            f"{name}: a schedule is a cron expression or a block, not "
            f"{type(spec).__name__}.")

    schedule = spec.get("schedule", "default")
    if schedule == "default" or schedule is None:
        schedule = task.default_schedule
    elif not isinstance(schedule, (str, dict)):
        raise OrpheusError(f"{name}: schedule must be a cron expression or a "
                           f"block, not {type(schedule).__name__}.")

    # `skip` rather than `cancel`: a verify that is still hashing when the next
    # slot comes round should finish, not be killed halfway and recorded as an
    # error nobody caused.
    return schedule, dict(spec.get("config") or {}), spec.get("overlap", "skip")
