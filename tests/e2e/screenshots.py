"""Take the screenshots the user guide uses, from a real running Orpheus.

Run by hand against a store you have already built:

    python tests/e2e/screenshots.py <store.sqlite> <storage-root> <out-dir> [case]

Committed so the pictures in `docs/user-guide.md` can be regenerated rather
than trusted. A guide illustrated with mockups is a guide that drifts from the
software the first time somebody renames a button, and the reader who notices
is the one who was following it step by step.

It starts a real Datasette with the real plugin, signs in the way `--root`
does, and photographs the pages. Nothing is stubbed.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: (filename, path, full page?). Ordered as the guide walks through them.
SHOTS = {
    "contracts": [
        ("index", "/-/orpheus", True),
        ("document", "{document}", True),
        ("read", "/-/orpheus/read/{document_id}?page=1", True),
        ("wiki", "/-/orpheus/wiki", True),
        ("entity", "{entity}", True),
        ("network", "/-/orpheus/network", True),
        ("calendar", "/-/orpheus/calendar?within_days=3650", True),
        ("lint", "/-/orpheus/lint", True),
        ("registers", "/-/orpheus/registers", True),
    ],
    "survey": [
        ("ontology", "/-/orpheus/ontology", True),
    ],
    "council": [
        ("index", "/-/orpheus", True),
        ("document", "{document}", True),
        ("wiki", "/-/orpheus/wiki", True),
        ("entity", "{entity}", True),
        ("network", "/-/orpheus/network", True),
    ],
}


def serve(db: Path, storage: Path, port: int) -> tuple[subprocess.Popen, str]:
    log = db.parent / "server.log"
    handle = log.open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "datasette", "serve", str(db),
         "--metadata", str(db.parent / "metadata.yml"),
         "--config", str(db.parent / "datasette.yml"),
         "--plugins-dir", str(ROOT / "plugins"),
         "--template-dir", str(ROOT / "templates"),
         "--port", str(port), "--root", "--secret", "guide"],
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
    text = log.read_text()
    match = re.search(r"http://127\.0\.0\.1:\d+/-/auth-token\?token=[a-f0-9]+", text)
    if not match:
        raise SystemExit(f"no sign-in URL in {log}")
    return process, match.group(0).replace(match.group(0).split("/-/")[0], base)


def main() -> int:
    db, storage, out, case = (Path(sys.argv[1]), Path(sys.argv[2]),
                              Path(sys.argv[3]), sys.argv[4])
    out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    port = 8100 + (hash(case) % 200)
    process, token_url = serve(db, storage, port)
    base = f"http://127.0.0.1:{port}"
    try:
        import sqlite3
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        # The document with the most extractions, and the page with the most
        # mentions: the two that show what the software actually did.
        document = conn.execute(
            "SELECT document_id FROM instance_index GROUP BY document_id "
            "ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
        entity = conn.execute(
            "SELECT e.entity_id FROM entities e "
            "JOIN entity_mentions m ON m.entity_id = e.entity_id "
            "WHERE m.unlinked_at IS NULL GROUP BY e.entity_id "
            "ORDER BY COUNT(DISTINCT m.document_id) DESC, COUNT(*) DESC "
            "LIMIT 1").fetchone()
        conn.close()
        fill = {
            "document": (f"/-/orpheus/document/{document['document_id']}"
                         if document else "/-/orpheus"),
            "document_id": document["document_id"] if document else "",
            "entity": (f"/-/orpheus/wiki/{entity['entity_id']}"
                       if entity else "/-/orpheus/wiki"),
        }

        with sync_playwright() as play:
            # The pinned build, not a path Playwright resolves for itself:
            # the image ships one and re-downloading it is blocked.
            chrome = next(iter(sorted(
                Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome"))),
                None)
            browser = play.chromium.launch(
                executable_path=str(chrome) if chrome else None)
            # 1x, not retina: these go in a repository, and a 1280-wide PNG
            # is what a reader's browser shows anyway. Shooting at 2x made the
            # image directory 17MB.
            page = browser.new_page(viewport={"width": 1280, "height": 900},
                                    device_scale_factor=1)
            page.goto(token_url, wait_until="networkidle")
            for name, path, full in SHOTS[case]:
                target = base + path.format(**fill)
                page.goto(target, wait_until="networkidle")
                # The map draws on a canvas after load; everything else is
                # server-rendered and settled by `networkidle`.
                page.wait_for_timeout(1200)
                shot = out / f"{case}-{name}.png"
                page.screenshot(path=str(shot), full_page=full)
                print(f"  {shot.name:34} {target}")
            browser.close()
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
