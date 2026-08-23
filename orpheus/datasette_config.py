"""The two YAML files Datasette needs, generated from the bundle.

Two files, because Datasette reads them through different paths and they are not
interchangeable: `--metadata` is descriptive text and the only one that reaches
the rendered pages, `--config` is anything that changes behaviour. A canned query
left in the metadata file loses its `sql` on the way through and Datasette 1.0
dies at startup with `KeyError: 'sql'`; a table description put in the config
file renders nothing at all and says nothing about it. Both are generated here so
they cannot disagree.

The canned queries come from the **bundle**, not from this file. They were
hardcoded once, which meant a domain-neutral engine shipped a fixed set of
contract-flavoured questions; a planning-application bundle now ships its own.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import bundle as bundle_mod
from .auth import permission_sql
from .store import Store


def _yaml(value, indent: int = 0) -> str:
    """Just enough YAML for what is generated here.

    Hand-rolled rather than depending on PyYAML: the core has no third-party
    dependencies, and this emits four shapes. Everything is quoted or block-
    scalared, so a bundle name containing a colon cannot produce a file
    Datasette refuses to parse — which it did, once.
    """
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            rendered = _yaml(item, indent + 1)
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:\n{rendered}")
            else:
                lines.append(f"{pad}{key}: {rendered}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        return "\n".join(f"{pad}- {_yaml(v, indent + 1).lstrip()}" for v in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)

    text = str(value)
    if "\n" in text:
        body = "\n".join(f"{pad}  {line}" for line in text.split("\n"))
        return f"|-\n{body}"
    return json.dumps(text)


def instance_union(bundle: dict) -> str:
    """A UNION over the managed instance tables, for queries that need status.

    `instance_index` deliberately does not carry review status — status lives on
    the instance row, and copying it into the index would mean two places to
    keep in step. So a query that groups by status has to span the tables, and
    the set of tables comes from the bundle.
    """
    tables = [bundle_mod.table_name(o) for o in bundle_mod.managed_object_types(bundle)]
    tables = [t for t in tables if t]
    return "\nUNION ALL ".join(
        f'SELECT instance_id, status FROM "{t}"' for t in tables)


def build_metadata(bundle: dict, database_name: str = "orpheus") -> dict:
    title = f"Orpheus: {bundle.get('metadata', {}).get('name') or bundle['bundleId']}"
    return {
        "title": title,
        "description_html": (
            "<p>Read-only view of the Orpheus store. Every write goes through "
            "the single writer; this process only reads.</p>\n"
            "<p>AI-sourced rows carry <code>source</code>, <code>confidence</code> "
            "and <code>status</code>. A row with <code>status = unconfirmed</code> "
            "has not been checked by a person. <code>confidence</code> is one of "
            "five rubric levels, not an arbitrary score: 1.0 explicit, 0.9 "
            "clearly named, 0.7 implied, 0.5 inferred, 0.2 speculative.</p>"),
        "databases": {database_name: {"tables": {
            "documents": {"description":
                          "One row per ingested document. review_status is the "
                          "document-level flag; per-instance review state lives "
                          "on the instance tables."},
            "provenance": {"description":
                           "What the machine said, and where it read it. Never "
                           "changed by a correction — the instance row carries "
                           "the current values, this row carries the original."},
            "edit_history": {"description":
                             "Append-only. Ordered by seq rather than timestamp, "
                             "because changes made in one transaction share a "
                             "timestamp to the second."},
            "schema_amendments": {"description":
                                  "Properties and types seen during extraction "
                                  "but not declared in the bundle. Accepting one "
                                  "changes the bundle for every document."},
            "concept_evaluations": {"description":
                                    "stale = 1 means an instance this read has "
                                    "since been amended."},
            "llm_calls": {"description":
                          "Every model call, local and cloud, attempted or "
                          "succeeded."},
        }}},
    }


def build_config(bundle: dict, database_name: str = "orpheus",
                 api_url: str = "http://127.0.0.1:8000",
                 max_file_size: int = 50 * 1024 * 1024) -> dict:
    queries: dict = {}
    for query in bundle_mod.queries(bundle):
        definition = query.get("definition") or {}
        if definition.get("kind") not in (None, "sql"):
            continue
        body = definition.get("body", "")
        extensions = (query.get("extensions") or {}).get("orpheus") or {}
        if "instanceUnion" in (extensions.get("expand") or []):
            body = body.replace("{{instanceUnion}}", instance_union(bundle))
        entry = {"title": (query.get("display") or {}).get("name", query["id"]),
                 "sql": body}
        if extensions.get("allow"):
            entry["allow"] = extensions["allow"]
        queries[query["id"]] = entry

    return {
        # No anonymous access. The documents this store holds have no public
        # audience, so an actor is required before any resource-level rule is
        # even consulted.
        "allow": {"id": "*"},
        "plugins": {"orpheus-datasette": {
            "api_url": api_url,
            "max_file_size": max_file_size,
            # Named, not written: regenerating this file never commits a secret.
            "token": {"$env": "ORPHEUS_API_TOKEN"},
        }},
        "databases": {database_name: {
            "tables": {
                "actor_tokens": {"allow": False},
                "actors": {"allow": {"is_admin": 1}},
                "llm_calls": {"allow": {"is_admin": 1}},
                "edit_history": {"sort": "seq"},
            },
            "queries": queries,
        }},
    }


def write_config(path: str | Path = "inst/datasette/datasette.yml",
                 database_name: str = "orpheus", bundle: dict | None = None,
                 api_url: str = "http://127.0.0.1:8000",
                 max_file_size: int = 50 * 1024 * 1024,
                 metadata_path: str | Path | None = None) -> dict:
    """Write both files. Returns their paths."""
    bundle = bundle or bundle_mod.load()
    path = Path(path)
    metadata_path = Path(metadata_path or path.parent / "metadata.yml")
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)

    header = (
        "# Generated by orpheus.datasette_config — regenerate rather than editing.\n"
        f"#\n#   ORPHEUS_API_TOKEN=... datasette serve data/{database_name}.sqlite \\\n"
        f"#     --metadata {metadata_path.name} --config {path.name} \\\n"
        "#     --plugins-dir plugins --template-dir templates --port 8001\n\n")

    metadata_path.write_text(header + _yaml(build_metadata(bundle, database_name)) + "\n")

    permissions = (
        "\n# ---------------------------------------------------------------\n"
        "# Per-document row-level permissions\n"
        "#\n"
        "# Datasette's own allow blocks gate a whole table or database, not\n"
        "# rows. The rules below are what a permission_resources_sql plugin\n"
        "# hook should run; they are generated from auth.permission_sql(), so\n"
        "# they cannot drift from what the API enforces. Bind :actor_id.\n"
        "# ---------------------------------------------------------------\n"
        + "\n".join("# " + line for line in permission_sql("view").split("\n"))
        + "\n#\n"
        + "\n".join("# " + line for line in permission_sql("edit").split("\n"))
        + "\n")

    path.write_text(header
                    + _yaml(build_config(bundle, database_name, api_url, max_file_size))
                    + "\n" + permissions)
    return {"config": str(path), "metadata": str(metadata_path)}


def serve_command(db_path: str = "data/orpheus.sqlite",
                  metadata_path: str = "inst/datasette/metadata.yml",
                  config_path: str = "inst/datasette/datasette.yml",
                  port: int = 8001, ui: bool = True,
                  immutable: bool = False) -> str:
    """The command to serve the store.

    Deliberately **not** `--immutable`, despite that being the obvious flag for
    a database this process never writes to. `--immutable` sets SQLite's
    `immutable=1`, which lets it skip the write-ahead log — and with WAL
    enabled, committed data lives in the `-wal` sidecar until a checkpoint. An
    immutable reader therefore sees the store as of the last checkpoint, which
    on a live database means silently missing rows and no error to notice.
    Measured: 0 documents visible immutable-uncheckpointed, 1 with a plain
    read-only connection.
    """
    parts = ["datasette serve"]
    if immutable:
        parts.append("--immutable")
    parts += [db_path, f"--metadata {metadata_path}", f"--config {config_path}"]
    if ui:
        parts.append("--plugins-dir plugins --template-dir templates")
    parts += [f"--port {port}",
              "--setting sql_time_limit_ms 3000",
              "--setting max_returned_rows 2000"]
    return " ".join(parts)
