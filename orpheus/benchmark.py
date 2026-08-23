"""Extraction measured against a labelled corpus, rather than against itself.

`quality` measures agreement with the people reviewing this deployment's own
documents, which is the number that matters but is also circular: it cannot tell
you whether the reviewers and the model are wrong together. CUAD is the outside
check — 510 commercial contracts with expert-annotated clause spans, in
SQuAD format.

Two things are deliberate. The category vocabulary is derived from the data file
rather than hardcoded, so a different CUAD release does not silently score
against a stale list. And a category with no answers is kept as a **true
negative** rather than dropped: `is_impossible` means the clause is genuinely
absent from that contract, and discarding those makes precision unmeasurable.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .extract import extract
from .ingest import ingest
from .store import Store
from .utils import OrpheusError, from_json, new_id

MAP_PATH = Path(__file__).parent / "benchmarks" / "cuad-clause-map.json"

_QUOTED = re.compile(r'"([^"]+)"')
_SPACE = re.compile(r"\s+")


def cuad_category(question: str) -> str:
    """Recover the category from a CUAD question.

    The questions are long natural-language prompts that name the category in
    quotes: `Highlight the parts ... related to "Governing Law" ...`.
    """
    match = _QUOTED.search(question or "")
    return match.group(1) if match else (question or "").strip()


def load_cuad(path: str | Path) -> dict:
    """Read a CUAD SQuAD-format file into contracts, labels and categories."""
    path = Path(path)
    if not path.exists():
        raise OrpheusError(f"No CUAD file at {path}.")
    raw = json.loads(path.read_text())
    entries = raw.get("data", raw) if isinstance(raw, dict) else raw
    if not entries:
        raise OrpheusError(f"{path} contains no entries.")

    contracts, labels = [], []
    for entry in entries:
        title = entry.get("title") or new_id("cuad")
        for paragraph in entry.get("paragraphs", []):
            contracts.append({"title": title,
                              "context": paragraph.get("context", "")})
            for qa in paragraph.get("qas", []):
                category = cuad_category(qa.get("question", ""))
                answers = qa.get("answers") or []
                if not answers:
                    # A real label: the clause is absent from this contract.
                    # Dropping these would make precision unmeasurable.
                    labels.append({"title": title, "category": category,
                                   "text": None, "answer_start": None,
                                   "present": False})
                    continue
                for answer in answers:
                    labels.append({"title": title, "category": category,
                                   "text": answer.get("text", ""),
                                   "answer_start": answer.get("answer_start"),
                                   "present": True})

    return {"contracts": contracts, "labels": labels,
            # Derived from the data, never hardcoded: a different release must
            # not silently score against a stale vocabulary.
            "categories": sorted({label["category"] for label in labels})}


def load_map(path: str | Path = MAP_PATH) -> dict:
    """CUAD category → this bundle's clause_type.

    The file wraps the mapping in provenance — where CUAD came from, its
    licence, and a note that the mapping is seeded rather than complete — so
    the mapping itself lives under a key.
    """
    path = Path(path)
    if not path.exists():
        return {}
    loaded = from_json(path.read_text()) or {}
    return loaded.get("mapping", loaded) if isinstance(loaded, dict) else {}


def _normalise(text: str | None) -> str:
    return _SPACE.sub(" ", (text or "").strip()).lower()


def score_document(store: Store, document_id: str, labels: list[dict],
                   mapping: dict) -> list[dict]:
    """Score one extracted document against its CUAD labels."""
    extracted = store.query(
        "SELECT clause_type, text FROM instances_Clause "
        "WHERE document_id = ? AND status != 'rejected'", (document_id,))
    for row in extracted:
        row["norm"] = _normalise(row["text"])

    rows = []
    for category in sorted({label["category"] for label in labels}):
        present = [l for l in labels if l["category"] == category and l["present"]]
        # An unmapped category is a reportable gap in the benchmark's
        # configuration, not a reason to stop the run.
        mapped = mapping.get(category)
        if not mapped:
            rows.append({"category": category, "clause_type": None,
                         "n_labelled": len(present), "n_found": None,
                         "n_extracted": None, "mapped": False})
            continue

        candidates = [r["norm"] for r in extracted
                      if r["clause_type"] == mapped and r["norm"]]
        found = 0
        for label in present:
            span = _normalise(label["text"])
            if not span:
                continue
            # Either containment counts: an extraction may be a clause
            # containing the labelled span, or a span inside a longer clause.
            if any(span in candidate or candidate in span for candidate in candidates):
                found += 1

        rows.append({"category": category, "clause_type": mapped,
                     "n_labelled": len(present), "n_found": found,
                     "n_extracted": len(candidates), "mapped": True})
    return rows


def benchmark_extraction(store: Store, cuad: dict, tier: str = "local",
                         limit: int = 5, actor_id: str | None = None,
                         opt_in: bool = False,
                         storage_root: str | Path = "storage/benchmark",
                         mapping: dict | None = None,
                         engine_name: str | None = None) -> dict:
    """Ingest, extract and score the first `limit` contracts."""
    store.assert_writable()
    mapping = mapping if mapping is not None else load_map()
    contracts = cuad["contracts"][:limit]
    if not contracts:
        raise OrpheusError("No contracts to benchmark.")

    unmapped = sorted(set(cuad["categories"]) - set(mapping))
    per_contract: list[dict] = []
    failures: list[dict] = []

    with tempfile.TemporaryDirectory() as tmp:
        for contract in contracts:
            title = contract["title"]
            safe = re.sub(r"[^A-Za-z0-9]+", "-", title)[:80] or "contract"
            path = Path(tmp) / f"{new_id('bench')}.txt"
            path.write_text(contract["context"])

            ingested = ingest(store, path, actor_id=actor_id,
                              storage_root=storage_root, filename=f"{safe}.txt")
            if ingested["duplicate"]:
                continue
            try:
                extract(store, ingested["document_id"], tier=tier,
                        actor_id=actor_id, opt_in=opt_in, engine_name=engine_name)
            except Exception as exc:
                # One contract that will not extract should not end the run;
                # the failures are reported alongside the scores.
                failures.append({"title": title, "error": str(exc)})
                continue

            scored = score_document(
                store, ingested["document_id"],
                [l for l in cuad["labels"] if l["title"] == title], mapping)
            for row in scored:
                row["title"] = title
            per_contract.extend(scored)

    if not per_contract:
        return {"n_contracts": 0, "by_category": [], "unmapped_categories": unmapped,
                "failures": failures,
                "note": "No contract was extracted successfully."}

    by_category: dict[str, dict] = {}
    for row in per_contract:
        entry = by_category.setdefault(row["category"], {
            "category": row["category"], "clause_type": row["clause_type"],
            "mapped": row["mapped"], "n_labelled": 0, "n_found": 0,
            "n_extracted": 0})
        entry["n_labelled"] += row["n_labelled"]
        entry["n_found"] += row["n_found"] or 0
        entry["n_extracted"] += row["n_extracted"] or 0

    for entry in by_category.values():
        entry["recall"] = (round(entry["n_found"] / entry["n_labelled"], 3)
                           if entry["mapped"] and entry["n_labelled"] else None)

    scored_categories = [e for e in by_category.values() if e["recall"] is not None]
    overall = (round(sum(e["n_found"] for e in scored_categories)
                     / sum(e["n_labelled"] for e in scored_categories), 3)
               if sum(e["n_labelled"] for e in scored_categories) else None)

    return {
        "n_contracts": len({row["title"] for row in per_contract}),
        "tier": tier,
        "overall_recall": overall,
        "by_category": sorted(by_category.values(),
                              key=lambda e: (e["recall"] is None, e["recall"])),
        "per_contract": per_contract,
        # Named rather than silently skipped: an unmapped category is a gap in
        # the benchmark's configuration and it changes what the recall means.
        "unmapped_categories": unmapped,
        "failures": failures,
        "caveat": ("Recall only. Precision needs a judgement about extractions "
                   "CUAD does not label, which this does not attempt."),
    }
