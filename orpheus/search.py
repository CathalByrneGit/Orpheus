"""Full-text search over what documents say and what was extracted from them.

Two indexes, because they answer different questions. `document_pages.text` is
the source: *does this name appear anywhere we have not extracted it from?*
`provenance.excerpt` is what the machine actually quoted: *what did it read that
on?* The first is the one the wiki direction needs — a name found in a document
with no matching instance is an **unlinked mention**, and that is entity
resolution as something a person decides rather than something an algorithm
guesses.

sqlite-utils rather than hand-rolled FTS5. The triggers that keep an index in
step with its table, and the escaping that stops a stray quote in a company name
becoming a syntax error, are both easy to write and easy to write subtly wrong.
It is an optional install (`pip install 'orpheus[search]'`), so everything here
says what is missing rather than failing obscurely.

It never opens its own connection: `Database(store.conn)` borrows the one it is
handed, which is what lets this run inside Datasette's write thread. Verified
rather than assumed — it takes no transaction of its own and commits nothing.
"""

from __future__ import annotations

from .store import Store
from .utils import OrpheusError

PAGE_INDEX = ("document_pages", ["text"])
EXCERPT_INDEX = ("provenance", ["excerpt"])


def _database(store: Store):
    try:
        import sqlite_utils
    except ImportError as exc:
        raise OrpheusError(
            "Full-text search needs sqlite-utils. "
            "`pip install 'orpheus[search]'`."
        ) from exc
    # The connection the store already holds, never a second one.
    return sqlite_utils.Database(store.conn)


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("sqlite_utils") is not None


def enable_search(store: Store, rebuild: bool = False) -> dict:
    """Build the indexes, with triggers so they stay in step.

    Idempotent: `enable_fts` on an existing index is a no-op unless `rebuild`
    is asked for, which is what makes this safe to call at startup.
    """
    store.assert_writable()
    db = _database(store)
    built = {}
    with store.transaction():
        for table, columns in (PAGE_INDEX, EXCERPT_INDEX):
            if table not in db.table_names():
                continue
            existing = f"{table}_fts" in db.table_names()
            if existing and not rebuild:
                built[table] = "already indexed"
                continue
            db[table].enable_fts(columns, create_triggers=True,
                                 tokenize="porter", replace=True)
            built[table] = "rebuilt" if existing else "indexed"
    return built


def _search(store: Store, table: str, query: str, columns: list[str],
            limit: int) -> list[dict]:
    db = _database(store)
    if f"{table}_fts" not in db.table_names():
        raise OrpheusError(
            f"{table} is not indexed for search yet. Run `orpheus search --build`."
        )
    return [dict(row) for row in
            db[table].search(query, columns=columns, limit=limit)]


def search_pages(store: Store, query: str, limit: int = 50) -> list[dict]:
    """Where in the corpus does this phrase appear?"""
    return _search(store, "document_pages", query,
                   ["document_id", "page_no", "text"], limit)


def search_excerpts(store: Store, query: str, limit: int = 50) -> list[dict]:
    """What did the machine quote that matches this?"""
    return _search(store, "provenance", query,
                   ["instance_id", "document_id", "page_no", "excerpt",
                    "confidence", "alignment"], limit)


def unextracted_mentions(store: Store, name: str, limit: int = 50) -> dict:
    """Documents that say this name but have no instance carrying it.

    An **extraction** gap, not a linking one, and the distinction is why this is
    not called `unlinked_mentions` -- `entities.unlinked_mentions()` answers the
    neighbouring question of which extracted mentions have no entity yet. This
    one finds names the extractor never picked up at all, which no amount of
    linking can recover because there is nothing to link.

    Presented as candidates for a person, never as a merge the machine
    performed.
    """
    from .utils import naive_key

    key = naive_key(name)
    # Which documents already carry an instance with this key.
    linked: set[str] = set()
    for table in {r["table_name"] for r in
                  store.query("SELECT DISTINCT table_name FROM instance_index")}:
        columns = {r[1] for r in store.execute(f'PRAGMA table_info("{table}")')}
        if "naive_key" not in columns:
            continue
        linked |= {r["document_id"] for r in store.query(
            f'SELECT DISTINCT document_id FROM "{table}" WHERE naive_key = ?', (key,))}

    hits = search_pages(store, f'"{name}"', limit=limit)
    return {
        "name": name,
        "naive_key": key,
        "linked_documents": sorted(linked),
        "unlinked": [h for h in hits if h["document_id"] not in linked],
        "resolution_quality": "naive_unresolved",
        "caveat": ("Candidates to check, not matches. The search is on the "
                   "phrase as written, and the linked set is on a normalised "
                   "key; neither is entity resolution."),
    }
