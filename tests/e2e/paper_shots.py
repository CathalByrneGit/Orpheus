"""The idea space, photographed: the same memo before and after a redaction.

Run by hand. It stands up a real Datasette with `datasette-paper` and both
Orpheus plugins, writes a brief citing a contested page, a checked fact, an
unchecked one and a document, shoots it, then redacts the document the brief
was built on and shoots it again.

The pair is the argument. Nothing in the prose changed between the two
pictures; every reference under it did, because a reference is resolved when it
is read rather than when it was written.

    python3 tests/e2e/paper_shots.py [port]

Needs `pip install 'orpheus[paper]' playwright` and a chromium.
"""

from __future__ import annotations

import glob
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from orpheus import datasette_config, entities as entities_mod          # noqa: E402
from orpheus import ingest as ingest_mod, tensions as tensions_mod      # noqa: E402
import orpheus.bundle as bundle_mod                                     # noqa: E402
from orpheus.store import connect                                       # noqa: E402
from orpheus.utils import naive_key                                     # noqa: E402

OUT = ROOT / "docs" / "images"

DOCUMENTS = [
    "This Agreement is between Halloran Instruments, Inc. and Kestrel Medical "
    "Group PLC. The term ends on 2024-01-31.",
    "Amendment No. 2 with Halloran Instruments Inc. extends the term to "
    "2026-12-31.",
]

#: Written as somebody actually would: a claim, and the evidence under it.
BRIEF = [
    ("## Does the Halloran term run to 2024 or 2026?", None),
    ("The page itself is contested — the two documents disagree, and neither "
     "reading has been settled:", "ENTITY"),
    ("The party name is the one thing here a person has actually checked:",
     "inst_1"),
    ("This one is still the machine talking:", "inst_2"),
    ("And the document the whole argument rests on:", "DOCUMENT"),
]


def build_store(work: Path) -> dict:
    db, storage = work / "orpheus.sqlite", work / "storage"
    store = connect(db)
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)

    documents = []
    for n, text in enumerate(DOCUMENTS, start=1):
        path = work / f"doc{n}.txt"
        path.write_text(text)
        documents.append(
            ingest_mod.ingest(store, path, storage_root=storage)["document_id"])

    # One confirmed, two not: the pill has to tell them apart on sight.
    rows = [("Halloran Instruments, Inc.", documents[0], "confirmed"),
            ("Kestrel Medical Group PLC", documents[0], "unconfirmed"),
            ("Halloran Instruments Inc.", documents[1], "unconfirmed")]
    for n, (name, document_id, status) in enumerate(rows, start=1):
        store.execute(
            "INSERT INTO instances_Company (instance_id, document_id, name, "
            "naive_key, source, confidence, status, created_at) "
            "VALUES (?,?,?,?,'ai_local',0.9,?,datetime('now'))",
            (f"inst_{n}", document_id, name, naive_key(name), status))
        store.execute(
            "INSERT INTO instance_index (instance_id, type_id, table_name, "
            "document_id, created_at) VALUES (?,'Company','instances_Company',"
            "?,datetime('now'))", (f"inst_{n}", document_id))

    entities_mod.propose_entities(store)
    entity_id = store.scalar(
        "SELECT entity_id FROM entities ORDER BY canonical_name")
    tensions_mod.raise_tension(
        store, kind="conflicting_value",
        summary="The two documents give different end dates.",
        sides=["inst_1", "inst_3"], scope="entity", subject_id=entity_id)
    store.close()

    config = datasette_config.build_config(bundle, storage_root=str(storage))
    (work / "datasette.yml").write_text(yaml.safe_dump(config))
    (work / "metadata.yml").write_text(
        yaml.safe_dump(datasette_config.build_metadata(bundle)))
    return {"db": db, "entity_id": entity_id, "document_id": documents[0]}


def serve(work: Path, db: Path, port: int):
    log = work / "server.log"
    handle = log.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "datasette", "serve", str(db),
         "--metadata", str(work / "metadata.yml"),
         "--config", str(work / "datasette.yml"),
         "--plugins-dir", str(ROOT / "plugins"),
         "--template-dir", str(ROOT / "templates"),
         "--internal", str(work / "internal.db"),
         "--port", str(port), "--root", "--secret", "paper"],
        stdout=handle, stderr=subprocess.STDOUT, cwd=ROOT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            urllib.request.urlopen(f"{base}/-/orpheus", timeout=2)
            break
        except Exception:                          # noqa: BLE001 - polling
            if process.poll() is not None:
                raise SystemExit(f"server exited; see {log}")
            time.sleep(0.5)
    token = re.search(r"token=([a-f0-9]+)", log.read_text())
    if not token:
        raise SystemExit(f"no sign-in token in {log}")
    return process, base, token.group(1)


def fence(ref: str) -> str:
    body = json.dumps({"ref": f"/-/orpheus/ref/{ref}", "mode": "card",
                       "config": {}})
    return "```paper-embed\n" + body + "\n```"


def write_brief(page, base: str, facts: dict) -> int:
    """Create the paper and append the brief, through paper's own API."""
    blocks = []
    for prose, ref in BRIEF:
        blocks.append(prose)
        if ref == "ENTITY":
            blocks.append(fence(facts["entity_id"]))
        elif ref == "DOCUMENT":
            blocks.append(fence(facts["document_id"]))
        elif ref:
            blocks.append(fence(ref))

    created = page.request.post(
        f"{base}/-/paper/api/docs",
        data={"name": "Does the Halloran term run to 2024 or 2026?"})
    doc_id = created.json()["id"]
    page.request.post(f"{base}/-/paper/api/docs/{doc_id}/append",
                      data={"content": "\n\n".join(blocks),
                            "content_type": "markdown"})
    return doc_id


def shoot(page, base: str, doc_id: int, name: str) -> None:
    page.goto(f"{base}/-/paper/doc/{doc_id}", wait_until="networkidle")
    # The cards each fetch their own answer; wait for the last one to settle
    # rather than for a fixed time.
    page.wait_for_timeout(4000)
    OUT.mkdir(parents=True, exist_ok=True)
    page.locator(".ProseMirror").screenshot(path=str(OUT / name))
    print(f"  wrote {OUT / name}")
    for card in page.locator(".orpheus-ref-mount").all():
        print("   ·", " | ".join(x for x in card.inner_text().split("\n") if x))


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8096
    work = Path(tempfile.mkdtemp())
    print(f"store in {work}")
    facts = build_store(work)
    process, base, token = serve(work, facts["db"], port)
    chrome = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")[0]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=chrome,
                                         args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1000, "height": 1400},
                                          device_scale_factor=2)
            page = context.new_page()
            page.goto(f"{base}/-/auth-token?token={token}")

            doc_id = write_brief(page, base, facts)
            print("before the redaction:")
            shoot(page, base, doc_id, "idea-space-before.png")

            page.request.post(
                f"{base}/-/orpheus/api/documents/{facts['document_id']}/redact",
                data={"note": "subject access request", "confirm": True})

            print("after it:")
            shoot(page, base, doc_id, "idea-space-after.png")
            browser.close()
    finally:
        process.terminate()
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
