"""Reading a batch of pages with the companion, as a `datasette-enrichments` job.

The companion reads one passage at a time, which is right for somebody working
through a document and wrong for "read the other thirty pages". Doing that from
the CLI works and tells you nothing while it runs: no progress, no way to stop
it once it is clearly going wrong, and a failure on page 12 that takes the rest
with it.

`datasette-enrichments` is that missing half, already built — a user picks rows,
fills in a form, and gets a job with per-row progress, an error table, cancel
and pause. This is Orpheus's enrichment over `document_pages`.

**The hook, never the write.** `enrich_batch` is handed a `Database` and the
plugin's own examples write with `db.execute_write`, which is
`execute_write_sql` by another name and fails for
[the same reasons](../docs/datasette-ecosystem.md#why-execute_write_sql-must-never-touch-the-store):
no provenance, no rubric snapping, no review vocabulary, no `edit_history`, no
cloud audit. The difference is that Orpheus writes this class, so what goes
inside the batch loop is `companion.read_passage()` — the same function the CLI
and the API call, on the same write connection Datasette serialises.

**The gate is checked once, before anything is sent.** A cloud tier needs an
explicit opt-in, and it is on the form rather than in configuration, because
"this batch may leave the building" is a decision somebody makes each time. The
gate answers the same for every row, so it is asked in `initialize` — asking per
page would send the first page to find out and then refuse the rest.

**A whole batch failing one way stops the job.** The runner declares
`default_max_errors` and never reads it, so a job that cannot work logs one
error per row and finishes reporting success. A single page failing is a page; a
batch failing identically is the job, and that is judged on the shape of the
failures rather than on words in the message, because reading intent out of an
error string is guesswork.

**Cost stays in characters.** The plugin tracks money in `cost_100ths_cent`,
and Orpheus denominates its budget in characters
[on purpose](../docs/network-and-corroboration.md#what-a-budget-is-denominated-in):
a price list goes stale and a character count does not. `llm_calls.prompt_chars`
already records every call this makes, so that column is left alone rather than
filled with a number that would quietly rot.
"""

from __future__ import annotations

from datasette import hookimpl

try:  # optional: `pip install 'orpheus[enrichments]'`
    from datasette_enrichments import Enrichment
except ImportError:  # pragma: no cover - exercised by not installing the extra
    Enrichment = None

try:
    from wtforms import BooleanField, Form, IntegerField, SelectField
    from wtforms.validators import NumberRange
except ImportError:  # pragma: no cover
    Form = None

from orpheus import companion as companion_mod
from orpheus.store import Store
from orpheus.utils import OrpheusError

PLUGIN = "orpheus-datasette"

#: The one table this makes sense over. An enrichment offered on every table in
#: the database and refusing on all but one is a worse offer than none.
TABLE = "document_pages"


def _database(datasette):
    """The Orpheus database, or None if this Datasette is not serving one.

    `get_database` raises `KeyError` for a name it does not have, and every
    caller here is written to treat None as "not configured" -- so a
    misconfigured name produced a traceback where a message was intended.
    """
    config = datasette.plugin_config(PLUGIN) or {}
    name = config.get("database")
    if not name:
        return None
    try:
        return datasette.get_database(name)
    except KeyError:
        return None


def _engines() -> list[tuple[str, str]]:
    """What a person can pick, cheapest and safest first.

    The pattern pass leads because it cannot offer something the page does not
    contain, needs no opt-in and sends nothing anywhere -- and on a batch of
    forty pages that difference is forty calls.
    """
    from orpheus.engines import engine_names

    out = [(companion_mod.DEFAULT_ENGINE,
            "patterns only -- instant, local, sends nothing")]
    out += [(name, f"{name} -- a model") for name in engine_names()
            if name != "chat"]
    return out


if Enrichment is not None and Form is not None:

    class ReadPassages(Enrichment):
        name = "Read these pages with the companion"
        slug = "orpheus-read-passages"
        description = (
            "Offer what each selected page seems to contain. Nothing is "
            "recorded as fact: every offer is a suggestion somebody still has "
            "to accept, which is what keeps a batch like this out of the "
            "extraction-quality number.")

        # A page is one model call. Small batches so progress moves, a job can
        # be stopped part-way, and a run that is going wrong is visible early
        # rather than after the last page.
        batch_size = 5
        default_max_errors = 5

        async def get_config_form(self, datasette, db, table):
            class ConfigForm(Form):
                engine = SelectField(
                    "Read with",
                    choices=_engines(),
                    default=companion_mod.DEFAULT_ENGINE,
                    description=("The pattern pass cannot offer something the "
                                 "page does not contain. A model can, and goes "
                                 "through the same gate as any other call."))
                cloud_opt_in = BooleanField(
                    "Send these pages to a cloud model",
                    default=False,
                    description=("Required for a cloud tier, and asked every "
                                 "run rather than configured once: this is the "
                                 "decision about whether these documents leave "
                                 "the building."))
                context_chars = IntegerField(
                    "Characters of surrounding document to send with each page",
                    default=0,
                    validators=[NumberRange(min=0, max=200_000)],
                    description=("0 keeps each page in isolation, which is the "
                                 "default everywhere. Context is charged to "
                                 "the same budget as the page and, measured on "
                                 "a real filing, did not improve the read."))

            return ConfigForm

        async def initialize(self, datasette, db, table, config):
            """Refuse early and clearly, rather than once per row.

            Everything decidable before the first page is decided here. The
            runner declares `default_max_errors` and never reads it, so a job
            that cannot possibly work otherwise logs one error per row and
            finishes reporting success.
            """
            expected = _database(datasette)
            if expected is None:
                raise self.Cancel(
                    "No Orpheus database is configured for this Datasette, or "
                    "the name in `orpheus-datasette` does not match one it is "
                    "serving. This enrichment writes through Orpheus and has "
                    "nowhere to write.")
            if db.name != expected.name:
                raise self.Cancel(
                    f"{db.name!r} is not the Orpheus database "
                    f"({expected.name!r}). This enrichment writes through "
                    "Orpheus, and writing these rows any other way is the one "
                    "thing it exists to avoid.")
            if table != TABLE:
                raise self.Cancel(
                    f"This reads pages, so it runs over {TABLE!r}. On any "
                    "other table there is nothing for it to read.")

            # The gate, once, before anything is sent anywhere. It answers the
            # same for every row, so asking per page would send the first page
            # to find out and then refuse the rest.
            engine = config.get("engine") or companion_mod.DEFAULT_ENGINE
            if engine != companion_mod.DEFAULT_ENGINE and config.get("cloud_opt_in"):
                def check(conn):
                    from orpheus import llm as llm_mod

                    store = Store.adopt(conn, path=db.path,
                                        owns_transaction=False)
                    llm_mod.assert_cloud_allowed(store, opt_in=True)

                try:
                    await db.execute_write_fn(check)
                except OrpheusError as refused:
                    raise self.Cancel(str(refused))

        async def enrich_batch(self, datasette, db, table, rows, pks, config,
                               job_id, actor_id=None):
            engine = config.get("engine") or companion_mod.DEFAULT_ENGINE
            cloud = bool(config.get("cloud_opt_in"))
            context_chars = int(config.get("context_chars") or 0)
            tier = "cloud" if (cloud and engine != companion_mod.DEFAULT_ENGINE) \
                else "local"

            # Per row, not per batch. Letting one page's failure raise would
            # lose the other four in its batch: the runner logs an exception
            # against every row it was given, so a single unreadable page
            # would be recorded as five failures and four pages nobody read.
            done = 0
            failures: list[str] = []
            for row in rows:
                document_id = row.get("document_id")
                page_no = row.get("page_no")
                if not document_id or page_no is None:
                    message = "A row with no document_id or page_no is not a page."
                    failures.append(message)
                    await self.log_error(
                        db, job_id, [[document_id, page_no]], message)
                    continue

                def run(conn, document_id=document_id, page_no=page_no):
                    # Adopted, not opened. The store's writer lock refuses a
                    # second Orpheus writer and cannot refuse a plugin opening
                    # its own connection -- so this uses the one Datasette
                    # already serialises, and every write goes through the core.
                    store = Store.adopt(conn, path=db.path,
                                        owns_transaction=False)
                    store.assert_current()
                    return companion_mod.read_passage(
                        store, document_id, int(page_no), actor_id=actor_id,
                        engine=engine, tier=tier, opt_in=cloud,
                        context_chars=context_chars)

                try:
                    await db.execute_write_fn(run)
                    done += 1
                except Exception as failed:  # noqa: BLE001 - logged, not hidden
                    failures.append(str(failed))
                    await self.log_error(db, job_id,
                                         [[document_id, page_no]], str(failed))

            # A page that fails is a page. A whole batch failing the same way
            # is the job -- a store behind its migrations, a model nobody
            # installed -- and carrying on writes that sentence once per page
            # for the rest of the corpus.
            #
            # Judged on the shape of the failures rather than on words in them:
            # reading intent out of an error message is guesswork, and this is
            # not.
            # More than one, because a pattern cannot be read off a single
            # observation: one bad row is a bad row, and the last batch of a
            # job is often one row long.
            if len(rows) > 1 and len(failures) == len(rows) \
                    and len(set(failures)) == 1:
                raise self.Cancel(
                    f"Every page in this batch failed the same way, so the "
                    f"rest will too: {failures[0]}")
            return done

    @hookimpl
    def register_enrichments():
        return [ReadPassages()]

else:  # pragma: no cover - the extra is not installed

    @hookimpl
    def register_enrichments():
        """No extra, no enrichment. The plugin's own pages keep working."""
        return []
