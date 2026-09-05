"""Work done on a clock, and the three things that makes different.

Nobody is present when a scheduled task runs, which is not a detail — it is
what every test here is about. Nobody is there to be named as the author of a
write, nobody is there to opt a document out of the building, and nobody is
watching a connection that a long job is holding. So: the actor is a machine
one and cannot hold a credential, the cloud gate refuses before it asks
anything else, and a task is expressed in chunks that add up to the same answer
as one pass.

The `datasette-cron` wiring is exercised in `test_cron_plugin.py`. Everything
here is the core, on a real store.
"""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus import auth, ingest as ingest_mod, llm, scheduled
from orpheus.utils import OrpheusError, naive_key


@pytest.fixture
def corpus(store, tmp_path):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    storage = tmp_path / "storage"
    for n, text in enumerate([
        "This Agreement is between Halloran Instruments, Inc. and Kestrel "
        "Medical Group PLC. The term ends on 2024-01-31.",
        "Amendment No. 2 with Halloran Instruments Inc. extends the term.",
        "Kestrel Medical Group PLC licenses from Ardmore Digital Ltd.",
    ], start=1):
        path = tmp_path / f"doc{n}.txt"
        path.write_text(text)
        ingest_mod.ingest(store, path, storage_root=storage)
    store.storage_root = storage
    return store


def _mention(store, instance_id, document_id, name):
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name, "
        "naive_key, source, confidence, status, created_at) "
        "VALUES (?,?,?,?,'ai_local',0.9,'unconfirmed',datetime('now'))",
        (instance_id, document_id, name, naive_key(name)))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name, "
        "document_id, created_at) VALUES (?,'Company','instances_Company',?,"
        "datetime('now'))", (instance_id, document_id))


# -- the catalogue ----------------------------------------------------------

def test_every_task_says_whether_it_writes():
    # Not decoration: the plugin picks execute_write_fn or execute_fn off this
    # flag, so a task that lies about it is wired to the wrong thread.
    assert {entry["name"] for entry in scheduled.catalogue()} == set(scheduled.TASKS)
    for entry in scheduled.catalogue():
        assert isinstance(entry["writes"], bool)
        assert entry["summary"].strip()
        assert entry["default_schedule"]


def test_an_unknown_task_names_the_ones_that_exist():
    with pytest.raises(OrpheusError, match="Known: calendar-digest"):
        scheduled.get("nightly-backup")


# -- the machine actor ------------------------------------------------------

def test_a_scheduled_write_is_attributed_to_a_machine_actor(corpus):
    documents = [row["document_id"] for row in
                 corpus.query("SELECT document_id FROM documents ORDER BY date_added")]
    _mention(corpus, "i1", documents[0], "Halloran Instruments, Inc.")
    _mention(corpus, "i2", documents[1], "Halloran Instruments Inc.")

    result = scheduled.run(corpus, "wiki-propose")
    assert result["proposed"] >= 1

    creators = {row["created_by"] for row in
                corpus.query("SELECT created_by FROM entities")}
    assert len(creators) == 1
    actor = auth.get_actor(corpus, creators.pop())
    assert scheduled.is_scheduled(actor)
    # Never an administrator. A machine actor with the flag would be an admin
    # account nobody signs in as and nobody watches.
    assert not actor["is_admin"]


def test_the_machine_actor_is_one_row_however_often_it_runs(corpus):
    first = scheduled.scheduled_actor(corpus)
    assert scheduled.scheduled_actor(corpus) == first
    assert corpus.scalar("SELECT COUNT(*) FROM actors WHERE idp = ?",
                         (scheduled.SCHEDULED_IDP,)) == 1


def test_nothing_can_sign_in_as_the_scheduler(corpus):
    actor_id = scheduled.scheduled_actor(corpus)
    with pytest.raises(OrpheusError, match="machine actor"):
        auth.create_token(corpus, actor_id, label="handy")
    assert corpus.scalar("SELECT COUNT(*) FROM actor_tokens") == 0


def test_a_person_can_still_be_minted_a_token(corpus):
    # The refusal is about the idp, not about tokens being awkward.
    person = auth.create_actor(corpus, "Nuala Ryan", idp="datasette",
                               external_id="nuala")
    assert auth.create_token(corpus, person)["token"]


# -- the cloud refusal ------------------------------------------------------

def test_a_scheduled_run_may_not_reach_a_cloud_model(corpus):
    """The gate refuses even where the deployment has enabled cloud and the
    caller claims an opt-in — which is the only test worth writing, because
    both of the gate's own conditions are satisfied here."""
    corpus.set_setting("cloud_ai_policy", "org_allow")
    llm.assert_cloud_allowed(corpus, True)          # allowed outside the block

    with llm.no_cloud(scheduled.CLOUD_REFUSAL):
        with pytest.raises(OrpheusError, match="clock cannot take it"):
            llm.assert_cloud_allowed(corpus, True)

    llm.assert_cloud_allowed(corpus, True)          # and allowed again after


def test_the_refusal_is_installed_around_the_work_not_around_the_caller(corpus):
    """`run_chunk` is the function that runs on the store's thread, so it is
    the only placement that holds for both the CLI and Datasette — where a
    ContextVar set on the event loop would not cross into the executor."""
    corpus.set_setting("cloud_ai_policy", "org_allow")
    seen = []

    original = scheduled.TASKS["verify"].run_chunk
    try:
        object.__setattr__(scheduled.TASKS["verify"], "run_chunk",
                           lambda store, config, chunk: seen.append(
                               llm.cloud_refusal()) or {"documents": []})
        scheduled.run_chunk(corpus, "verify", [])
    finally:
        object.__setattr__(scheduled.TASKS["verify"], "run_chunk", original)

    assert seen == [scheduled.CLOUD_REFUSAL]
    assert llm.cloud_refusal() is None


# -- chunking ---------------------------------------------------------------

def test_a_batched_verify_and_a_single_pass_agree(corpus):
    batched = scheduled.run(corpus, "verify", {"batch": 1})
    whole = ingest_mod.audit_storage(corpus, verify=True)
    assert [chunk for chunk in scheduled.plan(corpus, "verify", {"batch": 1})] == \
        [[document] for document in ingest_mod.audit_document_ids(corpus)]
    assert batched["n_documents"] == whole["n_documents"] == 3
    assert batched["headline"] == whole["headline"]
    assert batched["bytes_read"] == whole["bytes_read"] > 0


def test_an_empty_corpus_still_produces_one_chunk(store):
    bundle_mod.register(store, bundle_mod.load())
    bundle_mod.apply_schema(store, bundle_mod.load())
    assert scheduled.plan(store, "verify") == [[]]
    assert scheduled.run(store, "verify")["headline"] == "No documents to check."


# -- what the tasks actually do ---------------------------------------------

def test_verify_fails_when_an_original_is_not_the_file_that_was_ingested(corpus):
    path = corpus.one("SELECT storage_path FROM documents "
                      "ORDER BY date_added")["storage_path"]
    with open(path, "w") as handle:
        handle.write("not the bytes that were ingested")

    with pytest.raises(scheduled.TaskFailed) as failed:
        scheduled.run(corpus, "verify")
    assert "altered" in str(failed.value)
    # The full audit travels with the failure, so an operator reading the run
    # does not have to run the pass again to find out which document.
    assert failed.value.result["n_unavailable"] == 1


def test_a_redacted_document_is_not_a_failed_verify(corpus):
    from orpheus import redact
    document_id = corpus.one("SELECT document_id FROM documents "
                             "ORDER BY date_added")["document_id"]
    actor = auth.create_actor(corpus, "Nuala Ryan", is_admin=True)
    redact.redact_document(corpus, document_id, actor_id=actor,
                           note="subject access request")

    result = scheduled.run(corpus, "verify")
    assert result["n_redacted"] == 1
    assert result["n_unavailable"] == 0


def test_the_calendar_digest_is_silent_with_nothing_overdue(corpus):
    result = scheduled.run(corpus, "calendar-digest", {"within_days": 30})
    assert result["n_overdue"] == 0
    assert result["headline"]


def test_the_calendar_digest_speaks_up_when_something_is_past_its_date(corpus):
    document_id = corpus.one("SELECT document_id FROM documents "
                             "ORDER BY date_added")["document_id"]
    corpus.insert("instances_KeyDate", {
        "instance_id": "kd_1", "document_id": document_id, "value": "2001-01-31",
        "raw_text": "31 January 2001", "date_role": "end", "page_no": 1,
        "source": "ai_local", "confidence": 1.0, "status": "unconfirmed",
        "created_at": "2001-01-01T00:00:00Z"})
    corpus.insert("instance_index", {
        "instance_id": "kd_1", "document_id": document_id, "type_id": "KeyDate",
        "table_name": "instances_KeyDate",
        "created_at": "2001-01-01T00:00:00Z"})

    with pytest.raises(scheduled.TaskFailed) as failed:
        scheduled.run(corpus, "calendar-digest")
    assert failed.value.result["n_overdue"] == 1


def test_building_the_index_twice_changes_nothing(corpus):
    pytest.importorskip("sqlite_utils")
    first = scheduled.run(corpus, "search-index")
    assert set(first["indexes"].values()) == {"indexed"}
    assert set(scheduled.run(corpus, "search-index")["indexes"].values()) == \
        {"already indexed"}
    assert set(scheduled.run(corpus, "search-index",
                             {"rebuild": True})["indexes"].values()) == {"rebuilt"}


def test_a_quiet_propose_says_which_kind_of_quiet_it_is(corpus):
    """Two different silences. "Nothing to propose" on a store nothing has been
    extracted from is reporting that extraction never ran, and reads as
    reassurance unless it says so."""
    empty = scheduled.run(corpus, "wiki-propose")
    assert "nothing has been extracted" in empty["headline"]

    documents = [row["document_id"] for row in
                 corpus.query("SELECT document_id FROM documents ORDER BY date_added")]
    _mention(corpus, "i1", documents[0], "Halloran Instruments, Inc.")
    assert scheduled.run(corpus, "wiki-propose")["proposed"] == 1
    assert "already on a page" in scheduled.run(corpus, "wiki-propose")["headline"]


def test_a_config_flag_typed_at_a_terminal_means_what_it_says(corpus):
    """`--config rebuild=false` arrives as the string "false", which is truthy.
    Reading it with bool() would make the spelling a person is most likely to
    type mean the opposite of what it says."""
    pytest.importorskip("sqlite_utils")
    scheduled.run(corpus, "search-index")
    assert set(scheduled.run(corpus, "search-index",
                             {"rebuild": "false"})["indexes"].values()) == \
        {"already indexed"}
    assert set(scheduled.run(corpus, "search-index",
                             {"rebuild": "true"})["indexes"].values()) == {"rebuilt"}
    # And a YAML block, which gives a real bool.
    assert set(scheduled.run(corpus, "search-index",
                             {"rebuild": True})["indexes"].values()) == {"rebuilt"}


def test_a_task_needing_a_missing_extra_is_not_offered(monkeypatch):
    monkeypatch.setitem(scheduled._EXTRA_AVAILABLE, "search", lambda: False)
    assert not scheduled.TASKS["search-index"].available()
    offered = {entry["name"] for entry in scheduled.catalogue()
               if entry["available"]}
    assert "search-index" not in offered
    assert "verify" in offered
