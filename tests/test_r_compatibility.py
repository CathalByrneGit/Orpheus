"""The R implementation and this one share a store.

Not a nicety: the point of the port is to move the code, not the data. A store
written by the R package must open here, upgrade in place, and keep every row.
The fixture is a real store created by the R implementation, stripped of its
rows so the file stays small -- the schema, and the migration record saying
which migrations R had run, are what matter.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

from orpheus.store import connect

FIXTURE = Path(__file__).parent / "fixtures" / "r-built-store.sqlite"


def _schema(conn) -> dict:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master "
        # Per-type instance tables and their indexes come from the bundle, not
        # from a migration, so a store with no bundle applied has none of them.
        "WHERE name NOT LIKE 'sqlite_%' "
        "  AND name NOT LIKE 'instances_%' AND name NOT LIKE 'idx_instances_%'"
    ).fetchall()
    return {(t, n): _normalise(s) for t, n, s in rows}


def _normalise(sql: str | None) -> str:
    """Compare DDL by meaning, not by layout.

    R heredocs carry their source indentation into the stored SQL, and spacing
    around a parenthesis is never semantic, so both are flattened before the
    two schemas are compared.
    """
    flat = re.sub(r"\s+", " ", sql or "").strip()
    return re.sub(r"\s*([(),])\s*", r"\1", flat)


def _columns(ddl: str) -> dict[str, str]:
    """`{name: type}` from a normalised CREATE TABLE."""
    body = ddl[ddl.index("(") + 1:ddl.rindex(")")]
    out: dict[str, str] = {}
    depth = 0
    field = ""
    for char in body + ",":
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts = field.strip().split()
            if parts and parts[0].upper() not in (
                    "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"):
                out[parts[0]] = parts[1].upper() if len(parts) > 1 else ""
            field = ""
        else:
            field += char
    return out


def test_python_migrations_reproduce_the_r_schema(tmp_path):
    fresh = connect(tmp_path / "python.sqlite")
    try:
        python = _schema(fresh.conn)
    finally:
        fresh.close()
    r = _schema(sqlite3.connect(FIXTURE))

    # What Python has and R did not. Migration 3 adopts the five tables
    # conceptR used to create as a side effect, which is why they were absent
    # from R's own migration list even though the store depended on them.
    # Migration 4 adds grounding to provenance, which R never recorded at all.
    new_in_python = {("index", "idx_concept_versions_active"),
                     ("index", "idx_provenance_alignment")}
    assert set(python) - set(r) == new_in_python

    # Everything R had, Python creates. Whitespace is normalised because R
    # heredocs carry their source indentation into the DDL.
    assert set(r) - set(python) == set()
    for key in set(r) & set(python):
        if key[0] == "table":
            # A later migration may *add* a column -- migration 4 adds three to
            # provenance. What must not happen is a column R had changing type
            # or disappearing, because an R-built store would then be read
            # wrongly rather than merely read incompletely.
            assert _columns(r[key]).items() <= _columns(python[key]).items(), key
        else:
            assert python[key] == r[key], key


def test_an_r_built_store_upgrades_in_place(tmp_path):
    path = tmp_path / "from-r.sqlite"
    shutil.copy(FIXTURE, path)

    store = connect(path)
    try:
        from orpheus.schema import MIGRATIONS

        versions = [row["version"] for row in
                    store.query("SELECT version FROM schema_migrations ORDER BY version")]
        # R had run 1 and 2; Python adds the rest without rebuilding anything.
        assert versions == sorted(m["version"] for m in MIGRATIONS)
        assert versions[:2] == [1, 2]
        assert store.table_exists("concept_versions")
        assert store.table_exists("composite_score_components")

        # And the store is usable, not merely openable.
        store.set_setting("cloud_ai_policy", "disabled")
        assert store.setting("cloud_ai_policy") == "disabled"
    finally:
        store.close()


def test_upgrading_twice_is_a_no_op(tmp_path):
    path = tmp_path / "from-r.sqlite"
    shutil.copy(FIXTURE, path)
    connect(path).close()
    store = connect(path)
    try:
        assert store.migrate() == []
        from orpheus.schema import MIGRATIONS
        assert store.scalar("SELECT COUNT(*) FROM schema_migrations") == len(MIGRATIONS)
    finally:
        store.close()
