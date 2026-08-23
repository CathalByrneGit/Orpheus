"""The two YAML files, and why they cannot be one."""

from __future__ import annotations

import pytest

import orpheus.bundle as bundle_mod
from orpheus.datasette_config import (build_config, build_metadata,
                                      instance_union, serve_command,
                                      write_config)

yaml = pytest.importorskip("yaml")


@pytest.fixture
def generated(tmp_path):
    paths = write_config(tmp_path / "datasette.yml")
    return (yaml.safe_load(open(paths["config"])),
            yaml.safe_load(open(paths["metadata"])),
            paths)


def test_both_files_are_parseable(generated):
    config, metadata, _ = generated
    assert isinstance(config, dict) and isinstance(metadata, dict)


def test_canned_queries_live_in_the_config_and_never_in_the_metadata(generated):
    # Datasette 1.0 reads queries from --config only. One left in the metadata
    # file loses its `sql` on the way through and the server dies at startup
    # with KeyError: 'sql'.
    config, metadata, _ = generated
    queries = config["databases"]["orpheus"]["queries"]
    assert queries
    assert all("sql" in q for q in queries.values())
    assert "queries" not in metadata["databases"]["orpheus"]


def test_descriptions_live_in_the_metadata_and_never_in_the_config(generated):
    # The mirror of the rule above: only --metadata reaches the rendered pages,
    # so a description in the config file renders nothing and says nothing.
    config, metadata, _ = generated
    assert metadata["databases"]["orpheus"]["tables"]["documents"]["description"]
    for table in config["databases"]["orpheus"]["tables"].values():
        assert "description" not in table


def test_the_queries_come_from_the_bundle_not_from_the_code(generated):
    config, _, _ = generated
    bundle_query_ids = {q["id"] for q in bundle_mod.queries(bundle_mod.load())}
    assert set(config["databases"]["orpheus"]["queries"]) == bundle_query_ids


def test_a_different_domain_ships_different_questions(tmp_path):
    # The queries were hardcoded once, which meant a domain-neutral engine
    # shipped a fixed set of contract-flavoured questions.
    bundle = bundle_mod.load()
    bundle["queries"] = [{
        "id": "applications_awaiting_decision",
        "display": {"name": "Applications awaiting decision"},
        "returns": {"kind": "table"},
        "definition": {"kind": "sql", "body": "SELECT 1"},
    }]
    config = build_config(bundle)
    assert list(config["databases"]["orpheus"]["queries"]) == \
        ["applications_awaiting_decision"]


def test_the_instance_union_is_expanded_from_the_bundles_tables(generated):
    config, _, _ = generated
    sql = config["databases"]["orpheus"]["queries"][
        "extraction_accuracy_by_confidence"]["sql"]
    assert "{{instanceUnion}}" not in sql
    assert "instances_Contract" in sql and "instances_KeyDate" in sql


def test_the_union_spans_every_managed_table():
    # instance_index deliberately carries no review status -- status lives on
    # the instance row, and copying it into the index would mean two places to
    # keep in step -- so a query grouping by status has to span the tables.
    bundle = bundle_mod.load()
    union = instance_union(bundle)
    for obj in bundle_mod.managed_object_types(bundle):
        assert bundle_mod.table_name(obj) in union


def test_the_title_comes_from_the_bundle_and_a_colon_does_not_break_it(tmp_path):
    bundle = bundle_mod.load()
    bundle.setdefault("metadata", {})["name"] = 'Planning: "core" set'
    metadata = build_metadata(bundle)
    assert "Planning" in metadata["title"]
    path = tmp_path / "m.yml"
    write_config(tmp_path / "d.yml", bundle=bundle, metadata_path=path)
    assert "Planning" in yaml.safe_load(open(path))["title"]


def test_closed_tables_stay_closed(generated):
    config, _, _ = generated
    tables = config["databases"]["orpheus"]["tables"]
    assert tables["actor_tokens"]["allow"] is False
    assert tables["actors"]["allow"] == {"is_admin": 1}
    assert config["allow"] == {"id": "*"}      # no anonymous access at all


def test_the_api_token_is_named_not_written(generated):
    config, _, _ = generated
    plugin = config["plugins"]["orpheus-datasette"]
    assert plugin["token"] == {"$env": "ORPHEUS_API_TOKEN"}


def test_the_permission_rule_is_emitted_for_the_datasette_hook(tmp_path):
    paths = write_config(tmp_path / "datasette.yml")
    text = open(paths["config"]).read()
    # Generated from auth.permission_sql(), so it cannot drift from what the
    # API enforces.
    assert "permission_resources_sql" in text
    assert ":actor_id" in text
    assert "document_shares" in text


def test_the_serve_command_is_not_immutable_by_default():
    # --immutable lets SQLite skip the WAL, so a live store reads as empty.
    command = serve_command()
    assert "--immutable" not in command
    assert "--metadata" in command and "--config" in command
    assert "--immutable" in serve_command(immutable=True)
