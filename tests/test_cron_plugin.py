"""The wiring between `datasette-cron` and the scheduled tasks.

Two things this holds, and neither is about what a task does — that is
`test_scheduled.py`. The first is that a task is sent to the right thread: a
writing one through `execute_write_fn`, so Datasette's writer serialises it,
and a read-only one through `execute_fn`, so a nightly verify never queues
behind an upload. The second is that a long task is run **a chunk at a time**,
because the alternative is one connection held for minutes on a server that
answers pages from three of them.

Datasette's `Database` is stood in for — it is a queue in front of a
connection, and counting the calls is exactly what is under test here. The
store, the schema and every core function are the real ones.

The four tasks have also been run against a live Datasette with the real
`datasette-cron` scheduler: registered from config, triggered, recorded green,
and recorded red with the audit headline as the message when an original was
tampered with. That is not something a test can leave behind, so it is recorded
in `docs/scheduled-tasks.md`.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib

import pytest

import orpheus.bundle as bundle_mod
from orpheus import ingest as ingest_mod, scheduled
from orpheus.utils import OrpheusError, naive_key

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "orpheus_cron_under_test", ROOT / "plugins" / "orpheus_cron.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeDatabase:
    """Datasette's Database, minus the write thread.

    `execute_write_fn` opens its own transaction before calling, which is why
    the plugin adopts the connection with `owns_transaction=False`; that is
    reproduced here so the plugin is exercised under the nesting it really
    meets. Both counters are the point of the fake.
    """

    def __init__(self, store):
        self.store = store
        self.path = store.path
        self.reads = 0
        self.writes = 0

    async def execute_fn(self, fn):
        self.reads += 1
        return fn(self.store.conn)

    async def execute_write_fn(self, fn):
        self.writes += 1
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self.store.conn)
        except BaseException:
            self.store.conn.rollback()
            raise
        self.store.conn.commit()
        return result


class FakeDatasette:
    def __init__(self, database, **config):
        self.database = database
        self._config = config

    def plugin_config(self, name):
        return self._config.get(name)

    def get_database(self, name=None):
        return self.database


@pytest.fixture
def corpus(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    for n, text in enumerate([
        "This Agreement is between Halloran Instruments, Inc. and Kestrel "
        "Medical Group PLC.",
        "Amendment No. 2 with Halloran Instruments Inc. extends the term.",
        "Kestrel Medical Group PLC licenses from Ardmore Digital Ltd.",
    ], start=1):
        path = tmp_path / f"doc{n}.txt"
        path.write_text(text)
        ingest_mod.ingest(store, path, storage_root=tmp_path / "storage")
    return store


@pytest.fixture
def datasette(corpus):
    return FakeDatasette(FakeDatabase(corpus),
                         **{plugin.ORPHEUS_PLUGIN: {"database": "orpheus"}})


def run(datasette, name, config=None):
    return asyncio.run(plugin.run_task(datasette, name, config or {}))


# -- which thread ----------------------------------------------------------

def test_a_read_only_task_never_touches_the_write_thread(datasette):
    run(datasette, "verify")
    assert datasette.database.writes == 0
    assert datasette.database.reads > 0


def test_a_writing_task_goes_through_the_writer(datasette):
    run(datasette, "wiki-propose")
    assert datasette.database.writes > 0
    assert datasette.database.reads == 0


def test_the_thread_is_chosen_from_the_task_and_not_from_a_list_here():
    # A task added later cannot be wired to the wrong thread by somebody
    # forgetting to add it somewhere.
    assert {name: task.writes for name, task in scheduled.TASKS.items()} == {
        "verify": False, "search-index": True,
        "wiki-propose": True, "calendar-digest": False}


# -- one chunk at a time ---------------------------------------------------

def test_a_long_task_lets_go_of_the_connection_between_chunks(datasette):
    """Three documents, one per batch: one call to plan the work and one per
    document. The count is the test — a pass that held a single connection for
    the whole corpus would be one call however large the corpus got."""
    run(datasette, "verify", {"batch": 1})
    assert datasette.database.reads == 1 + 3

    datasette.database.reads = 0
    run(datasette, "verify", {"batch": 3})
    assert datasette.database.reads == 1 + 1


def test_a_batched_run_through_the_plugin_agrees_with_a_single_pass(datasette):
    batched = run(datasette, "verify", {"batch": 1})
    whole = ingest_mod.audit_storage(datasette.database.store, verify=True)
    assert batched["n_documents"] == whole["n_documents"] == 3
    assert batched["headline"] == whole["headline"]


# -- what comes back out ---------------------------------------------------

def test_a_finding_reaches_the_run_record_rather_than_the_log_alone(datasette):
    """`TaskFailed` is raised through the handler on purpose: `datasette-cron`
    records a raised run as failed with the message, which is how an operator
    sees the headline at all."""
    store = datasette.database.store
    path = store.one("SELECT storage_path FROM documents "
                     "ORDER BY date_added")["storage_path"]
    with open(path, "w") as handle:
        handle.write("not the bytes that were ingested")

    handler = plugin._make_handler("verify")
    with pytest.raises(scheduled.TaskFailed, match="altered"):
        asyncio.run(handler(datasette, {}))


def test_a_scheduled_proposal_is_attributed_to_the_machine_actor(datasette):
    store = datasette.database.store
    document_id = store.one("SELECT document_id FROM documents "
                            "ORDER BY date_added")["document_id"]
    name = "Halloran Instruments, Inc."
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, "
        "naive_key, source, confidence, status, created_at) "
        "VALUES ('i1',?,?,?,'ai_local',0.9,'unconfirmed',datetime('now'))",
        (document_id, name, naive_key(name)))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, "
        "document_id, created_at) VALUES ('i1','Company','instances_Company',?,"
        "datetime('now'))", (document_id,))

    assert run(datasette, "wiki-propose")["proposed"] == 1
    idp = store.scalar("SELECT idp FROM actors a JOIN entities e "
                       "ON e.created_by = a.actor_id")
    assert idp == scheduled.SCHEDULED_IDP


# -- reading the config ----------------------------------------------------

def test_a_bare_string_is_a_cron_expression():
    schedule, config, overlap = plugin._read_spec("verify", "0 3 * * *")
    assert schedule == "0 3 * * *"
    assert config == {}
    # skip, not cancel: a verify still hashing when the next slot comes round
    # should finish rather than be killed halfway and recorded as an error.
    assert overlap == "skip"


def test_default_takes_the_task_s_own_recommendation():
    for name, task in scheduled.TASKS.items():
        assert plugin._read_spec(name, "default")[0] == task.default_schedule
        assert plugin._read_spec(name, {})[0] == task.default_schedule


def test_a_block_carries_config_through():
    schedule, config, overlap = plugin._read_spec(
        "calendar-digest",
        {"schedule": {"interval": 3600}, "config": {"within_days": 30},
         "overlap": "cancel"})
    assert schedule == {"interval": 3600}
    assert config == {"within_days": 30}
    assert overlap == "cancel"


def test_an_unknown_task_in_config_is_refused_by_name():
    with pytest.raises(OrpheusError, match="No scheduled task named 'backup'"):
        plugin._read_spec("backup", "0 3 * * *")


def test_a_schedule_that_is_neither_a_string_nor_a_block_says_so():
    with pytest.raises(OrpheusError, match="not int"):
        plugin._read_spec("verify", {"schedule": 3600})


def test_the_handler_ref_is_the_one_datasette_cron_will_look_up():
    """The ref is stored as text in a task row, so it has to match what
    `datasette-cron` derives from this module's name. A rename that changed it
    would disable every task a deployment had already configured."""
    datasette_cron = pytest.importorskip("datasette_cron")
    module = "orpheus_cron"
    derived = module.replace("datasette_", "").split(".")[0]
    assert plugin.handler_ref("verify") == f"{derived}:verify"
    assert hasattr(datasette_cron, "cron_register_handlers")


def test_only_the_tasks_this_install_can_run_are_offered(monkeypatch, datasette):
    pytest.importorskip("datasette_cron")
    monkeypatch.setitem(scheduled._EXTRA_AVAILABLE, "search", lambda: False)
    handlers = plugin.cron_register_handlers(datasette)
    assert "search-index" not in handlers
    assert set(handlers) == {"verify", "wiki-propose", "calendar-digest"}
