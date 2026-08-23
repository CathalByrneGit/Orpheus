#!/usr/bin/env python3
"""Fetch real contracts from SEC EDGAR, for measuring extraction quality.

Not 10-K bodies. A 10-K is an annual report: it has no contract value, no term
dates, no governing law and no signature block, so running a contract ontology
over one measures the mismatch between the two rather than how well anything
extracts. The accuracy number would be low and would mean nothing.

**Exhibit 10.x is the corpus.** Item 601(b)(10) of Regulation S-K requires
material contracts to be filed as exhibits, so EDGAR holds hundreds of thousands
of executed commercial agreements -- supply, credit, licence, employment,
lease -- in public, in bulk, free. They are real contracts written by lawyers for
money, which is exactly the thing this pipeline claims to read.

    python3 tools/fetch_edgar_contracts.py --out corpus --limit 40

EDGAR requires a descriptive User-Agent with a contact address and rate-limits
to 10 requests/second; both are honoured below. See
https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

FULL_TEXT_SEARCH = "https://efts.sec.gov/LATEST/search-index?q="
SEARCH = "https://efts.sec.gov/LATEST/search-index"
BROWSE = "https://www.sec.gov/cgi-bin/browse-edgar"
ARCHIVES = "https://www.sec.gov/Archives/edgar/data"

# EDGAR asks for a real contact. Override with --user-agent; the default is
# deliberately not a browser string, because pretending to be one is both
# against their published terms and the reason access gets revoked.
DEFAULT_UA = "Orpheus research (contact: set --user-agent)"

# Exhibit types under Item 601(b)(10): material contracts.
EXHIBIT_TYPES = ("EX-10", "EX-10.1", "EX-10.2", "EX-10.3", "EX-10.4", "EX-10.5")

RATE = 0.15   # seconds between requests; EDGAR's ceiling is 10/second


def get(url: str, user_agent: str, accept: str = "application/json") -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate",
                      "Accept": accept, "Host": urllib.parse.urlparse(url).netloc})
    time.sleep(RATE)
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
    if response.headers.get("Content-Encoding") == "gzip":
        import gzip
        body = gzip.decompress(body)
    return body


def search_exhibits(user_agent: str, limit: int, forms: str = "10-K") -> list[dict]:
    """Full-text search for material-contract exhibits."""
    found: list[dict] = []
    for start in range(0, max(limit, 10), 10):
        query = urllib.parse.urlencode({
            "q": '"This Agreement"', "forms": forms, "from": start,
        })
        payload = json.loads(get(f"https://efts.sec.gov/LATEST/search-index?{query}",
                                 user_agent))
        hits = payload.get("hits", {}).get("hits", [])
        if not hits:
            break
        for hit in hits:
            source = hit.get("_source", {})
            adsh, _, name = hit.get("_id", "").partition(":")
            if not name:
                continue
            found.append({
                "cik": (source.get("ciks") or [""])[0],
                "accession": adsh.replace("-", ""),
                "file": name,
                "company": (source.get("display_names") or [""])[0],
                "filed": source.get("file_date"),
                "form": source.get("root_form"),
            })
            if len(found) >= limit:
                return found
    return found


def html_to_text(html: bytes) -> str:
    """Enough HTML stripping for an EDGAR exhibit.

    Exhibits are filed as loosely-structured HTML or plain text. Block-level
    tags become newlines so the page structure survives -- which matters,
    because the deterministic pass reads cue phrases near a date and a
    paragraph boundary is part of "near".
    """
    text = html.decode("utf-8", "replace")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br[^>]*>", "\n", text)
    text = re.sub(r"(?i)</(p|div|tr|h[1-6]|li|table)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                         ("&gt;", ">"), ("&#146;", "'"), ("&#147;", '"'),
                         ("&#148;", '"'), ("&quot;", '"'), ("&#39;", "'")):
        text = text.replace(entity, char)
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="corpus", help="directory to write into")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--forms", default="10-K",
                        help="parent form type to pull exhibits from")
    parser.add_argument("--user-agent", default=DEFAULT_UA,
                        help="EDGAR requires a contact address here")
    parser.add_argument("--min-chars", type=int, default=4000,
                        help="skip stubs: a one-page exhibit measures nothing")
    args = parser.parse_args(argv)

    if "contact:" in args.user_agent and "set --user-agent" in args.user_agent:
        print("EDGAR requires a real contact address in the User-Agent.\n"
              "  --user-agent 'Your Name your.email@example.org'\n"
              "See https://www.sec.gov/os/accessing-edgar-data", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    already = {entry["file"] for entry in manifest}

    print(f"searching EDGAR for material-contract exhibits in {args.forms} filings…")
    hits = search_exhibits(args.user_agent, args.limit, args.forms)
    print(f"  {len(hits)} candidates")

    written = 0
    for hit in hits:
        name = f"{hit['cik']}-{hit['accession']}-{hit['file']}"
        if name in already:
            continue
        url = f"{ARCHIVES}/{hit['cik']}/{hit['accession']}/{hit['file']}"
        try:
            body = get(url, args.user_agent, accept="text/html")
        except Exception as exc:                       # noqa: BLE001
            print(f"  skip {name}: {exc}", file=sys.stderr)
            continue

        text = html_to_text(body)
        if len(text) < args.min_chars:
            continue

        target = out / (re.sub(r"[^A-Za-z0-9._-]", "_", name) + ".txt")
        target.write_text(text)
        manifest.append({**hit, "file": name, "path": str(target),
                         "chars": len(text), "url": url})
        written += 1
        print(f"  {target.name}  {len(text):>7,} chars  {hit['company'][:40]}")

    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"\n{written} written, {len(manifest)} in {out}")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
