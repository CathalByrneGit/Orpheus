"""A picture of the review queue, for the documentation.

Run by hand. Builds a small store, opens the queue, and shoots the first card —
the one the ranking chose, with the sentence it was read from and the keys to
decide it.

    python3 tests/e2e/review_queue_shot.py [port]
"""

from __future__ import annotations

import glob
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from measure_review import build_store, serve          # noqa: E402

OUT = ROOT / "docs" / "images"


def main() -> int:
    from playwright.sync_api import sync_playwright

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
    work = Path(tempfile.mkdtemp())
    db = build_store(work)
    process, base, token = serve(work, db, port)
    chrome = glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")[0]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=chrome,
                                         args=["--no-sandbox"])
            context = browser.new_context(viewport={"width": 1060, "height": 900},
                                          device_scale_factor=2)
            page = context.new_page()
            page.goto(f"{base}/-/auth-token?token={token}")
            page.goto(f"{base}/-/orpheus/review/queue", wait_until="networkidle")
            page.wait_for_selector("#rq-card")
            page.wait_for_timeout(500)
            OUT.mkdir(parents=True, exist_ok=True)
            page.locator("#rq-root").screenshot(
                path=str(OUT / "review-queue.png"))
            print("wrote", OUT / "review-queue.png")
            print(" ", " | ".join(x for x in
                                  page.locator("#rq-card").inner_text().split("\n")
                                  if x.strip())[:160])
            browser.close()
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
