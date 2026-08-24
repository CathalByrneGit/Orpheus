"""The store, projected out as markdown.

Two properties are defended. The first is the invariant, carried across the
boundary: every claim written out carries its source, and a page with no source
is not written at all — an export that quietly dropped citations would read
beautifully and be worthless.

The second is that the conflicts survive the trip. A markdown bundle is where a
smoothing pass is easiest to hide, because the output is prose and prose is
supposed to flow.
"""

from __future__ import annotations

import re

import pytest

import orpheus.bundle as bundle_mod
from orpheus.entities import (confirm_link, create_entity, describe_entity,
                              link_mention)
from orpheus.export_md import export, frontmatter, slug
from orpheus.tensions import accept_tension, propose_tensions
from orpheus.utils import naive_key

MENTIONS = [
    ("i1", "doc_1", "Ardmore Digital Ltd", "12 Ushers Quay, Dublin 8"),
    ("i2", "doc_2", "Ardmore Digital Limited", "4 Sandwith Street, Dublin 2"),
]


@pytest.fixture
def corpus(store):
    store.insert("actors", {"actor_id": "act_a", "display_name": "Ada",
                            "is_admin": 1, "created_at": "2026-01-01T00:00:00Z"})
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle, actor_id="act_a")
    bundle_mod.apply_schema(store, bundle)
    for document_id in ("doc_1", "doc_2"):
        store.execute(
            "INSERT INTO documents (document_id, filename, file_hash, byte_size,"
            " n_pages, date_added, created_by, visibility, review_status,"
            " doc_type) VALUES (?,?,?,100,2,datetime('now'),'act_a','private',"
            "'unreviewed','Contract')",
            (document_id, f"{document_id}.pdf", document_id))
    for instance_id, document_id, name, address in MENTIONS:
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name,"
            " naive_key, address, source, confidence, status, created_at)"
            " VALUES (?,?,?,?,?,'ai_local',0.9,'confirmed',datetime('now'))",
            (instance_id, document_id, name, naive_key(name), address))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name,"
            " document_id, created_at) VALUES (?,'Company','instances_Company',?,"
            " datetime('now'))", (instance_id, document_id))
        store.execute(
            "INSERT INTO provenance (provenance_id, instance_id, document_id,"
            " source_label, page_no, excerpt, confidence, created_at, source,"
            " alignment, char_start, char_end)"
            " VALUES (?,?,?,?,2,?,0.9,datetime('now'),'ai_local','match_exact',10,40)",
            (f"p_{instance_id}", instance_id, document_id,
             f"{document_id}.pdf", f"registered office of {name} at {address}"))
    store.conn.commit()
    return store


@pytest.fixture
def bundle_dir(corpus, tmp_path):
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    for instance_id in ("i1", "i2"):
        link_mention(corpus, entity_id, instance_id, actor_id="act_a",
                     basis="naive_key")
        confirm_link(corpus, entity_id, instance_id, actor_id="act_a")
    describe_entity(corpus, entity_id, "The parent company, per the 2024 filing.",
                    actor_id="act_a")
    result = propose_tensions(corpus, actor_id="act_a")
    accept_tension(corpus, result["raised"][0], "act_a",
                   note="moved offices in 2024")
    corpus.conn.commit()
    out = tmp_path / "bundle"
    written = export(corpus, out)
    return corpus, out, written, entity_id


def read(path):
    return path.read_text(encoding="utf-8")


# -- the shape ---------------------------------------------------------------

def test_the_bundle_has_an_index_a_log_and_a_file_per_concept(bundle_dir):
    _, out, written, _ = bundle_dir
    assert (out / "index.md").exists()
    assert (out / "log.md").exists()
    assert (out / "entities" / "ardmore-digital-ltd.md").exists()
    assert (out / "documents" / "doc-1-pdf.md").exists()
    assert written["n_entities"] == 1 and written["n_documents"] == 2


def test_every_file_opens_with_a_typed_frontmatter_block(bundle_dir):
    # `type` is the only field OKF requires, so it is the one that must be there.
    _, out, _, _ = bundle_dir
    for path in out.rglob("*.md"):
        text = read(path)
        assert text.startswith("---\n"), path
        block = text.split("---")[1]
        assert re.search(r"^type: ", block, re.M), path


def test_links_between_files_resolve(bundle_dir):
    _, out, _, _ = bundle_dir
    for path in out.rglob("*.md"):
        for target in re.findall(r"\]\(([^)]+\.md)\)", read(path)):
            assert (path.parent / target).resolve().exists(), f"{path} -> {target}"


def test_a_name_that_slugs_the_same_gets_a_stable_distinct_file(corpus, tmp_path):
    # A counter would renumber every later page when one is inserted, so a
    # re-export would rewrite files whose content had not changed.
    for name in ("Ardmore Digital Ltd", "ardmore digital ltd"):
        entity_id = create_entity(corpus, "Company", name, actor_id="act_a")
        link_mention(corpus, entity_id, "i1" if name.istitle() else "i2",
                     actor_id="act_a", basis="human")
    corpus.conn.commit()
    export(corpus, tmp_path / "b")
    names = {p.name for p in (tmp_path / "b" / "entities").iterdir()}
    assert "ardmore-digital-ltd.md" in names
    suffixed = names - {"ardmore-digital-ltd.md"}
    assert len(suffixed) == 1
    # Suffixed with the row's own id, so the file a page lands in does not
    # depend on what else happens to be in the store.
    assert suffixed.pop().startswith("ardmore-digital-ltd-")


# -- the invariant crosses the boundary --------------------------------------

def test_every_source_line_carries_its_excerpt(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "entities" / "ardmore-digital-ltd.md")
    assert "doc_1.pdf" in text and "p. 2" in text
    assert "registered office of Ardmore Digital Ltd" in text
    assert "registered office of Ardmore Digital Limited" in text


def test_a_page_with_no_source_is_not_written(corpus, tmp_path):
    create_entity(corpus, "Company", "Invented Holdings", actor_id="act_a",
                  description="written from memory")
    corpus.conn.commit()
    written = export(corpus, tmp_path / "b")
    assert not (tmp_path / "b" / "entities" / "invented-holdings.md").exists()
    assert [s["name"] for s in written["skipped"]] == ["Invented Holdings"]
    # Refused, and said so -- silently dropping it would be the same failure
    # in the other direction.
    assert "Invented Holdings" in read(tmp_path / "b" / "index.md")
    assert "Not exported" in read(tmp_path / "b" / "index.md")


def test_the_index_is_titled_for_the_bundle_it_describes(bundle_dir):
    # ontologySpecR keys are camelCase and the name lives under `metadata`.
    # Read the wrong ones and every export is titled the same whatever domain
    # it describes -- in the one artefact meant to leave here.
    _, out, _, _ = bundle_dir
    text = read(out / "index.md")
    assert 'title: "Core contract ontology"' in text
    assert 'orpheus_bundle: "contract-core"' in text
    assert "# Core contract ontology" in text


def test_the_index_states_that_the_excerpts_are_immutable(bundle_dir):
    _, out, _, _ = bundle_dir
    assert "immutable" in read(out / "index.md")


# -- conflicts survive the trip ----------------------------------------------

def test_a_verified_conflict_is_written_as_a_tension(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "entities" / "ardmore-digital-ltd.md")
    assert "> **Tension**:" in text
    # Above the facts it is about, not below them: a reader who scrolls past
    # two contradictory rows has already been misled.
    assert text.index("Where the sources disagree") < text.index("What the sources say")


def test_both_sides_of_a_conflict_are_quoted(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "entities" / "ardmore-digital-ltd.md")
    assert "12 Ushers Quay, Dublin 8" in text
    assert "4 Sandwith Street, Dublin 2" in text


def test_the_contested_property_is_marked_in_the_table(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "entities" / "ardmore-digital-ltd.md")
    row = [l for l in text.split("\n") if l.startswith("| address")]
    assert row and "⚠︎" in row[0]


def test_the_index_lists_where_the_sources_disagree(bundle_dir):
    _, out, _, _ = bundle_dir
    assert "Where the sources disagree" in read(out / "index.md")


# -- what a person wrote is kept apart ---------------------------------------

def test_a_human_note_is_marked_as_not_from_a_document(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "entities" / "ardmore-digital-ltd.md")
    assert "> **Context**: The parent company, per the 2024 filing." in text


def test_an_invented_quotation_is_not_reported_as_low_confidence(corpus, tmp_path):
    # Two different failures. A cautious model and a fabricating one must not
    # read the same in the output.
    corpus.execute("UPDATE provenance SET alignment = NULL WHERE instance_id = 'i1'")
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a", basis="human")
    corpus.conn.commit()
    export(corpus, tmp_path / "b")
    text = read(tmp_path / "b" / "entities" / "ardmore-digital-ltd.md")
    assert "not found in the document it cites" in text


def test_proposals_are_never_mixed_in_with_confirmed_sources(corpus, tmp_path):
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a", basis="naive_key")
    link_mention(corpus, entity_id, "i2", actor_id="act_a", basis="naive_key")
    confirm_link(corpus, entity_id, "i1", actor_id="act_a")
    corpus.conn.commit()
    export(corpus, tmp_path / "b")
    text = read(tmp_path / "b" / "entities" / "ardmore-digital-ltd.md")
    assert "Proposed, not yet checked" in text
    assert text.index("doc_1.pdf") < text.index("Proposed, not yet checked")
    assert text.index("Proposed, not yet checked") < text.index("doc_2.pdf")


def test_confirmed_only_leaves_the_proposals_out_entirely(corpus, tmp_path):
    entity_id = create_entity(corpus, "Company", "Ardmore Digital Ltd",
                              actor_id="act_a")
    link_mention(corpus, entity_id, "i1", actor_id="act_a", basis="naive_key")
    link_mention(corpus, entity_id, "i2", actor_id="act_a", basis="naive_key")
    confirm_link(corpus, entity_id, "i1", actor_id="act_a")
    corpus.conn.commit()
    export(corpus, tmp_path / "b", confirmed_only=True)
    text = read(tmp_path / "b" / "entities" / "ardmore-digital-ltd.md")
    assert "doc_2.pdf" not in text
    assert "everything here was checked by a person" in \
        read(tmp_path / "b" / "index.md").replace("\n", " ")


# -- documents are first-class -----------------------------------------------

def test_a_document_page_lists_what_was_read_from_it(bundle_dir):
    _, out, _, _ = bundle_dir
    text = read(out / "documents" / "doc-1-pdf.md")
    # Every scalar is quoted, so a filename containing a colon cannot produce
    # frontmatter the reader refuses to parse.
    assert 'type: "source"' in text
    assert "Ardmore Digital Ltd" in text


def test_a_document_contributing_nothing_says_so_as_a_gap(corpus, tmp_path):
    corpus.conn.commit()
    export(corpus, tmp_path / "b")
    text = read(tmp_path / "b" / "documents" / "doc-1-pdf.md")
    assert "a gap, not an omission" in text


def test_the_log_is_newest_first_and_ordered_by_sequence(bundle_dir):
    _, out, _, _ = bundle_dir
    rows = [l for l in read(out / "log.md").split("\n")
            if l.startswith("| ") and l[2].isdigit()]
    seqs = [int(l.split("|")[1].strip()) for l in rows]
    assert seqs == sorted(seqs, reverse=True)


# -- small pieces ------------------------------------------------------------

def test_a_title_with_a_colon_does_not_produce_broken_frontmatter(bundle_dir):
    block = frontmatter({"type": "Company", "title": "Ardmore: a study"})
    assert '"Ardmore: a study"' in block


def test_slug_is_kebab_case_and_bounded():
    assert slug("Ardmore Digital, Ltd.") == "ardmore-digital-ltd"
    assert slug("") == "untitled"
    assert len(slug("x" * 200)) <= 60
