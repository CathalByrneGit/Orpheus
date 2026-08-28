import os
import sqlite3

import pytest

from orpheus.store import Store, connect, writer_lock_path
from orpheus.utils import OrpheusError


def test_a_new_store_is_migrated_and_in_wal_mode(store):
    from orpheus.schema import MIGRATIONS

    versions = [r["version"] for r in store.query("SELECT version FROM schema_migrations")]
    # Every declared migration, in order, and no gaps. Pinned to MIGRATIONS
    # rather than to a literal list, because a literal one fails on every new
    # migration and says nothing about whether that migration was correct.
    assert versions == sorted(m["version"] for m in MIGRATIONS)
    assert versions == list(range(1, len(versions) + 1))
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


# -- naive_key, and the migration that repairs stored ones -------------------

def test_a_holding_company_no_longer_shares_a_key_with_its_subsidiary():
    """The suffix list once stripped `group` and `holdings` anywhere in a name.

    Those are not legal forms — they are name components, and they denote a
    different legal entity in a corporate structure. The result was a false
    merge, which is strictly worse than the false split this function is
    documented as having: a split leaves two rows a person can join, while a
    merge combines two organisations and leaves nothing to notice.
    """
    from orpheus.utils import naive_key

    for parent, child in (("Kestrel Medical Group", "Kestrel Medical Ltd"),
                          ("Ardmore Holdings plc", "Ardmore Ltd"),
                          ("CRH Group", "CRH plc"),
                          ("Smith Group", "Smith Holdings")):
        assert naive_key(parent) != naive_key(child), (parent, child)


def test_two_renderings_of_one_company_still_match():
    from orpheus.utils import naive_key

    for a, b in (("Halloran Instruments, Inc.", "Halloran Instruments Inc"),
                 ("Ardmore Digital Limited", "ARDMORE DIGITAL LTD"),
                 ("Foo Co Ltd", "Foo Limited"),          # stacked forms
                 ("The Kestrel Group", "Kestrel Group")):  # a leading article
        assert naive_key(a) == naive_key(b), (a, b)


def test_a_legal_form_inside_a_name_is_left_alone():
    # Unanchored matching took the "co" out of "Costa Coffee".
    from orpheus.utils import naive_key

    assert naive_key("Costa Coffee") == "costa coffee"
    assert naive_key("The Boston Consulting Group") == "boston consulting group"
    # A name that is only a legal form keeps it: an empty key would match every
    # other empty key.
    assert naive_key("Company") == "company"
    assert naive_key("") == ""


def test_a_title_is_not_part_of_a_persons_name():
    """Three pages for one man, found by running 40 real filings.

    "Dr. Mitchell Felder", "Mitchell Felder" and "Mitchell S. Felder" were three
    keys, three pages, and his three relations split across them.
    """
    from orpheus.utils import naive_key

    assert naive_key("Dr. Mitchell Felder", "personal") == \
           naive_key("Mitchell Felder", "personal")
    # Stacked, like stacked legal forms.
    assert naive_key("Prof. Dr. Meier", "personal") == naive_key("Meier", "personal")
    # A middle initial is a real difference, and is left to a candidate rather
    # than merged here: John A. Smith is not John B. Smith.
    assert naive_key("Mitchell S. Felder", "personal") != \
           naive_key("Mitchell Felder", "personal")


def test_a_title_stripped_from_a_company_would_be_a_false_merge():
    # Which is why the style is per type and not a general rule. This function's
    # output is matched on for equality, so a bad strip merges silently.
    from orpheus.utils import naive_key

    assert naive_key("Dr Pepper Snapple Group") == "dr pepper snapple group"
    assert naive_key("Dr Pepper Snapple Group", "organisation") == \
           "dr pepper snapple group"
    # And a name that is only a title keeps it, for the same reason a name that
    # is only a legal form does.
    assert naive_key("Dr.", "personal") == "dr"


def test_each_style_leaves_the_other_ones_work_alone():
    # A personal name does not lose a trailing word that happens to be a legal
    # form, and an organisation does not lose a leading honorific.
    from orpheus.utils import naive_key

    assert naive_key("Mary Coe", "personal") == "mary coe"
    assert naive_key("Ardmore Digital Ltd", "organisation") == "ardmore digital"
    # An unsaid style is the organisation one, so an older bundle keeps its keys.
    assert naive_key("Ardmore Digital Ltd") == naive_key("Ardmore Digital Ltd",
                                                          "organisation")


def test_the_bundle_says_which_style_a_type_uses():
    import orpheus.bundle as bundle_mod

    bundle = bundle_mod.load()
    assert bundle_mod.key_for(bundle, "Person", "Dr. Mitchell Felder") == \
           "mitchell felder"
    assert bundle_mod.key_for(bundle, "Company", "Dr Pepper Snapple Group") == \
           "dr pepper snapple group"
    # A type the bundle does not describe falls back rather than raising: the
    # caller is asking for a key, not for a schema check.
    assert bundle_mod.key_for(bundle, "NoSuchType", "Ardmore Ltd") == "ardmore"


def test_the_known_false_split_is_still_there():
    # Documented rather than hidden, so the limitation cannot quietly vanish.
    from orpheus.utils import naive_key

    assert naive_key("Ernst & Young") != naive_key("Ernst and Young")


def test_stored_keys_are_recomputed_when_an_old_store_is_opened(tmp_path):
    import orpheus.bundle as bundle_mod

    path = tmp_path / "old.sqlite"
    store = Store(str(path), mode="write")
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.execute("INSERT INTO documents (document_id, filename, file_hash, byte_size,"
                  " n_pages, date_added, visibility, review_status)"
                  " VALUES ('doc_1','a.txt','h',10,1,datetime('now'),'private','unreviewed')")
    for instance_id, name in (("i1", "Kestrel Medical Group"),
                              ("i2", "Kestrel Medical Ltd")):
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name, naive_key,"
            " source, confidence, status, created_at)"
            " VALUES (?,?,?,?,'ai_local',1.0,'unconfirmed',datetime('now'))",
            (instance_id, "doc_1", name, "kestrel medical"))   # the old key, shared
    store.execute("DELETE FROM schema_migrations WHERE version = 5")
    store.conn.commit()
    store.close()

    reopened = Store(str(path), mode="write", force_lock=True)
    try:
        keys = [r["naive_key"] for r in reopened.query(
            "SELECT naive_key FROM instances_Company ORDER BY instance_id")]
        assert len(set(keys)) == 2, keys
        # The recompute is recorded, like any other change to the store.
        assert reopened.one("SELECT note FROM edit_history WHERE action = 'migrate'")
    finally:
        reopened.close()


def test_an_old_stores_personal_keys_are_recomputed_in_the_new_style(tmp_path):
    # The page and the instance both hold a key, and they have to agree: a page
    # written in one style and looked up in the other silently never matches,
    # which reads as "no page exists" rather than as a bug.
    import orpheus.bundle as bundle_mod

    path = tmp_path / "old.sqlite"
    store = Store(str(path), mode="write")
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.execute("INSERT INTO documents (document_id, filename, file_hash, byte_size,"
                  " n_pages, date_added, visibility, review_status)"
                  " VALUES ('doc_1','a.txt','h',10,1,datetime('now'),'private','unreviewed')")
    store.execute(
        "INSERT INTO instances_Person (instance_id, document_id, name, naive_key,"
        " source, confidence, status, created_at)"
        " VALUES ('i1','doc_1','Dr. Mitchell Felder','dr mitchell felder',"
        "'ai_local',1.0,'unconfirmed',datetime('now'))")
    store.execute(
        "INSERT INTO entities (entity_id, type_id, canonical_name, naive_key,"
        " source, confidence, status, created_at)"
        " VALUES ('ent_1','Person','Dr. Mitchell Felder','dr mitchell felder',"
        "'ai_local',0.7,'unconfirmed',datetime('now'))")
    store.execute("DELETE FROM schema_migrations WHERE version = 10")
    store.conn.commit()
    store.close()

    reopened = Store(str(path), mode="write", force_lock=True)
    try:
        assert reopened.scalar(
            "SELECT naive_key FROM instances_Person WHERE instance_id = 'i1'") == \
            "mitchell felder"
        assert reopened.scalar(
            "SELECT naive_key FROM entities WHERE entity_id = 'ent_1'") == \
            "mitchell felder"
    finally:
        reopened.close()


def test_recomputing_marks_corpus_comparisons_stale(tmp_path):
    # They were computed on the old keys. Silently stale is worse than visibly
    # stale, which is the whole reason the staleness machinery exists.
    import orpheus.bundle as bundle_mod

    path = tmp_path / "stale.sqlite"
    store = Store(str(path), mode="write")
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    store.execute("INSERT INTO documents (document_id, filename, file_hash, byte_size,"
                  " n_pages, date_added, visibility, review_status)"
                  " VALUES ('doc_1','a.txt','h',10,1,datetime('now'),'private','unreviewed')")
    store.execute("INSERT INTO instances_Company (instance_id, document_id, name, naive_key,"
                  " source, confidence, status, created_at)"
                  " VALUES ('i1','doc_1','Kestrel Medical Group','kestrel medical',"
                  " 'ai_local',1.0,'unconfirmed',datetime('now'))")
    store.execute(
        "INSERT INTO concept_evaluations (evaluation_id, concept_id, concept_version,"
        " concept_scope, kind, scope, target_document_id, result, source, confidence,"
        " status, generated_at, stale) VALUES ('ev1','m',1,'corpus','corpus',"
        " 'document','doc_1','{}','ai_local',1.0,'unconfirmed',datetime('now'),0)")
    store.execute("DELETE FROM schema_migrations WHERE version = 5")
    store.conn.commit()
    store.close()

    reopened = Store(str(path), mode="write", force_lock=True)
    try:
        row = reopened.one("SELECT stale, stale_reason FROM concept_evaluations")
        assert row["stale"] == 1
        assert "recomputed" in row["stale_reason"]
    finally:
        reopened.close()


def test_a_fresh_store_needs_no_recompute(store):
    # Nothing to repair, and no spurious audit row claiming otherwise.
    assert store.one("SELECT 1 FROM edit_history WHERE action = 'migrate'") is None


# -- serving a store this build does not match --------------------------------
#
# Migrations run only on a write open, and Datasette holds a shared connection,
# so an upgraded deployment serves a stale schema until somebody runs the CLI.
# Found by upgrading a store and watching every new route return `no such
# table` -- invisible to the suite, because tests build fresh stores.

def test_a_fresh_store_is_current(store):
    assert store.pending_migrations() == []
    store.assert_current()


def test_a_store_behind_this_build_names_the_fix(store):
    from orpheus.utils import OrpheusError
    store.execute("DELETE FROM schema_migrations WHERE version = "
                  "(SELECT MAX(version) FROM schema_migrations)")
    pending = store.pending_migrations()
    assert len(pending) == 1

    with pytest.raises(OrpheusError) as caught:
        store.assert_current()
    message = str(caught.value)
    # A sentence naming the command, not `no such table` from a route that
    # worked yesterday.
    assert "orpheus" in message and "migrate" in message
    assert str(pending) in message


def test_a_store_with_no_migration_history_is_entirely_behind(db_path):
    import sqlite3
    from orpheus.schema import MIGRATIONS
    sqlite3.connect(db_path).close()
    empty = Store(db_path, mode="read")
    try:
        assert empty.pending_migrations() == [m["version"] for m in MIGRATIONS]
    finally:
        empty.close()
