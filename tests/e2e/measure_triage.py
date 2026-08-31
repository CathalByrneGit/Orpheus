"""Does the ranking actually get to a verdict faster than reviewing in order?

Run by hand from the repository root, not by pytest -- it builds and discards
several stores and takes about half a minute:

    python tests/e2e/measure_triage.py

`tests/test_triage.py` holds the ranking to its rules. This asks the question
those rules exist to answer, which is not the same thing: given a corpus shaped
the way real ones are, how much review does each ordering need before
`confidence_calibration` stops saying `insufficient_evidence`?

Two runs over identical stores. One takes the queue `triage` proposes; the
other takes them in the order the extractor produced, which is what a reviewer
with no ranking does. The reviewer rejects more at lower confidence, because a
reviewer who confirms everything produces a flat line at every level -- an
honest verdict that tells you nothing about ordering.

Measured over five seeds: 10 reviews every time for the ranked queue, 19-28 in
extractor order. The ranked figure does not vary because it is not a heuristic
-- it is `min_reviewed` twice over.
"""
import random
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from orpheus import bundle as bundle_mod, extract as extract_mod, ingest, quality, review
from orpheus.population import set_populator
from orpheus.store import connect

CORPUS = ROOT / "tests/canary/corpus"
LEVELS = [1.0, 0.9, 0.7, 0.5, 0.2]


def build(seed):
    """Six documents, extracted with a spread of confidence levels.

    The spread is skewed on purpose: most extractions land `explicit`, a few at
    each of the others. That is what a real corpus looks like -- a model that
    quotes well quotes well most of the time -- and it is exactly the shape
    that makes reviewing in order useless for calibration.
    """
    rng = random.Random(seed)
    work = Path(tempfile.mkdtemp())
    store = connect(work / "o.sqlite")
    b = bundle_mod.load(); bundle_mod.register(store, b); bundle_mod.apply_schema(store, b)
    store.insert("actors", {"actor_id": "act_r", "display_name": "R", "is_admin": 1,
                            "created_at": "2026-01-01T00:00:00Z"})

    def populate(*, text, **kwargs):
        out = []
        for i, line in enumerate(l for l in text.splitlines() if len(l.strip()) > 25):
            out.append({"type": "Clause", "excerpt": line.strip(),
                        "properties": {"text": line.strip()[:80],
                                       "clause_number": str(i + 1)}})
        return {"extractions": out}

    set_populator(populate)
    for path in sorted(CORPUS.glob("*.txt")):
        r = ingest.ingest(store, path, actor_id="act_r", storage_root=work / "storage")
        extract_mod.extract(store, r["document_id"], tier="local", actor_id="act_r")
    set_populator(None)

    # Alignment made every excerpt exact, so spread the levels by hand -- the
    # question here is the ranking, not how confidence is computed. Skewed the
    # way a real corpus is: most extractions land `explicit`, a thin tail
    # everywhere else. That skew is exactly what makes reviewing in order
    # useless for calibration.
    rows = [r["instance_id"] for r in store.query(
        "SELECT instance_id FROM instances_Clause ORDER BY instance_id")]
    rng.shuffle(rows)
    plan = {}
    tail = rows[:24]
    for n, instance_id in enumerate(tail):
        plan[instance_id] = LEVELS[1 + n % 4]
    for instance_id in rows[24:]:
        plan[instance_id] = LEVELS[0]
    for instance_id, level in plan.items():
        store.execute("UPDATE provenance SET confidence = ? WHERE instance_id = ?",
                      (level, instance_id))
        store.execute("UPDATE instances_Clause SET confidence = ? WHERE instance_id = ?",
                      (level, instance_id))
    return store, work


def verdict(store):
    return quality.confidence_calibration(store)["verdict"]


def run(order_fn, label, seed=1):
    rng = random.Random(99)
    store, work = build(seed)
    total = store.scalar("SELECT COUNT(*) FROM instances_Clause")
    spread = {}
    for r in store.query("SELECT confidence, COUNT(*) n FROM provenance GROUP BY confidence"):
        spread[r["confidence"]] = r["n"]
    reviewed = 0
    while verdict(store) == "insufficient_evidence" and reviewed < total:
        nxt = order_fn(store)
        if not nxt:
            break
        # A reviewer who confirms everything produces a flat line at every
        # level, which is the honest verdict for that and tells us nothing
        # about ranking. Real review rejects more at lower confidence -- that
        # is the whole premise of the rubric -- so mirror it.
        level = store.scalar("SELECT confidence FROM provenance WHERE "
                             "instance_id = ?", (nxt,))
        if rng.random() > level:
            review.reject_instance(store, nxt, actor_id="act_r", note="wrong")
        else:
            review.confirm_instance(store, nxt, actor_id="act_r", note="checked")
        reviewed += 1
    result = (reviewed, verdict(store))
    shutil.rmtree(work, ignore_errors=True)
    return result, total, spread


def by_triage(store):
    q = review.triage(store, limit=1)["queue"]
    return q[0]["instance_id"] if q else None


def in_order(store):
    row = store.one("SELECT instance_id FROM instances_Clause "
                    "WHERE status = 'unconfirmed' ORDER BY created_at, instance_id")
    return row["instance_id"] if row else None


for seed in (1, 2, 3, 4, 5):
    (t_n, t_v), total, spread = run(by_triage, "triage", seed=seed)
    (o_n, o_v), _, _ = run(in_order, "in order", seed=seed)
    if seed == 1:
        print(f"corpus: {total} extractions, "
              f"levels {dict(sorted(spread.items(), reverse=True))}\n")
        print(f"{'seed':>5}  {'ranked queue':>22}  {'in extractor order':>22}")
    print(f"{seed:>5}  {t_n:>10} reviews -> {t_v:<8}  "
          f"{o_n:>10} reviews -> {o_v:<8}")
