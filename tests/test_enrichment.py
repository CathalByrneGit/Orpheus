"""Reading a batch of pages as a `datasette-enrichments` job.

What is defended here is the seam, not the reading — `test_companion.py` already
holds what a passage read produces. The job of this file is the three ways a
batch run can go wrong that a single read cannot:

**It must write through the core.** The plugin hands `enrich_batch` a database
and its own examples write with `db.execute_write`, which is
`execute_write_sql` by another name. This one calls `companion.read_passage`.

**It must refuse early.** Everything decidable before the first page — the
database, the table, the cloud gate — is decided in `initialize`, because the
alternative is sending the first page to find out.

**It must stop when it cannot work.** The runner declares `default_max_errors`
and never reads it, so a hopeless job otherwise logs one error per row and
finishes reporting success.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

import pytest

# Imported first, deliberately. `datasette_enrichments` pulls in
# `datasette_secrets`, which loads every Datasette entrypoint through pluggy --
# and one of those imports `datasette_secrets` again, so a direct import from a
# cold interpreter dies on a circular import. A Datasette process has already
# done this by the time it loads a plugin, and so has this file.
import datasette.plugins  # noqa: F401

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load(name="orpheus_enrichments_under_test"):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "plugins" / "orpheus_enrichments.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load()
pytestmark = pytest.mark.skipif(
    plugin.Enrichment is None,
    reason="datasette-enrichments is not installed")


class FakeDatabase:
    """Just enough of Datasette's `Database` for the seam under test.

    `refuse` maps a call number to what that call should raise, so a test can
    say "the second page fails" without a real store behind it.
    """

    def __init__(self, name="store", path="/tmp/store.sqlite", refuse=None):
        self.name = name
        self.path = path
        self.refuse = refuse or {}
        self.calls = []

    async def execute_write_fn(self, fn):
        self.calls.append(fn)
        failure = self.refuse.get(len(self.calls))
        if failure:
            raise failure
        return {"n_offered": 0}


Writes = FakeDatabase


class FakeDatasette:
    def __init__(self, database=None, configured="store"):
        self._database = database
        self._configured = configured

    def plugin_config(self, name):
        return {"database": self._configured} if self._configured else {}

    def get_database(self, name):
        if self._database is None or name != self._database.name:
            raise KeyError(name)
        return self._database


@pytest.fixture
def enrichment():
    found = plugin.register_enrichments()
    assert len(found) == 1
    return found[0]


def run(coro):
    return asyncio.run(coro)


# -- what it offers ----------------------------------------------------------

def test_it_registers_one_enrichment_over_pages(enrichment):
    assert enrichment.slug == "orpheus-read-passages"
    assert plugin.TABLE == "document_pages"
    # The description is what a person reads before starting a batch, so it
    # says the thing that stops this being mistaken for extraction.
    assert "suggestion" in enrichment.description


def test_the_pattern_pass_is_offered_first(enrichment):
    # On forty pages the difference between the pattern pass and a model is
    # forty calls, so the cheap and local one leads.
    first, label = plugin._engines()[0]
    assert first == "deterministic"
    assert "sends nothing" in label


def test_the_form_asks_for_the_opt_in_every_run(enrichment):
    form = run(enrichment.get_config_form(FakeDatasette(), FakeDatabase(),
                                          "document_pages"))
    fields = {f.name for f in form()}
    assert fields == {"engine", "cloud_opt_in", "context_chars"}
    # Asked rather than configured, because "these documents may leave the
    # building" is a decision somebody makes each time.
    assert form().cloud_opt_in.data is False


# -- refusing early ----------------------------------------------------------

def _initialize(enrichment, datasette, db, table="document_pages", **config):
    return run(enrichment.initialize(datasette, db, table, config))


def test_no_orpheus_database_is_a_refusal_not_a_traceback(enrichment):
    # `get_database` raises KeyError for a name it does not have, and this is
    # written to treat "not configured" as a message.
    with pytest.raises(enrichment.Cancel) as stopped:
        _initialize(enrichment, FakeDatasette(database=None), FakeDatabase())
    assert "nowhere to write" in str(stopped.value)


def test_a_name_that_matches_nothing_served_is_also_a_refusal(enrichment):
    served = FakeDatabase(name="something_else")
    with pytest.raises(enrichment.Cancel):
        _initialize(enrichment, FakeDatasette(database=served,
                                              configured="store"),
                    served)


def test_it_refuses_to_run_against_another_database(enrichment):
    orpheus = FakeDatabase(name="store")
    other = FakeDatabase(name="scratch")
    with pytest.raises(enrichment.Cancel) as stopped:
        _initialize(enrichment, FakeDatasette(database=orpheus), other)
    assert "not the Orpheus database" in str(stopped.value)


def test_it_refuses_a_table_that_is_not_pages(enrichment):
    orpheus = FakeDatabase(name="store")
    with pytest.raises(enrichment.Cancel) as stopped:
        _initialize(enrichment, FakeDatasette(database=orpheus), orpheus,
                    table="entities")
    assert "document_pages" in str(stopped.value)


def test_the_pattern_pass_needs_no_gate_check(enrichment):
    # It cannot offer something the page does not contain and sends nothing
    # anywhere, so there is nothing to ask permission for.
    orpheus = Writes(name="store")
    _initialize(enrichment, FakeDatasette(database=orpheus), orpheus,
                engine="deterministic", cloud_opt_in=True)
    assert orpheus.calls == []


def test_the_cloud_gate_is_asked_once_before_any_page(enrichment):
    from orpheus.utils import OrpheusError

    orpheus = Writes(name="store",
                     refuse={1: OrpheusError("Cloud extraction is not "
                                             "permitted by the org policy.")})
    with pytest.raises(enrichment.Cancel) as stopped:
        _initialize(enrichment, FakeDatasette(database=orpheus), orpheus,
                    engine="llm", cloud_opt_in=True)
    assert "org policy" in str(stopped.value)
    # Once. Asking per page would send the first page to find out.
    assert len(orpheus.calls) == 1


# -- the batch ---------------------------------------------------------------

def _batch(enrichment, db, rows, **config):
    return run(enrichment.enrich_batch(
        datasette=FakeDatasette(database=db), db=db, table="document_pages",
        rows=rows, pks=["document_id", "page_no"], config=config, job_id=1,
        actor_id="act_a"))


PAGES = [{"document_id": "doc_1", "page_no": n} for n in (1, 2, 3)]


def test_every_page_is_read_through_the_core(enrichment):
    db = Writes()
    assert _batch(enrichment, db, PAGES, engine="deterministic") == 3
    assert len(db.calls) == 3


def test_one_bad_page_does_not_take_its_batch_with_it(enrichment, monkeypatch):
    """The runner logs an exception against every row it was given, so letting
    one page raise would record five failures and four pages nobody read."""
    logged = []

    async def log_error(self, db, job_id, ids, error):
        logged.append((ids, error))

    monkeypatch.setattr(type(enrichment), "log_error", log_error)
    db = Writes(refuse={2: ValueError("that page is not text")})

    assert _batch(enrichment, db, PAGES, engine="deterministic") == 2
    assert len(logged) == 1
    assert logged[0][0] == [["doc_1", 2]]
    assert "not text" in logged[0][1]


def test_a_row_that_is_not_a_page_is_logged_rather_than_raised(
        enrichment, monkeypatch):
    logged = []

    async def log_error(self, db, job_id, ids, error):
        logged.append(error)

    monkeypatch.setattr(type(enrichment), "log_error", log_error)
    db = Writes()
    done = _batch(enrichment, db,
                  [{"document_id": None, "page_no": None}] + PAGES[:1],
                  engine="deterministic")
    assert done == 1 and "is not a page" in logged[0]


def test_one_row_failing_is_never_the_whole_job(enrichment, monkeypatch):
    # A pattern cannot be read off a single observation, and the last batch of
    # a job is often one row long.
    async def log_error(self, db, job_id, ids, error):
        pass

    monkeypatch.setattr(type(enrichment), "log_error", log_error)
    db = Writes(refuse={1: ValueError("odd page")})
    assert _batch(enrichment, db, PAGES[:1], engine="deterministic") == 0


def test_a_batch_failing_one_way_stops_the_job(enrichment, monkeypatch):
    """Judged on the shape of the failures, not on words in them.

    A store behind its migrations answers the same for every page in the
    corpus, and the runner's own error budget is declared and never read.
    """
    from orpheus.utils import OrpheusError

    async def log_error(self, db, job_id, ids, error):
        pass

    monkeypatch.setattr(type(enrichment), "log_error", log_error)
    behind = OrpheusError("This store is behind: migration(s) [11] have not "
                          "been applied.")
    db = Writes(refuse={1: behind, 2: behind, 3: behind})

    with pytest.raises(enrichment.Cancel) as stopped:
        _batch(enrichment, db, PAGES, engine="deterministic")
    assert "the rest will too" in str(stopped.value)
    assert "migration" in str(stopped.value)


def test_pages_failing_differently_are_pages_and_not_the_job(
        enrichment, monkeypatch):
    # Three unrelated failures is three bad pages. Stopping there would give up
    # on a corpus because three of its pages are odd.
    async def log_error(self, db, job_id, ids, error):
        pass

    monkeypatch.setattr(type(enrichment), "log_error", log_error)
    db = Writes(refuse={1: ValueError("a"), 2: ValueError("b"),
                        3: ValueError("c")})
    assert _batch(enrichment, db, PAGES, engine="deterministic") == 0


def test_a_model_without_the_opt_in_runs_at_the_local_tier(enrichment):
    # It still runs -- the gate is about what leaves the building, and nothing
    # does. What it must not do is quietly upgrade itself to the cloud tier.
    db = Writes(name="store")
    _initialize(enrichment, FakeDatasette(database=db), db,
                engine="llm", cloud_opt_in=False)
    assert db.calls == [], "no gate check, because there is nothing to gate"
    assert _batch(enrichment, db, PAGES[:1], engine="llm",
                  cloud_opt_in=False) == 1


# -- without the extra -------------------------------------------------------

def test_the_plugin_registers_nothing_when_the_extra_is_absent(monkeypatch):
    """The Orpheus pages have to keep working without it, which is the normal
    install: `datasette-enrichments` is an opt-in extra."""
    monkeypatch.setitem(sys.modules, "datasette_enrichments", None)
    absent = _load("orpheus_enrichments_without_the_extra")
    assert absent.Enrichment is None
    assert absent.register_enrichments() == []
