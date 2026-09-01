"""Getting a public register into the store.

`registers.py` holds reference data apart from the corpus and says why: only 2
of 74 companies in the calibration corpus state a registered number, and a
shared registered number is the decisive, rare value resolution otherwise never
gets. An active register holds those numbers. This is the part that puts one
there.

**It is a fetch, and nothing more.** Everything downstream is unchanged: rows
land `staged`, somebody promotes the register, `identifier_candidates` proposes
links, and a person confirms them. A register that arrived over HTTP gets no
more trust than one somebody pasted, because the question a reviewer is being
asked -- *is this row about this page* -- is the same question either way.

## Bulk file first, API second

Every public register publishes a file: the Irish CRO publishes company data as
CSV, Companies House a monthly bulk product, GLEIF a daily concatenated golden
copy. A file has no API key to procure, no rate limit to respect, no per-lookup
network dependency, and it works in a room with no internet -- all of which
matter more in a public body than in a startup. `registers.load_csv` has read
one since registers were built, so a deployment that can download a file
already has everything it needs and should use it.

The HTTP adapters here exist for the case where a file is not practical: a
handful of names to check against a register that is too large to hold.

## What is honest about the adapters below

The `gleif` adapter is written to the documented shape of the GLEIF API and
**has not been run against the live service**, because the environment this was
built in denies egress to `api.gleif.org`. Its parsing is covered by tests
against a recorded-shape fixture, which establishes that the parser does what
the fixture says and not that the fixture is what GLEIF sends. Anyone wiring
this to a live register should check one response by hand before trusting it,
and `fetch()` returns the raw payload alongside the rows so that is possible
without a debugger.

GLEIF is the one chosen to ship because it needs no API key, which is the
single biggest practical obstacle in a public-sector deployment. Companies
House and the CRO both need one, so they are left as a documented shape rather
than a half-configured client.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable

from .utils import OrpheusError

#: How long to wait on a register that is not answering. Short on purpose: a
#: lookup is a convenience, and a reviewer sitting in front of a spinner will
#: stop using the feature long before the timeout matters.
TIMEOUT = 20

GLEIF_ENDPOINT = "https://api.gleif.org/api/v1/lei-records"


def gleif_rows(payload: dict) -> list[dict]:
    """LEI records, as register rows.

    `entity.registeredAs` is the field this whole exercise is for: the number
    the *national* register gave the company, which is what an Irish contract
    means when it says "company number 482991". The LEI itself is kept beside
    it because it is the one identifier that is the same in every jurisdiction,
    and a corpus spanning two of them will want it.
    """
    rows = []
    for record in (payload or {}).get("data") or []:
        attributes = record.get("attributes") or {}
        entity = attributes.get("entity") or {}
        legal = entity.get("legalName") or {}
        address = entity.get("legalAddress") or {}
        registration = entity.get("registeredAs")
        rows.append({
            "name": legal.get("name"),
            # The national number when there is one, falling back to the LEI.
            # Never blank: a register row with no identifier is a row that
            # cannot settle anything, which is the one thing rows are for.
            "identifier": registration or attributes.get("lei"),
            "lei": attributes.get("lei"),
            "registered_as": registration,
            "jurisdiction": entity.get("jurisdiction"),
            "status": (entity.get("status") or ""),
            "address": ", ".join(
                part for part in (
                    " ".join(address.get("addressLines") or []),
                    address.get("city"), address.get("country")) if part),
        })
    return [row for row in rows if row["name"] and row["identifier"]]


def gleif_query(name: str, limit: int) -> str:
    return (f"{GLEIF_ENDPOINT}?"
            + urllib.parse.urlencode({"filter[entity.legalName]": name,
                                      "page[size]": limit}))


#: name → (build the URL, parse the payload). A deployment adds its own by
#: putting a pair in here; nothing else needs to change.
ADAPTERS: dict[str, tuple[Callable[[str, int], str],
                          Callable[[dict], list[dict]]]] = {
    "gleif": (gleif_query, gleif_rows),
}


def fetch(name: str, *, source: str = "gleif", limit: int = 5,
          opener: Callable[[str], bytes] | None = None) -> dict:
    """Ask a public register about one name.

    Returns the parsed rows *and* the raw payload. The raw half is not
    debugging clutter: these adapters are written to a documented shape rather
    than to a captured response, so the first thing anyone wiring one up should
    do is read what actually came back.
    """
    if source not in ADAPTERS:
        raise OrpheusError(
            f"No adapter for {source!r}. Available: {', '.join(sorted(ADAPTERS))}. "
            "A bulk file loaded with `orpheus register load` needs no adapter "
            "at all and is the better path where one is published.")
    build, parse = ADAPTERS[source]
    url = build(name, limit)

    def _open(target: str) -> bytes:
        request = urllib.request.Request(
            target, headers={"Accept": "application/vnd.api+json",
                             "User-Agent": "orpheus"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read()

    try:
        raw = (opener or _open)(url)
    except Exception as exc:                       # noqa: BLE001 - reported
        raise OrpheusError(
            f"Could not reach {source}: {exc}. A bulk file loaded with "
            "`orpheus register load` needs no network at lookup time and is "
            "the better path where the register publishes one.") from exc

    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise OrpheusError(
            f"{source} did not return JSON. The first 200 bytes were: "
            f"{raw[:200]!r}") from exc

    return {"source": source, "query": name, "url": url,
            "rows": parse(payload), "raw": payload}


def to_csv(rows: list[dict]) -> str:
    """Register rows as CSV, for `registers.load_csv`.

    Deliberately routed back through the file path rather than inserted
    directly. `load_csv` guesses the name and identifier columns and *says what
    it guessed*, rows land `staged`, and somebody promotes the register. A
    fetch that wrote straight to `register_rows` would skip all three, and the
    thing skipped is the review.
    """
    if not rows:
        return ""
    import csv
    import io
    columns = list(rows[0])
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    return out.getvalue()
