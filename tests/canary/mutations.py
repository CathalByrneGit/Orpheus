"""Does the canary bite? Inject each historical bug and expect it to fail.

Run by hand, not by pytest -- it edits files in the working tree and restores
them afterwards, which is not something to do inside a test run:

    python tests/canary/mutations.py

A canary that cannot fail is decoration. Every fault below is one that really
happened in this project, reduced to the smallest edit that reproduces its
shape, and the selector names the assertion that has to catch it. Two of these
were MISSED the first time this was run, and both misses were in the canary
rather than in the code:

- Paraphrased excerpts align to `None`, not `match_fuzzy`, and the assertion
  computed its ratio over `WHERE alignment IS NOT NULL` -- so it scored a
  paraphrasing model 35 out of 35 and passed.
- `classify._in_vocabulary` drops an out-of-vocabulary answer to NULL, so
  removing the guard changes nothing on its own: the canary's own data is
  valid. A model that stops speaking the vocabulary shows up as silence, which
  is what the assertion now checks for as well.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[2]
CANARY = ROOT / "tests/test_canary.py"
REPLIES = ROOT / "tests/canary/replies.py"
CLASSIFY = ROOT / "orpheus/classify.py"
BUNDLE = ROOT / "orpheus/bundles/contract-core-0.5.0.json"

GUARD = ("        if text.casefold() == candidate.casefold():\n"
         "            return candidate\n"
         "    return None")

# Excerpts only. A property *value* need not be verbatim -- it is the model's
# reading of a passage, and the excerpt beside it is what carries the
# grounding -- so paraphrasing one is a different fault, caught by lint.
PARAPHRASE = [
    ("Nuala Ryan, Managing Director", "Nuala Ryan the Managing Director"),
    ("Peter Halloran, Chief Executive", "Peter Halloran the Chief Executive"),
    ("Peter Halloran, Director, Kestrel Medical Group PLC",
     "Peter Halloran a Director of Kestrel Medical Group PLC"),
    ("CALL-OFF CONTRACT UNDER FRAMEWORK OGP/2022/0311",
     "a CALL-OFF CONTRACT UNDER the FRAMEWORK numbered OGP/2022/0311"),
]

OUT_OF_VOCABULARY = [("\"sector\": \"health\"", "\"sector\": \"Health Services\"")]

# (fault, [(file, find, replace), ...], the assertion that must fail)
MUTATIONS = [
    ("the null naive_key that merged 174 meetings onto one page",
     [(CANARY,
       '    entities.propose_entities(store, actor_id="act_canary")',
       '    store.execute("UPDATE instances_Company SET naive_key = NULL")\n'
       '    entities.propose_entities(store, actor_id="act_canary")')],
     "no_page_swallows or named_instance_always_has_a_key"),

    ("classification failing quietly on every document",
     [(CANARY,
       '        classify.classify(store, result["document_id"], actor_id="act_canary")',
       '        try:\n'
       '            classify.classify(store, result["document_id"],\n'
       '                              actor_id="act_canary", engine="anthropic")\n'
       '        except Exception:\n'
       '            pass')],
     "classification_succeeded"),

    ("a model that has stopped speaking the vocabulary",
     [(REPLIES, *OUT_OF_VOCABULARY[0])],
     "classified_value_is_in_the_bundles"),

    ("the vocabulary guard removed, so the bad answer lands in the column",
     [(CLASSIFY, GUARD, GUARD.replace("return None", "return text")),
      (REPLIES, *OUT_OF_VOCABULARY[0])],
     "classified_value_is_in_the_bundles"),

    ("an excerpt that is not in the document it cites",
     [(REPLIES, '"excerpt": "Kestrel Medical Group PLC",',
       '"excerpt": "Bergamot Holdings of Vienna",')],
     "really_in_the_document"),

    ("a model that paraphrases instead of quoting",
     [(REPLIES, a, b) for a, b in PARAPHRASE],
     "quoted_rather_than_paraphrased"),

    # Naming the amendment after the agreement is *not* enough to merge them:
    # `Contract` is DocumentScoped, so a filing gets its own page whatever it
    # is called. Dropping that interface is the edit that reproduces the fault,
    # which is also the honest statement of what the assertion guards.
    ("Contract no longer scoped to the document it was read from",
     [(BUNDLE, '"Reviewable",\n        "Named",\n        "DocumentScoped"',
       '"Reviewable",\n        "Named"'),
      (REPLIES, '"name": "Amendment No. 1 to the Services Agreement",',
       '"name": "Services Agreement",')],
     "amendment_is_not_merged"),

    ("a relation that never reaches the graph",
     [(REPLIES, '"link_type_id": "subcontracts_to",',
       '"link_type_id": "not_a_declared_link",')],
     "relations_reach_the_graph or one_connected_world or bundle_fits"),
]


def main() -> int:
    original = {p: p.read_text() for p in (CANARY, REPLIES, CLASSIFY,
                                           BUNDLE)}
    print(f"{'injected fault':62}{'canary':>8}")
    bitten = True
    for fault, edits, selector in MUTATIONS:
        pending = dict(original)
        for path, find, replace in edits:
            text = pending[path]
            assert find in text, f"mutation anchor missing: {fault} / {find[:40]}"
            pending[path] = text.replace(find, replace)
        for path, text in pending.items():
            path.write_text(text)
        try:
            run = subprocess.run(
                [sys.executable, "-m", "pytest", str(CANARY), "-q",
                 "-k", selector, "-p", "no:randomly"],
                capture_output=True, text=True, cwd=ROOT)
        finally:
            for path, text in original.items():
                path.write_text(text)
        caught = run.returncode != 0
        bitten &= caught
        print(f"{fault:62}{'CAUGHT' if caught else 'MISSED':>8}")
        if not caught:
            print(textwrap.indent(run.stdout[-800:], "      "))
    return 0 if bitten else 1


if __name__ == "__main__":
    sys.exit(main())
