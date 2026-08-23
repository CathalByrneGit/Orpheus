"""The command line, exercised as a person would use it.

Every test drives `cli()` with an argv list rather than calling the command
functions, because argparse is half of what could be wrong: a missing default,
a flag that does not reach the core, a subcommand wired to the previous one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orpheus.cli import cli
from orpheus.population import set_populator
from orpheus.store import Store

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"

COMPANY = {"type": "Company", "excerpt": "Ardmore Digital Limited",
           "properties": {"name": "Ardmore Digital Limited"}}


@pytest.fixture(autouse=True)
def no_leaked_populator():
    yield
    set_populator(None)


@pytest.fixture
def offline():
    """No model reachable. The deterministic pass still has to work."""
    def broken(**kwargs):
        raise RuntimeError("no model is configured")
    set_populator(broken)
    return broken


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def out_json(capsys, *argv) -> dict:
    code, out, err = run(capsys, *argv)
    assert code in (0, 2), f"exit {code}: {err}"
    return json.loads(out)


@pytest.fixture
def initialised(tmp_path, capsys):
    db = str(tmp_path / "orpheus.sqlite")
    result = out_json(capsys, "--db", db, "--json", "init",
                      "--admin", "Ada Byrne", "--cloud-policy", "org_allow",
                      "--config", str(tmp_path / "datasette.yml"),
                      "--storage-root", str(tmp_path / "storage"))
    return db, result["admin_actor_id"], tmp_path


# -- init --------------------------------------------------------------------

def test_init_leaves_a_working_store_and_both_datasette_files(tmp_path, capsys):
    db = str(tmp_path / "orpheus.sqlite")
    result = out_json(capsys, "--db", db, "--json", "init",
                      "--config", str(tmp_path / "datasette.yml"),
                      "--storage-root", str(tmp_path / "storage"))
    assert Path(result["config"]).exists()
    assert Path(result["metadata"]).exists()
    assert result["bundle"] == "contract-core"
    # The command it prints is the one that serves what it just built.
    assert db in result["next"] and "--plugins-dir" in result["next"]

    store = Store(db, mode="read")
    tables = {r["name"] for r in store.query(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "instances_Contract" in tables and "provenance" in tables
    # Concepts and scores are set up too: a store you have to run a second
    # command against before it can flag anything is not initialised.
    assert store.scalar("SELECT COUNT(*) FROM concept_definitions") > 0


def test_init_makes_the_admin_an_admin(initialised):
    db, actor_id, _ = initialised
    store = Store(db, mode="read")
    assert store.scalar("SELECT is_admin FROM actors WHERE actor_id = ?",
                        (actor_id,)) == 1
    assert store.setting("cloud_ai_policy") == "org_allow"


# -- ingest ------------------------------------------------------------------

def test_ingesting_a_directory_takes_every_document_in_it(initialised, capsys,
                                                          offline):
    db, actor_id, tmp_path = initialised
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "a.pdf").write_bytes(PDF.read_bytes())
    (incoming / "b.txt").write_text(
        "This Agreement commences on 1 April 2025 and terminates on "
        "31 March 2027.\nThe total contract value is EUR 250,000.\n")
    (incoming / "notes.xlsx").write_text("not a document type we read")

    result = out_json(capsys, "--db", db, "--json", "ingest", str(incoming),
                      "--actor-id", actor_id, "--extract",
                      "--storage-root", str(tmp_path / "storage"))
    # The spreadsheet is not one of the suffixes, so it is not attempted.
    assert len(result["documents"]) == 2
    assert result["n_failed"] == 0


def test_a_corpus_run_survives_one_unreadable_file(initialised, capsys, offline):
    # One bad PDF in a hundred should cost that document, not the run.
    db, actor_id, tmp_path = initialised
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    (incoming / "good.pdf").write_bytes(PDF.read_bytes())
    (incoming / "broken.pdf").write_bytes(b"not a PDF at all")

    result = out_json(capsys, "--db", db, "--json", "ingest", str(incoming),
                      "--actor-id", actor_id, "--extract",
                      "--storage-root", str(tmp_path / "storage"))
    assert result["n_failed"] == 1
    good = [d for d in result["documents"] if not d.get("error")]
    assert len(good) == 1
    assert good[0]["extraction"]["n_deterministic"] > 0


def test_the_deterministic_pass_works_with_no_model_at_all(initialised, capsys,
                                                           offline):
    """The premise of the local tier, checked from the outside.

    A deployment with no model configured must still get dates and amounts out
    of a contract, and must be told plainly that the model half did not run.
    """
    db, actor_id, tmp_path = initialised
    result = out_json(capsys, "--db", db, "--json", "ingest", str(PDF),
                      "--actor-id", actor_id, "--extract",
                      "--storage-root", str(tmp_path / "storage"))
    extraction = result["documents"][0]["extraction"]
    assert extraction["n_deterministic"] > 0
    assert "no model is configured" in extraction["model_error"]

    store = Store(db, mode="read")
    assert store.scalar("SELECT status FROM extraction_runs") == "partial"


def test_the_human_readable_line_counts_what_was_actually_found(initialised,
                                                               capsys, offline):
    # It reported "0 found" for a run that found four dates without a model,
    # because it printed only the model's tally.
    db, actor_id, tmp_path = initialised
    code, out, _ = run(capsys, "--db", db, "ingest", str(PDF),
                       "--actor-id", actor_id, "--extract",
                       "--storage-root", str(tmp_path / "storage"))
    assert code == 0
    assert "0 found" not in out
    assert "deterministic only" in out


def test_ingesting_the_same_content_twice_is_reported_not_duplicated(
        initialised, capsys, offline):
    db, actor_id, tmp_path = initialised
    args = ("--db", db, "--json", "ingest", str(PDF), "--actor-id", actor_id,
            "--storage-root", str(tmp_path / "storage"))
    out_json(capsys, *args)
    second = out_json(capsys, *args)
    assert second["documents"][0]["duplicate"] is True
    assert Store(db, mode="read").scalar("SELECT COUNT(*) FROM documents") == 1


# -- review ------------------------------------------------------------------

@pytest.fixture
def reviewable(initialised, capsys, offline):
    db, actor_id, tmp_path = initialised
    out_json(capsys, "--db", db, "--json", "ingest", str(PDF),
             "--actor-id", actor_id, "--extract",
             "--storage-root", str(tmp_path / "storage"))
    store = Store(db, mode="read")
    ids = [r["instance_id"] for r in store.query(
        "SELECT instance_id FROM instance_index ORDER BY created_at")]
    return db, actor_id, ids


def test_the_three_verbs_reach_the_store(reviewable, capsys):
    db, actor_id, ids = reviewable
    out_json(capsys, "--db", db, "--json", "review", "confirm", ids[0],
             "--actor-id", actor_id, "--note", "checked")
    out_json(capsys, "--db", db, "--json", "review", "reject", ids[1],
             "--actor-id", actor_id)
    progress = out_json(capsys, "--db", db, "--json", "review", "amend", ids[2],
                        "--actor-id", actor_id, "--set", "date_role=start")
    assert progress["confirmed"] == 1
    assert progress["rejected"] == 1
    assert progress["amended"] == 1


def test_an_amendment_that_changes_nothing_is_refused_at_the_command_line(
        reviewable, capsys):
    db, actor_id, ids = reviewable
    store = Store(db, mode="read")
    row = store.one("SELECT date_role FROM instances_KeyDate WHERE instance_id = ?",
                    (ids[0],))
    code, _, err = run(capsys, "--db", db, "review", "amend", ids[0],
                       "--actor-id", actor_id,
                       "--set", f"date_role={row['date_role']}")
    assert code == 1
    assert "Nothing was changed" in err


def test_a_core_error_is_a_sentence_not_a_traceback(reviewable, capsys):
    db, actor_id, _ = reviewable
    code, _, err = run(capsys, "--db", db, "review", "confirm", "inst_nope",
                       "--actor-id", actor_id)
    assert code == 1
    assert err.startswith("orpheus: ")
    assert "Traceback" not in err


# -- report ------------------------------------------------------------------

def test_the_report_says_when_there_is_not_enough_to_say(reviewable, capsys):
    db, _, _ = reviewable
    code, out, _ = run(capsys, "--db", db, "report")
    assert code == 0
    assert "Not enough" in out


def test_the_report_measures_what_reviewers_did(reviewable, capsys):
    db, actor_id, ids = reviewable
    for instance_id in ids[:4]:
        out_json(capsys, "--db", db, "--json", "review", "confirm",
                 instance_id, "--actor-id", actor_id)
    out_json(capsys, "--db", db, "--json", "review", "reject", ids[4],
             "--actor-id", actor_id)

    report = out_json(capsys, "--db", db, "--json", "report", "--min-reviewed", "3")
    overall = report["extraction"]["overall"]
    assert overall["n_reviewed"] == 5
    assert overall["n_confirmed"] == 4
    assert overall["n_rejected"] == 1
    assert "confirmed as extracted" in report["headline"]


def test_the_report_opens_the_store_read_only(reviewable, capsys, tmp_path):
    # Measuring quality must never be able to change it, and a reader must not
    # take the writer lock a live Datasette needs.
    db, _, _ = reviewable
    run(capsys, "--db", db, "report")
    assert not Path(db + ".orpheus-writer").exists()


# -- the rest ----------------------------------------------------------------

def test_the_bundle_command_validates_without_a_store(capsys):
    result = out_json(capsys, "--json", "bundle")
    assert result["valid"] is True
    assert "instances_Contract" in result["tables"]


def test_a_token_is_returned_once_and_stored_only_as_a_hash(initialised, capsys):
    db, actor_id, _ = initialised
    result = out_json(capsys, "--db", db, "--json", "token", actor_id,
                      "--label", "corpus runner")
    store = Store(db, mode="read")
    stored = store.one("SELECT token_hash FROM actor_tokens")
    assert result["token"] not in stored["token_hash"]
    assert len(result["token"]) > 20


def test_serve_prints_the_command_rather_than_running_it(initialised, capsys):
    db, _, tmp_path = initialised
    code, out, _ = run(capsys, "--db", db, "serve", "--port", "9999",
                       "--print-only")
    assert code == 0
    assert "datasette serve" in out and "--port 9999" in out
    assert "--immutable" not in out


def test_config_regenerates_from_the_bundle_in_the_store(initialised, capsys,
                                                         tmp_path):
    db, _, _ = initialised
    target = tmp_path / "regenerated" / "datasette.yml"
    result = out_json(capsys, "--db", db, "--json", "config",
                      "--config", str(target))
    assert Path(result["config"]).exists() and Path(result["metadata"]).exists()
    assert "orpheus-datasette" in target.read_text()


def test_an_unknown_subcommand_is_refused(capsys):
    with pytest.raises(SystemExit):
        cli(["nonsense"])
