"""The Orpheus tool pack for datasette-agent.

These run the tool functions against a real store on a real Datasette, because
the thing worth testing is not that they return JSON — it is that the review
state and the provenance survive into the payload. A tool that answered like
`sql_query` would pass a shape check and fail the only requirement it has.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus.datasette_config import build_config
from orpheus.store import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins"))
datasette_app = pytest.importorskip("datasette.app")
agent = pytest.importorskip("orpheus_agent")


@pytest.fixture
def served(tmp_path):
    db = tmp_path / "store.sqlite"
    store = connect(db)
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.execute(
        "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
        " n_pages, date_added, created_by, visibility, review_status)"
        " VALUES ('doc_1','contract.txt','h1',100,1,datetime('now'),'act_a',"
        "'private','unreviewed')")
    text = "Ardmore Digital Ltd of 12 Ushers Quay is the supplier."
    store.execute(
        "INSERT INTO document_pages (document_id, page_no, text, text_source,"
        " char_count) VALUES ('doc_1',1,?,'text',?)", (text, len(text)))
    store.execute(
        "INSERT INTO instance_index (instance_id, type_id, table_name,"
        " document_id, created_at) VALUES ('inst_1','Company',"
        "'instances_Company','doc_1','2026-01-01T00:00:00Z')")
    # naive_key strips the legal suffix, so it is 'ardmore digital', not the
    # lowercased name. unextracted_mentions matches on it.
    store.execute(
        "INSERT INTO instances_Company (instance_id, document_id, name,"
        " naive_key, address, source, confidence, status, created_at) VALUES"
        " ('inst_1','doc_1','Ardmore Digital Ltd','ardmore digital',"
        "'12 Ushers Quay','ai_cloud',1.0,'unconfirmed','2026-01-01T00:00:00Z')")
    store.execute(
        "INSERT INTO provenance (provenance_id, instance_id, document_id,"
        " source_label, source, page_no, excerpt, confidence, alignment,"
        " created_at) VALUES ('p1','inst_1','doc_1','engine','ai_cloud',1,"
        "'Ardmore Digital Ltd of 12 Ushers Quay',1.0,'match_exact',"
        "'2026-01-01T00:00:00Z')")
    store.conn.commit()
    from orpheus import search as search_mod
    if search_mod.available():
        search_mod.enable_search(store)
        store.conn.commit()
    config = build_config(bundle, database_name="store", storage_root="s")
    store.close()

    datasette = datasette_app.Datasette([str(db)], config=config)
    asyncio.run(datasette.invoke_startup())
    return datasette


def call(fn, datasette, **kwargs):
    return json.loads(asyncio.run(fn(datasette, {"id": "act_a"}, **kwargs)))


# -- the review state has to survive into the payload ------------------------

def test_a_lookup_distinguishes_absent_from_unextracted(served):
    result = call(agent.find_entity, served, query="Ardmore Digital Ltd")
    assert "documents_naming_it_with_no_extraction" in result
    assert "doc_1" in result["documents_already_carrying_it"]
    assert "not 'absent'" in result["reading"]


def test_a_partial_name_matches_pages_but_not_the_extraction_check(served):
    # Two different matchers in one payload: pages are found by substring, and
    # the extraction check by a normalised whole name. A half-name therefore
    # finds no extraction even where one exists, which the caveat has to own —
    # otherwise it reads as "the extractor missed this".
    result = call(agent.find_entity, served, query="Ardmore")
    assert result["documents_already_carrying_it"] == []
    assert "neither is entity resolution" in result["caveat"]


def test_a_name_the_extractor_never_saw_is_reported_as_such(served):
    result = call(agent.find_entity, served, query="Ushers")
    # The phrase is in the document and no instance carries it, which is the
    # case orpheus_record exists for.
    assert result["documents_naming_it_with_no_extraction"]
    assert not result["documents_already_carrying_it"]


def test_the_page_payload_carries_status_and_provenance(served):
    from orpheus import entities as entities_mod

    def make(conn):
        from orpheus.store import Store
        store = Store.adopt(conn, path=None, owns_transaction=False)
        entity_id = entities_mod.create_entity(
            store, "Company", "Ardmore Digital Ltd", actor_id="act_a")
        entities_mod.link_mention(store, entity_id, "inst_1",
                                  actor_id="act_a", basis="naive_key")
        return entity_id

    database = served.get_database("store")
    entity_id = asyncio.run(database.execute_write_fn(make))

    result = call(agent.entity_page, served, entity_id=entity_id)
    blob = json.dumps(result)
    assert "unconfirmed" in blob, \
        "an answer written without the review state is the failure this exists to stop"
    assert "12 Ushers Quay" in blob
    assert "match_exact" in blob or "provenance" in blob
    assert "unconfirmed extraction" in result["reading"]


def test_every_tool_tells_the_model_not_to_use_raw_sql(served):
    tools = agent._tools() if agent.AgentTool is not None else []
    if not tools:
        pytest.skip("datasette-agent is not installed")
    lookups = [t for t in tools if t.name.startswith("orpheus_")
               and t.name not in ("orpheus_record", "orpheus_needs_review")]
    assert lookups
    for tool in lookups:
        assert "instead of sql_query" in tool.description


def test_the_hook_registers_nothing_when_the_extra_is_absent(monkeypatch, served):
    # The plugin's own pages must keep working without datasette-agent, which
    # is the normal install.
    monkeypatch.setattr(agent, "AgentTool", None)
    assert agent.register_agent_tools(served) == []


def test_a_missing_search_index_is_reported_rather_than_thrown(tmp_path):
    # The index is optional, and without it "the extractor missed this" cannot
    # be told apart from "no document says it" — opposite replies, so the tool
    # says it cannot tell rather than guessing or crashing inside a chat.
    from orpheus import search as search_mod

    db = tmp_path / "store.sqlite"
    store = connect(db)
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    store.conn.commit()
    config = build_config(bundle, database_name="store", storage_root="s")
    store.close()

    datasette = datasette_app.Datasette([str(db)], config=config)
    asyncio.run(datasette.invoke_startup())

    result = call(agent.find_entity, datasette, query="Anybody")
    assert result["documents_naming_it_with_no_extraction"] is None
    assert "do not report either" in result["caveat"]
