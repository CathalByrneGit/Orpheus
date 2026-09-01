"""Take the screenshots the user guide uses, from a real running Orpheus.

Run by hand against a store you have already built:

    python tests/e2e/screenshots.py <store.sqlite> <storage-root> <out-dir> [case]

Committed so the pictures in `docs/user-guide.md` can be regenerated rather
than trusted. A guide illustrated with mockups is a guide that drifts from the
software the first time somebody renames a button, and the reader who notices
is the one who was following it step by step.

It starts a real Datasette with the real plugin, signs in the way `--root`
does, and photographs the pages. Nothing is stubbed.

The chat shot needs two things the others do not: `datasette-agent` installed,
and `datasette-llm` given a `default_model` in the store's `datasette.yml`. The
server inherits this process's environment, so whatever key that model needs
should be exported before running this. Without either, the panel is not
rendered and the shot is skipped with a line saying so -- a missing screenshot
beats one of an empty box captioned as a conversation.

Shots are 1280 wide at 1x, and full-page unless the entry says otherwise. These
land in a repository, so afterwards they are worth putting through a palette
squeeze -- they are pictures of text, and 192 colours is lossless to the eye:

    from PIL import Image
    image = Image.open(path).convert("RGB")
    image.convert("P", palette=Image.ADAPTIVE, colors=192).save(path, optimize=True)

which took this directory from 7.5 MB to 2.6 MB.
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
        # The README hero: the top of a document page, not the whole thing. A
        # full-page shot of a 31-finding document is 10,000px tall and renders
        # in a README as an unreadable strip.
        ("hero-document", "{document}", False),
        ("index", "/-/orpheus", True),
        ("document", "{document}", True),
        ("read", "/-/orpheus/read/{document_id}?page=1", True),
        ("wiki", "/-/orpheus/wiki", True),
        ("entity", "{entity}", True),
        ("network", "/-/orpheus/network", True),
        ("map", "/-/orpheus/map?depth=3", False),
        ("chat", "/-/orpheus/read/{document_id}?page=1", False),
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


#: Typed into the reading page's chat, so the screenshot shows an answer rather
#: than an empty box. Chosen to be a question only the *store* can answer --
#: a model reading the passage alone could guess at the parties, and could not
#: say whether anybody has checked them.
CHAT_QUESTION = ("Who are the parties on this page, and has anybody confirmed "
                 "them yet?")


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


def _ask_the_chat(page) -> bool:
    """Type a question into the reading page's chat and wait for the answer.

    Returns False when the deployment has no chat to drive: `datasette-agent`
    needs `datasette-llm` configured with a `default_model`, and without one the
    panel is not rendered at all. A missing screenshot is a better outcome than
    one of an empty box captioned as a conversation.
    """
    box = page.query_selector("#orpheus-ask-text")
    if box is None:
        return False
    box.fill(CHAT_QUESTION)
    page.click("#orpheus-ask button[type=submit]")
    # The answer arrives inside datasette-agent's own conversation view, in a
    # frame, streamed. Wait for text in it rather than for a fixed delay.
    page.wait_for_selector("#orpheus-chat:not([hidden])", timeout=30_000)
    frame = page.frame_locator("#orpheus-chat")
    # Wait for the answer to *settle*, not merely to start. The agent calls
    # tools and streams, so the first few hundred characters are usually it
    # narrating what it is about to look up -- a screenshot taken there shows
    # the machine thinking rather than the machine answering.
    previous, unchanged = "", 0
    for _ in range(90):
        try:
            body = frame.locator("body").inner_text(timeout=2_000)
        except Exception:                          # noqa: BLE001 - polling
            body = ""
        unchanged = unchanged + 1 if body == previous and body else 0
        previous = body
        if unchanged >= 4 and len(body) > 300:
            break
        page.wait_for_timeout(2_000)
    page.wait_for_timeout(1_500)
    return True


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
                                    device_scale_factor=1,
                                    # The map is a d3 force simulation that
                                    # fits the view when it settles, which
                                    # takes seconds and lands somewhere
                                    # slightly different each run. Under
                                    # reduced motion the component settles in
                                    # one synchronous pass and fits at once --
                                    # its own comment says the picture is the
                                    # same, it simply does not move on the way
                                    # -- so the shot is immediate and the same
                                    # every time.
                                    reduced_motion="reduce")
            page.goto(token_url, wait_until="networkidle")
            for name, path, full in SHOTS[case]:
                target = base + path.format(**fill)
                page.goto(target, wait_until="networkidle")
                if name == "chat":
                    if not _ask_the_chat(page):
                        print(f"  {'(chat unavailable, skipped)':34} {target}")
                        continue
                # Everything else is server-rendered and settled by
                # `networkidle`; the map needs its one layout pass to run.
                page.wait_for_timeout(3000 if name == "map" else 1200)
                # `hero-*` names are shared across cases and keep their own.
                shot = out / (f"{name}.png" if name.startswith("hero-")
                              else f"{case}-{name}.png")
                page.screenshot(path=str(shot), full_page=full)
                print(f"  {shot.name:34} {target}")
            browser.close()
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
