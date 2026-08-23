import os
import sqlite3

import pytest

from orpheus.store import Store, connect, writer_lock_path
from orpheus.utils import OrpheusError


def test_a_new_store_is_migrated_and_in_wal_mode(store):
    versions = [r["version"] for r in store.query("SELECT version FROM schema_migrations")]
    assert versions == [1, 2, 3]
    assert store.scalar("PRAGMA journal_mode") == "wal"


def test_the_writer_lock_refuses_a_second_writer(db_path):
    first = connect(db_path)
    try:
        # Same process: a plugin opening its own Store beside the application's
        # is the realistic version of this mistake now that Datasette is the
        # writer, so it is named as such rather than reported as contention.
        with pytest.raises(OrpheusError, match="already holds the writer lock"):
            connect(db_path)
    finally:
        first.close()
    # and releases it on close, so the next writer gets in
    connect(db_path).close()


def test_a_lock_held_by_another_live_process_is_refused(db_path):
    connect(db_path).close()
    # pid 1 always exists and is never us.
    writer_lock_path(db_path).write_text('{"pid": 1, "acquired_at": "x"}')
    with pytest.raises(OrpheusError, match="single-writer lock"):
        connect(db_path)
    writer_lock_path(db_path).unlink()


def test_a_stale_lock_needs_force_and_then_yields(db_path):
    connect(db_path).close()
    # A lock left by a process that no longer exists. PID 1 is always alive, so
    # a plausible-but-dead pid is used instead.
    writer_lock_path(db_path).write_text('{"pid": 999999, "acquired_at": "x"}')
    with pytest.raises(OrpheusError, match="stale writer lock"):
        connect(db_path)
    s = connect(db_path, force_lock=True)
    s.close()


def test_a_read_connection_refuses_writes_before_sqlite_sees_them(db_path):
    connect(db_path).close()
    reader = connect(db_path, mode="read")
    try:
        with pytest.raises(OrpheusError, match="read-only"):
            reader.insert("org_settings", {"key": "k", "value": "v", "updated_at": "t"})
    finally:
        reader.close()


def test_a_read_connection_does_not_take_the_lock(db_path):
    connect(db_path).close()
    reader = connect(db_path, mode="read")
    try:
        assert not writer_lock_path(db_path).exists()
        writer = connect(db_path)      # a writer may open alongside readers
        writer.close()
    finally:
        reader.close()


def test_transactions_are_re_entrant(store):
    # Composed operations nest: accepting a schema amendment registers a bundle,
    # which is itself transactional. A nested BEGIN is an error in SQLite, so the
    # outermost call owns the transaction and inner calls join it.
    with store.transaction():
        store.set_setting("outer", "1")
        with store.transaction():
            store.set_setting("inner", "2")
    assert store.setting("outer") == "1"
    assert store.setting("inner") == "2"


def test_an_inner_failure_rolls_back_the_whole_operation(store):
    store.set_setting("before", "kept")
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.set_setting("during", "written")
            with store.transaction():
                raise RuntimeError("boom")
    assert store.setting("before") == "kept"
    assert store.setting("during") is None


def test_the_store_survives_a_failed_open_without_stranding_the_lock(tmp_path):
    # A migration failure must not leave the lock held on a database nobody is
    # using -- that would shut every later attempt out of it.
    path = tmp_path / "broken.sqlite"
    import orpheus.store as store_module
    original = store_module.MIGRATIONS
    store_module.MIGRATIONS = [{"version": 1, "name": "bad",
                                "statements": ["THIS IS NOT SQL"]}]
    try:
        with pytest.raises(sqlite3.Error):
            connect(path)
        assert not writer_lock_path(path).exists()
    finally:
        store_module.MIGRATIONS = original


def test_checkpoint_folds_the_wal_back_into_the_file(store):
    store.set_setting("k", "v")
    store.checkpoint("TRUNCATE")
    # An immutable reader skips the WAL entirely; after a checkpoint it can see
    # the write. This is the mechanism behind the --immutable deployment trap.
    ro = sqlite3.connect(f"file:{store.path}?immutable=1", uri=True)
    assert ro.execute("SELECT value FROM org_settings WHERE key='k'").fetchone()[0] == "v"
    ro.close()


def test_settings_round_trip_and_upsert(store):
    assert store.setting("missing", "fallback") == "fallback"
    store.set_setting("cloud_ai_policy", "disabled")
    store.set_setting("cloud_ai_policy", "opt_in")
    assert store.setting("cloud_ai_policy") == "opt_in"
    assert store.scalar("SELECT COUNT(*) FROM org_settings WHERE key='cloud_ai_policy'") == 1
