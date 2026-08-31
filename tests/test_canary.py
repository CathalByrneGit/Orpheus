"""The corpus canary: run the whole pipeline and check the *shape* of what
comes out.

Every defect found in this project over the last stretch of work came from
running documents, and **not one of them was findable by the unit suite**:

| found by running a corpus | why a unit test could not see it |
|---|---|
| 174 meetings merged onto one page | needed a bundle that had just gained `naive_key`, so the column was null on every existing row |
| classification failing on 88 of 88 documents | the failure is caught per document and reported quietly, so one missing model cannot stop an ingest |
| `sector` fragmenting into thirteen spellings of one answer | needed forty-eight real documents before the pattern was visible |
| graph coverage at 8% | needed a bundle a reviewer had actually chosen, with types that had no name |
| an amendment merged into the agreement it amends | needed one filing that contained both |

Each of those is a claim about the *shape* of a finished run — how many pages,
how the mentions distribute across them, whether a stage ran at all — and none
is a claim about any one function. So this file runs the pipeline end to end
over a small committed corpus and asserts the shape.

**Only the model is stubbed.** The reply comes from `tests/canary/replies.py`
over a real HTTP socket through the real `chat` engine, so `engines.ask`,
`_post_chat`, `_parse_chat_payload`, `normalise_population`, `align`,
`insert_instance`, the edge writer and `record_llm_call` are all the shipped
code. That is deliberate: every bug in the table above lived in the plumbing
around the model, not in the model.

It is fast, needs no network and no API key, and runs in the ordinary suite.

Every assertion below was checked by injecting the bug it claims to catch and
confirming it fails. `tests/canary/mutations.py` is that check, kept so the
claim stays checkable rather than remembered -- run it by hand, it edits the
tree. All eight faults are caught. Two were not, the first time, and both
misses were in this file rather than in the code:

- Paraphrased excerpts align to `None`, not `match_fuzzy`, and the assertion
  counted only `WHERE alignment IS NOT NULL` -- so it scored a paraphrasing
  model 35 out of 35 and passed. It now counts every excerpt and allows no
  drift at all, because the stub quotes verbatim by construction.
- An out-of-vocabulary `sector` never reaches the column: `classify` drops it
  to NULL. So removing the guard changed nothing, and a model that had stopped
  speaking the vocabulary was invisible. The shape that fault really has is
  silence, which is now half of what the vocabulary assertion checks.

What it does not catch: anything about whether an extraction is *right*. The
model is stubbed, so the canary can only see plumbing -- that a stage ran, that
what it produced is grounded, that pages and edges have the shape a corpus of
this construction must produce. Judging a reading needs a reviewer, and nobody
has reviewed a corpus yet.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import orpheus.bundle as bundle_mod
from orpheus import align, classify, entities, extract as extract_mod
from orpheus import graph, ingest
from orpheus import lint as lint_mod, ontology, quality
from orpheus.store import connect

CANARY = Path(__file__).parent / "canary"
CORPUS = CANARY / "corpus"
sys.path.insert(0, str(CANARY))
import replies  # noqa: E402


# ---------------------------------------------------------------------------
# The stub model, on a real socket
# ---------------------------------------------------------------------------

class _Model(BaseHTTPRequestHandler):
    """Answers whatever the pipeline asks, per document.

    Which reply to give is decided by looking for the document's own text in
    the request, exactly as a real model would have to. Nothing here inspects
    Orpheus's internals.
    """

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        system = body["messages"][0]["content"]
        document = body["messages"][1]["content"]

        name = _document_for(document)
        if name is None:
            payload = {"error": "the stub was sent text from no canary document"}
        elif system.startswith("Classify this document"):
            payload = replies.CLASSIFY[name]
        else:
            payload = replies.EXTRACT[name]

        out = json.dumps(
            {"choices": [{"message": {"content": json.dumps(payload)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def _fingerprint(name: str) -> str:
    """The reference line, matched whole.

    Matched as a whole line and not as a substring, which the first version got
    wrong: `Reference: HSE/2024/0117` is a prefix of `HSE/2024/0117-A1`, so the
    amendment was handed the agreement's reply and every excerpt in it came
    from the wrong document. The canary caught that on its first run, which is
    the check working -- but a fixture that lies is worse than no fixture.
    """
    for line in (CORPUS / name).read_text().splitlines():
        if line.startswith("Reference:"):
            return line.strip()
    raise AssertionError(f"{name} has no Reference: line to identify it by")


def _document_for(text: str) -> str | None:
    """Which canary document this text is, by exact reference line."""
    lines = {line.strip() for line in text.splitlines()}
    matched = [n for n in replies.EXTRACT if _fingerprint(n) in lines]
    assert len(matched) < 2, f"ambiguous canary references: {matched}"
    return matched[0] if matched else None


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def canary(tmp_path_factory):
    """One pipeline run over the whole corpus, shared by every check below."""
    root = tmp_path_factory.mktemp("canary")
    server = HTTPServer(("127.0.0.1", 0), _Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    store = connect(root / "canary.sqlite")
    bundle = bundle_mod.load()
    store.insert("actors", {"actor_id": "act_canary", "display_name": "Canary",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle_mod.register(store, bundle, actor_id="act_canary")
    bundle_mod.apply_schema(store, bundle)
    store.set_setting("extraction_engine", "chat", "act_canary")
    store.set_setting("local_base_url",
                      f"http://127.0.0.1:{server.server_port}/v1", "act_canary")
    store.conn.commit()

    documents = []
    for path in sorted(CORPUS.glob("*.txt")):
        result = ingest.ingest(store, path, actor_id="act_canary",
                               storage_root=root / "storage")
        classify.classify(store, result["document_id"], actor_id="act_canary")
        extract_mod.extract(store, result["document_id"], tier="local",
                            actor_id="act_canary", engine_name="chat")
        documents.append({**result, "filename": path.name})
    entities.propose_entities(store, actor_id="act_canary")
    store.conn.commit()

    yield {"store": store, "root": root, "documents": documents,
           "bundle": bundle}
    server.shutdown()
    store.close()


# -- ingest ------------------------------------------------------------------

def test_every_document_ingested_with_text(canary):
    store, documents = canary["store"], canary["documents"]
    assert len(documents) == len(list(CORPUS.glob("*.txt"))) == 6
    for document in documents:
        assert not document.get("duplicate"), document["filename"]
        assert ingest.has_text(store, document["document_id"])


def test_nothing_was_written_outside_the_storage_root(canary):
    """The upload route once left a second, unrecorded copy of every document
    in the system temp directory. Ingest itself must put originals in exactly
    one place, and the store must know where."""
    store, root = canary["store"], canary["root"]
    on_disk = {p.resolve() for p in (root / "storage").rglob("*") if p.is_file()}
    recorded = {Path(r["storage_path"]).resolve() for r in store.query(
        "SELECT storage_path FROM documents")}
    assert recorded == on_disk
    assert len(recorded) == 6


# -- classification ----------------------------------------------------------

def test_classification_succeeded_on_every_document(canary):
    """It failed on 88 of 88 documents across two corpora without anyone
    noticing, because a classification failure is caught per document so that
    one missing model cannot stop an ingest. Nothing counted it."""
    store = canary["store"]
    unclassified = store.query(
        "SELECT filename FROM documents WHERE doc_type IS NULL")
    assert unclassified == [], f"never classified: {unclassified}"
    failed = store.scalar(
        "SELECT COUNT(*) FROM llm_calls WHERE purpose = 'classify' "
        "AND error IS NOT NULL")
    assert failed == 0


def test_every_classified_value_is_in_the_bundles_vocabulary(canary):
    """`sector` was an open question until it produced thirteen spellings of one
    answer across forty-eight documents. A closed list is only closed if
    something checks."""
    store, bundle = canary["store"], canary["bundle"]
    allowed = {
        "doc_type": set(bundle_mod.document_types(bundle)),
        "sector": set(bundle_mod.sectors(bundle)),
        "jurisdiction": set(bundle_mod.jurisdictions(bundle)),
    }
    for column, vocabulary in allowed.items():
        answered = store.query(
            f"SELECT document_id, {column} AS value FROM documents")
        used = {r["value"] for r in answered if r["value"] is not None}
        assert used <= vocabulary, f"{column} outside its vocabulary: " \
                                   f"{sorted(used - vocabulary)}"
        # The other half of the same guard. `_in_vocabulary` drops an answer
        # that is not on the list, so a model that has stopped speaking the
        # vocabulary leaves the column NULL and the check above sees nothing
        # at all. Silence is the shape that fault actually has.
        silent = [r["document_id"] for r in answered if r["value"] is None]
        assert silent == [], (
            f"{column} was asked of {len(answered)} documents and answered "
            f"for {len(answered) - len(silent)}: {silent}")


# -- extraction --------------------------------------------------------------

def test_every_document_has_a_completed_extraction_run(canary):
    store = canary["store"]
    runs = store.query("SELECT document_id, status, error FROM extraction_runs")
    assert len(runs) == 6
    assert [r for r in runs if r["error"]] == []
    assert {r["status"] for r in runs} == {"succeeded"}


def test_the_bundle_fits_the_corpus(canary):
    """A pile of schema amendments means the ontology and the documents
    disagree. One or two is a finding; a corpus-worth is a broken bundle."""
    store = canary["store"]
    pending = store.query(
        "SELECT type_id, property_id FROM schema_amendments "
        "WHERE status = 'pending'")
    assert pending == [], f"the bundle did not fit: {pending}"


def test_every_excerpt_is_really_in_the_document_it_cites(canary):
    """Grounding is computed, never taken from the model. This is the assertion
    the whole alignment design exists to make true, checked over a whole run
    rather than one call."""
    store = canary["store"]
    ungrounded = lint_mod.ungrounded_quotations(store)
    assert ungrounded == [], f"{len(ungrounded)} excerpt(s) not in their source"


def test_excerpts_are_quoted_rather_than_paraphrased(canary):
    """`ungrounded_quotations` reports what aligns *not at all*, which is the
    right rule for a finding a reviewer must act on: a fuzzy match is a real
    passage recorded at a lower confidence, and reporting it would cry wolf on
    every rewrapped line.

    That leaves a gap only a whole run can show. A model that stops quoting and
    starts paraphrasing scatters its excerpts across `match_fuzzy` and `None`
    in some mixture nobody chose, and the corpus degrades without a single
    stage failing.

    Here the stub quotes verbatim by construction, so there is no slack to
    allow: every excerpt must land exactly, and anything else means the
    alignment path changed or the replies drifted off the corpus. Two earlier
    versions of this assertion could not see the fault it is named for -- one
    counted only `WHERE alignment IS NOT NULL`, so a paraphrasing model scored
    35 out of 35; the next allowed 80%, which six paraphrases out of
    forty-one excerpts sailed through.
    """
    store = canary["store"]
    drifted = store.query(
        "SELECT excerpt, alignment FROM provenance "
        "WHERE excerpt IS NOT NULL AND excerpt != '' "
        "AND (alignment IS NULL OR alignment != ?)", (align.MATCH_EXACT,))
    total = store.query(
        "SELECT COUNT(*) AS n FROM provenance "
        "WHERE excerpt IS NOT NULL AND excerpt != ''")[0]["n"]
    assert total > 0
    assert drifted == [], (
        f"{len(drifted)} of {total} excerpts did not match the document "
        "exactly, though every one of them is copied from it verbatim: "
        + "; ".join(f"{r['alignment'] or 'nowhere'}: {r['excerpt']!r}"
                    for r in drifted[:5]))


def test_a_named_instance_always_has_a_key_to_match_on(canary):
    """The null-column bug: a bundle that has just gained `naive_key` has it
    null on every row written before, and grouping on the column as read gave
    every one of those mentions the same key."""
    store, bundle = canary["store"], canary["bundle"]
    for type_id in bundle_mod.implementing_types(bundle, "Named"):
        table = bundle_mod.table_name(bundle_mod.object_type(bundle, type_id))
        blank = store.query(
            f'SELECT instance_id, name FROM "{table}" '
            "WHERE name IS NOT NULL AND name != '' "
            "AND (naive_key IS NULL OR naive_key = '')")
        assert blank == [], f"{type_id} rows with a name and no key: {blank}"


# -- the wiki ----------------------------------------------------------------

def test_no_page_swallows_a_whole_type(canary):
    """174 meetings landed on one page called "April through June 19, 2024",
    because they all shared an empty key. A false merge at the scale of a type
    is the worst outcome this store has a rule about, and it looks like success
    from every other angle: the pages exist, the mentions are linked."""
    store = canary["store"]
    by_type = {r["type_id"]: r["n"] for r in store.query(
        "SELECT e.type_id, COUNT(*) AS n FROM entity_mentions m "
        "JOIN entities e ON e.entity_id = m.entity_id "
        "WHERE m.unlinked_at IS NULL GROUP BY e.type_id")}
    biggest = store.query(
        "SELECT e.type_id, e.canonical_name, COUNT(*) AS n "
        "FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id "
        "WHERE m.unlinked_at IS NULL GROUP BY e.entity_id")
    for page in biggest:
        share = page["n"] / by_type[page["type_id"]]
        assert share <= 0.6, (
            f"{page['canonical_name']!r} holds {page['n']} of "
            f"{by_type[page['type_id']]} {page['type_id']} mentions "
            f"({share:.0%}) -- that is a whole type on one page")


def test_the_same_company_under_two_spellings_is_one_page(canary):
    """Ardmore appears as `Limited` and `Ltd` with one registration number;
    Halloran as `Inc.` and `Inc` with none. An identifier settles the first and
    the name key settles the second, and both are supposed to end up as one
    page rather than two."""
    store = canary["store"]
    pages = {r["canonical_name"]: r["entity_id"] for r in store.query(
        "SELECT canonical_name, entity_id FROM entities WHERE type_id = 'Company'")}
    ardmore = [name for name in pages if name.lower().startswith("ardmore")]
    halloran = [name for name in pages if name.lower().startswith("halloran")]
    assert len(ardmore) == 1, f"Ardmore split across {ardmore}"
    assert len(halloran) == 1, f"Halloran split across {halloran}"


def test_an_amendment_is_not_merged_into_what_it_amends(canary):
    """The last row of the table at the top of this file. An amendment shares
    its subject's name almost word for word -- `Services Agreement` against
    `Amendment No. 1 to the Services Agreement` -- and naming it the same thing
    is the easiest false merge in the corpus to make.

    A false merge is strictly worse than a false split: the amendment's
    EUR 310,000 would land on the agreement's page beside the EUR 250,000 it
    replaced, with nothing to say which is current.

    What actually holds them apart is `DocumentScoped` on `Contract` -- a
    filing gets its own page whatever it is called -- and the mutation run
    confirms that: renaming the amendment does not merge it, dropping the
    interface does. So this is a check on the bundle's shape reaching all the
    way through the pipeline, not on a name comparison.
    """
    store = canary["store"]
    filed = store.query(
        "SELECT c.reference, COUNT(DISTINCT m.entity_id) AS n_pages "
        "FROM entity_mentions m JOIN instances_Contract c "
        "  ON c.instance_id = m.instance_id "
        "WHERE m.unlinked_at IS NULL AND c.reference IN "
        "  ('HSE/2024/0117', 'HSE/2024/0117-A1') GROUP BY c.reference")
    assert {r["reference"] for r in filed} == {"HSE/2024/0117",
                                              "HSE/2024/0117-A1"}
    shared = store.query(
        "SELECT m.entity_id, COUNT(DISTINCT c.reference) AS n_references "
        "FROM entity_mentions m JOIN instances_Contract c "
        "  ON c.instance_id = m.instance_id "
        "WHERE m.unlinked_at IS NULL AND c.reference IN "
        "  ('HSE/2024/0117', 'HSE/2024/0117-A1') "
        "GROUP BY m.entity_id HAVING n_references > 1")
    assert shared == [], (
        "the amendment and the agreement it amends were filed as one page: "
        f"{shared}")


def test_a_person_recurring_across_documents_is_one_page(canary):
    store = canary["store"]
    peter = store.query(
        "SELECT e.entity_id, COUNT(DISTINCT m.document_id) AS n_documents "
        "FROM entities e JOIN entity_mentions m ON m.entity_id = e.entity_id "
        "WHERE e.type_id = 'Person' AND e.canonical_name LIKE 'Peter%' "
        "AND m.unlinked_at IS NULL GROUP BY e.entity_id")
    assert len(peter) == 1 and peter[0]["n_documents"] == 3


# -- the graph ---------------------------------------------------------------

def test_relations_reach_the_graph(canary):
    """`edges` was unreachable for a whole phase because no shipped engine ever
    returned a relationship, so the table, the normaliser and the writer were
    all correct and all dead."""
    store = canary["store"]
    assert store.scalar("SELECT COUNT(*) FROM edges") > 0
    topology = graph.topology(store)
    coverage = topology["coverage"]
    assert coverage["n_edges_total"] > 0
    assert coverage["projected_rate"] >= 0.5, (
        f"only {coverage['projected_rate']:.0%} of relations reached the "
        f"graph: {coverage['note']}")
    assert topology["counts"]["connected_entities"] > 0


def test_the_corpus_is_one_connected_world(canary):
    """Ardmore is a party to one contract and a subcontractor under another, so
    the two clusters must actually join. Two islands where the documents state
    a connection means the projection lost it."""
    store = canary["store"]
    components = graph.topology(store)["components"]
    assert components, "no components at all"
    largest = max(c["n_entities"] for c in components)
    assert largest >= 6, f"largest island is only {largest} pages"


# -- what the store says about itself ----------------------------------------

def test_lint_finds_nothing_it_calls_fabrication(canary):
    """Lint is allowed to find work to do -- unreviewed groupings, uncited
    pages. It is not allowed to find a quotation that is not in the document it
    cites, which is the one class that means the store is lying."""
    store = canary["store"]
    report = lint_mod.lint(store)
    fabricated = [f for f in report["findings"]
                  if f["check"] == "ungrounded_quotations"]
    assert fabricated == [], fabricated


def test_the_report_is_honest_about_having_no_reviewer(canary):
    """Nobody has reviewed anything here, and the number that matters must say
    so rather than reporting an accuracy computed from nothing."""
    store = canary["store"]
    report = quality.extraction_quality(store)
    assert report["overall"]["n_total"] > 0
    assert report["overall"]["n_reviewed"] == 0
    assert report["overall"]["accuracy"] is None


# -- the ontology survey, over the same corpus -------------------------------

def test_the_pattern_survey_finds_the_headers_and_invents_nothing(canary):
    """The header-block pass on a corpus that has header blocks. It must find
    the fields the documents actually declare and nothing else -- a survey that
    manufactures a schema is worse than one that finds none."""
    store = canary["store"]
    result = ontology.survey(store, actor_id="act_canary", sample=6)
    found = {c["property_id"] for c in result["candidates"]
             if c["kind"] == "property"}
    assert {"reference", "sector", "jurisdiction"} <= found
    for candidate in result["candidates"]:
        assert candidate["evidence"], candidate["type_id"]
        for item in candidate["evidence"]:
            page = store.scalar(
                "SELECT text FROM document_pages WHERE document_id = ?",
                (item["document_id"],))
            assert item["excerpt"] in page
