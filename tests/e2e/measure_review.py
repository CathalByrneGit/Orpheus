"""How long it takes to review, the old way and the new one.

Run by hand. It is the justification for the review queue existing, and it is
deliberately a measurement rather than an argument: the claim being tested is
that reviewing through the document page is slow enough to stop people, and a
claim like that is either timed or it is a feeling.

Two arms, each against its own freshly built, identical store:

**document** -- what a reviewer does today. Open the document page, find the
row, press Confirm. The form posts and redirects back to the document page, so
every decision costs a full page load of every row on that document.

**queue** -- `/-/orpheus/review/queue`. One page load, then a keypress per
decision.

Both confirm the same number of findings and are checked afterwards against the
store, because a fast arm that did not actually write anything would be the
easiest possible way to win this.

    python3 tests/e2e/measure_review.py [n] [port]

Needs `pip install 'orpheus[dev]' playwright pyyaml` and a chromium.
"""

from __future__ import annotations

import glob
import re
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import orpheus.bundle as bundle_mod                                  # noqa: E402
from orpheus import datasette_config, ingest as ingest_mod           # noqa: E402
from orpheus.store import connect                                    # noqa: E402
from orpheus.utils import naive_key                                  # noqa: E402

COMPANIES = ["Halloran Instruments, Inc.", "Kestrel Medical Group PLC",
             "Ardmore Digital Ltd", "Brennan Civil Engineering",
             "Sionna Analytics Limited", "Tramore Logistics plc"]
#: Four rubric levels, cycled, so the ranking has something to rank.
LEVELS = [1.0, 0.9, 0.7, 0.5]
DOCUMENTS = 8
PER_DOCUMENT = 5


def build_store(work: Path) -> Path:
    """Eight documents, forty findings, nothing reviewed."""
    db, storage = work / "orpheus.sqlite", work / "storage"
    store = connect(db)
    bundle = bundle_mod.load()
    bundle_mod.register(store, bundle)
    bundle_mod.apply_schema(store, bundle)

    n = 0
    for i in range(1, DOCUMENTS + 1):
        path = work / f"contract-{i:02d}.txt"
        path.write_text(
            f"AGREEMENT {i}\n\nThis Agreement is made between "
            f"{COMPANIES[i % len(COMPANIES)]} and "
            f"{COMPANIES[(i + 2) % len(COMPANIES)]}.")
        document_id = ingest_mod.ingest(
            store, path, storage_root=storage)["document_id"]
        for j in range(PER_DOCUMENT):
            n += 1
            name = COMPANIES[(i + j) % len(COMPANIES)]
            confidence = LEVELS[j % len(LEVELS)]
            store.execute(
                "INSERT INTO instances_Company (instance_id, document_id, name,"
                " naive_key, source, confidence, status, created_at) VALUES "
                "(?,?,?,?,'ai_local',?,'unconfirmed',datetime('now'))",
                (f"inst_{n:03d}", document_id, name, naive_key(name), confidence))
            store.execute(
                "INSERT INTO instance_index (instance_id, type_id, table_name,"
                " document_id, created_at) VALUES (?,'Company',"
                "'instances_Company',?,datetime('now'))",
                (f"inst_{n:03d}", document_id))
            store.execute(
                "INSERT INTO provenance (provenance_id, instance_id, document_id,"
                " excerpt, page_no, source, confidence, alignment, created_at)"
                " VALUES (?,?,?,?,1,'ai_local',?,'exact',datetime('now'))",
                (f"prov_{n:03d}", f"inst_{n:03d}", document_id,
                 f"This Agreement is made between {name} and …", confidence))
    store.close()

    config = datasette_config.build_config(bundle, storage_root=str(storage))
    (work / "datasette.yml").write_text(yaml.safe_dump(config))
    (work / "metadata.yml").write_text(
        yaml.safe_dump(datasette_config.build_metadata(bundle)))
    return db


def reviewed(db: Path) -> int:
    store = connect(db, mode="read")
    try:
        return store.scalar(
            "SELECT COUNT(*) FROM instances_Company "
            "WHERE status IN ('confirmed','amended','rejected')") or 0
    finally:
        store.close()


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
         "--port", str(port), "--root", "--secret", "measure"],
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


def through_the_document_pages(page, base: str, n: int) -> dict:
    """What a reviewer does today: a page load per decision."""
    loads = 0
    per_decision = []
    done = 0

    page.goto(f"{base}/-/orpheus", wait_until="networkidle")
    loads += 1
    links = [a.get_attribute("href") for a in
             page.locator("a[href*='/-/orpheus/document/']").all()]

    for href in links:
        if done >= n:
            break
        page.goto(base + href, wait_until="networkidle")
        loads += 1
        # Every row keeps its Confirm button whether or not it has been
        # reviewed -- re-deciding is legitimate, and the page is right to allow
        # it. So the arm has to name the instance it means, or it re-confirms
        # the first row twenty times and reports twenty decisions it did not
        # make. The store check at the end of the run is what caught that.
        instances = [i.get_attribute("value") for i in
                     page.locator("input[name='instance_id']").all()]
        for instance_id in instances:
            if done >= n:
                break
            button = page.locator(
                f"form:has(input[name='instance_id'][value='{instance_id}']) "
                "button[name='action'][value='confirm']")
            if not button.count():
                continue
            started = time.monotonic()
            button.first.click()
            page.wait_for_load_state("networkidle")
            per_decision.append(time.monotonic() - started)
            loads += 1                    # the redirect back to the document
            done += 1
    return {"decisions": done, "loads": loads, "per_decision": per_decision}


def through_the_queue(page, base: str, n: int) -> dict:
    """One page load, then a keypress each."""
    loads = 0
    per_decision = []

    page.goto(f"{base}/-/orpheus/review/queue", wait_until="networkidle")
    loads += 1
    page.wait_for_selector("#rq-card")

    for done in range(n):
        started = time.monotonic()
        before = page.locator("#rq-position").inner_text()
        page.keyboard.press("c")
        # The card advances only once the store has agreed, so waiting for the
        # counter to move is waiting for the write -- not for an animation.
        page.wait_for_function(
            "was => document.getElementById('rq-position').textContent !== was",
            arg=before, timeout=15000)
        per_decision.append(time.monotonic() - started)
    return {"decisions": n, "loads": loads, "per_decision": per_decision}


def report(name: str, result: dict, elapsed: float, confirmed: int) -> None:
    per = result["per_decision"]
    print(f"\n  {name}")
    print(f"    decisions     {result['decisions']}  "
          f"(store agrees: {confirmed})")
    print(f"    page loads    {result['loads']}")
    print(f"    total         {elapsed:.1f}s")
    print(f"    per decision  {statistics.mean(per):.2f}s "
          f"(median {statistics.median(per):.2f}s)")


def main() -> int:
    from playwright.sync_api import sync_playwright

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8093
    chrome = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")[0]

    print(f"Reviewing {n} findings each way, "
          f"on {DOCUMENTS} documents of {PER_DOCUMENT} findings.")
    results = {}

    for arm, drive in (("document pages", through_the_document_pages),
                       ("the review queue", through_the_queue)):
        work = Path(tempfile.mkdtemp())
        db = build_store(work)
        process, base, token = serve(work, db, port)
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path=chrome,
                                             args=["--no-sandbox"])
                page = browser.new_page(viewport={"width": 1100, "height": 900})
                page.goto(f"{base}/-/auth-token?token={token}")
                started = time.monotonic()
                result = drive(page, base, n)
                elapsed = time.monotonic() - started
                browser.close()
        finally:
            process.terminate()
            process.wait(timeout=10)
        report(arm, result, elapsed, reviewed(db))
        results[arm] = (result, elapsed)

    (old, old_elapsed) = results["document pages"]
    (new, new_elapsed) = results["the review queue"]
    print(f"\n  The queue is {old_elapsed / new_elapsed:.1f}x faster over "
          f"{n} decisions, on {old['loads']} page loads against {new['loads']}.")
    print("  Both arms wrote through the same API; the store was checked after "
          "each.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
