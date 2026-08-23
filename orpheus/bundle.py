"""The ontology bundle: one file that says what this deployment is about.

The format is **ontologySpecR's** — `objects`, `links`, `interfaces`,
`actions`, `queries`, `concepts`, `templates`, camelCase throughout, with a
published JSON Schema. Orpheus does not invent a format; it writes bundles the
spec already describes and keeps everything it needs of its own under
`extensions`, which is where the spec puts vendor concerns. A shipped bundle
validates against the unmodified ontologySpecR schema, and a test says so.

The R implementation carried **every** spelling at once — `object_types` *and*
`objects`, `primary_key` *and* `primaryKey`, `from` *and* `from_type_id` —
because three R packages each read a different one and none would convert.
That doubled the file and made drift possible between two copies of the same
list. Here there is one spelling, and `load()` normalises the legacy one on the
way in. Bundles written for the R stack still load; nothing writes two copies
of anything again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .rubric import RESERVED_PROPS
from .utils import OrpheusError, from_json

SCHEMA_DIR = Path(__file__).parent / "schemas"
BUNDLE_DIR = Path(__file__).parent / "bundles"
DEFAULT_BUNDLE = BUNDLE_DIR / "contract-core-0.2.0.json"

SPEC_SCHEMA = SCHEMA_DIR / "ontologySpecR.bundle.schema.json"
ORPHEUS_SCHEMA = SCHEMA_DIR / "orpheus.bundle.schema.json"


# ---------------------------------------------------------------------------
# Normalising the legacy spelling
# ---------------------------------------------------------------------------

_ROOT_ALIASES = {
    "bundle_id": "bundleId",
    "version": "bundleVersion",
    "bundle_version": "bundleVersion",
    "spec_version": "specVersion",
    "object_types": "objects",
    "link_types": "links",
    "interfaceTypes": "interfaces",
    "concept_defs": "concepts",
    "concept_templates": "templates",
}


# R names its types after R's storage modes. The spec names them after JSON's.
# Mapping here rather than editing every bundle keeps old files loadable.
_TYPE_ALIASES = {
    "double": "number", "numeric": "number", "float": "number",
    "int": "integer", "bool": "boolean", "logical": "boolean",
    "character": "string", "text": "string", "list": "json",
}


def _data_type(name: Any) -> str:
    return _TYPE_ALIASES.get(str(name or "string"), str(name or "string"))


def _display(entry: dict) -> dict | None:
    """Fold `display_name` + `description` into the spec's display block."""
    display = dict(entry.get("display") or {})
    if entry.get("display_name") and "name" not in display:
        display["name"] = entry["display_name"]
    if entry.get("description") and "description" not in display:
        display["description"] = entry["description"]
    return display or None


def _normalise_property(prop: dict) -> dict:
    out = {"id": prop["id"], "type": _data_type(prop.get("type"))}
    if "nullable" in prop:
        out["nullable"] = bool(prop["nullable"])
    display = _display(prop)
    if display:
        out["display"] = display
    if prop.get("source"):
        source = {k: v for k, v in prop["source"].items() if v is not None}
        if source:
            out["source"] = source
    # Codelists and standard mappings are Orpheus's business, not the spec's.
    extensions = {k[2:]: prop[k] for k in ("x_ocds",) if prop.get(k) is not None}
    if prop.get("values") is not None:
        extensions["values"] = prop["values"]
    if prop.get("extensions"):
        extensions.update(prop["extensions"])
    if extensions:
        out["extensions"] = extensions
    return out


def _normalise_object(obj: dict) -> dict:
    out: dict[str, Any] = {"id": obj["id"]}
    display = _display(obj)
    if display:
        out["display"] = display
    out["properties"] = [_normalise_property(p) for p in obj.get("properties", [])]

    primary = obj.get("primaryKey", obj.get("primary_key"))
    if isinstance(primary, str):
        # The legacy spelling named one column. The spec allows a composite key
        # and says how it is generated, which is what entity resolution will
        # need later -- a natural key is the thing a resolved entity has.
        primary = {"properties": [primary], "strategy": "surrogate"}
    elif isinstance(primary, dict):
        primary = {"properties": list(primary.get("properties", [])),
                   "strategy": primary.get("strategy", "surrogate")}
    if primary:
        out["primaryKey"] = primary

    if obj.get("implements"):
        out["implements"] = list(obj["implements"])

    source = dict(obj.get("source") or {})
    table = obj.get("table_name") or source.get("table")
    if table:
        source = {"kind": source.get("kind", "table"), "table": table}
    if source:
        out["source"] = source

    extensions = dict(obj.get("extensions") or {})
    legacy = obj.get("x_orpheus")
    if legacy:
        extensions["orpheus"] = {**legacy, **extensions.get("orpheus", {})}
    if extensions:
        out["extensions"] = extensions
    return out


def _normalise_link(link: dict) -> dict:
    out: dict[str, Any] = {
        "id": link["id"],
        "from": link.get("from") or link.get("from_type_id"),
        "to": link.get("to") or link.get("to_type_id"),
    }
    display = _display(link)
    if display:
        out["display"] = display
    if link.get("cardinality"):
        out["cardinality"] = link["cardinality"]
    if link.get("join"):
        out["join"] = {k: list(v) for k, v in link["join"].items() if v}
    return out


def _normalise_interface(iface: dict) -> dict:
    out: dict[str, Any] = {"id": iface["id"]}
    display = _display(iface)
    if display:
        out["display"] = display
    required = iface.get("requiredProperties") or iface.get("properties") or []
    out["requiredProperties"] = [
        {"id": p["id"], "type": _data_type(p.get("type"))} if isinstance(p, dict)
        else {"id": p, "type": "string"}
        for p in required
    ]
    return out


def _normalise_concept(concept: dict, templates: list[dict] | None = None) -> dict:
    out: dict[str, Any] = {
        "id": concept["id"],
        "objectTypeId": concept.get("objectTypeId") or concept.get("object_type_id"),
        "scope": concept.get("scope", "general"),
        "version": int(concept.get("version", 1)),
    }
    display = _display(concept)
    if display:
        out["display"] = display
    sql = concept.get("sqlExpr") or concept.get("sql_expr")
    if sql:
        out["sqlExpr"] = sql
    template_id = concept.get("templateId") or concept.get("template_id")
    if template_id:
        out["templateId"] = template_id
        values = concept.get("parameterValues") or concept.get("parameter_values")
        if values:
            out["parameterValues"] = values
        if not sql:
            # The spec requires every concept to state its SQL, and it is right
            # to: a file that shows a template id and a bag of parameters does
            # not show what will run. Resolving here means the bundle always
            # does, and validate() then checks the two still agree -- which is a
            # stronger guarantee than the R version's "exactly one of sqlExpr or
            # templateId", and catches the same silent-drift failure.
            resolved = resolve_template({"templates": templates or []},
                                        template_id, values or {})
            if resolved:
                out["sqlExpr"] = resolved
    for key, legacy in (("status", "status"), ("rationale", "rationale"),
                        ("sourceStandard", "source_standard")):
        value = concept.get(key) or concept.get(legacy)
        if value:
            out[key] = value
    out.setdefault("status", "draft")
    return out


def _normalise_template(template: dict) -> dict:
    out: dict[str, Any] = {
        "id": template.get("id") or template.get("template_id"),
        "objectTypeId": template.get("objectTypeId") or template.get("object_type_id"),
        "baseSqlExpr": template.get("baseSqlExpr") or template.get("base_sql_expr"),
    }
    display = _display(template)
    if display:
        out["display"] = display
    params = template.get("parameters") or []
    if isinstance(params, dict):
        # The R spelling: a mapping of name -> {type, default, description}.
        # The default is the load-bearing part -- it is what makes the pipeline
        # run out of the box before anyone sets a policy -- so it survives into
        # `extensions`, which is where the spec keeps what it does not define.
        params = [{"id": name, **(spec if isinstance(spec, dict) else {})}
                  for name, spec in params.items()]

    # A parameterDef takes id, type, required and display and nothing else, so
    # the defaults go in the template's own extensions block -- which is where
    # the spec keeps what it does not define, and keeps the file valid.
    defaults = {}
    out["parameters"] = []
    for param in params:
        if not isinstance(param, dict):
            out["parameters"].append({"id": param, "type": "number"})
            continue
        normalised = {"id": param["id"], "type": _data_type(param.get("type"))}
        if param.get("description"):
            normalised["display"] = {"description": param["description"]}
        elif param.get("display"):
            normalised["display"] = param["display"]
        if param.get("default") is not None:
            defaults[param["id"]] = param["default"]
        out["parameters"].append(normalised)

    extensions = dict(template.get("extensions") or {})
    orpheus_ext = dict(extensions.get("orpheus") or {})
    defaults = {**(orpheus_ext.get("defaults") or {}), **defaults}
    if defaults:
        orpheus_ext["defaults"] = defaults
    if orpheus_ext:
        extensions["orpheus"] = orpheus_ext
    if extensions:
        out["extensions"] = extensions
    return out


def normalise(raw: dict) -> dict:
    """Return the canonical, spec-shaped bundle for any accepted input.

    Idempotent: a bundle already in canonical form comes back unchanged, which
    is what makes it safe to call on load without knowing where a bundle came
    from.
    """
    bundle = {}
    for key, value in raw.items():
        bundle[_ROOT_ALIASES.get(key, key)] = value

    # The legacy file carried each list twice. Whichever survives normalisation
    # wins; they were kept in step by construction, and one of them is going.
    for legacy in ("object_types", "link_types", "concept_defs", "concept_templates",
                   "interfaceTypes", "bundle_id", "spec_version", "version"):
        bundle.pop(legacy, None)

    out: dict[str, Any] = {
        "specVersion": str(bundle.get("specVersion", "1.0")),
        "bundleId": bundle["bundleId"],
        "bundleVersion": str(bundle["bundleVersion"]),
    }

    metadata = dict(bundle.get("metadata") or {})
    if raw.get("bundle_name") and "name" not in metadata:
        metadata["name"] = raw["bundle_name"]
    if raw.get("description") and "description" not in metadata:
        metadata["description"] = raw["description"]
    if raw.get("created_at") and "createdAt" not in metadata:
        metadata["createdAt"] = raw["created_at"]
    if metadata:
        out["metadata"] = metadata

    out["objects"] = [_normalise_object(o) for o in bundle.get("objects", [])]
    out["links"] = [_normalise_link(l) for l in bundle.get("links", [])]
    out["interfaces"] = [_normalise_interface(i) for i in bundle.get("interfaces", [])]
    out["actions"] = list(bundle.get("actions") or [])
    out["queries"] = list(bundle.get("queries") or [])
    # Templates first: a concept may need one resolved to state its own SQL.
    templates = [_normalise_template(t) for t in bundle.get("templates") or []]
    if templates:
        out["templates"] = templates
    if bundle.get("concepts"):
        out["concepts"] = [_normalise_concept(c, templates) for c in bundle["concepts"]]

    extensions = dict(bundle.get("extensions") or {})
    domain = dict(extensions.get("orpheus") or {})
    legacy_domain = raw.get("x_orpheus") or {}
    for legacy_key, key in (("primary_object_type", "primaryObjectType"),
                            ("container_property", "containerProperty"),
                            ("value_property", "valueProperty"),
                            ("currency_property", "currencyProperty"),
                            ("document_types", "documentTypes")):
        if legacy_key in legacy_domain and key not in domain:
            domain[key] = legacy_domain[legacy_key]
    if raw.get("scores") and "scores" not in domain:
        domain["scores"] = [_normalise_score(s) for s in raw["scores"]]
    if domain:
        extensions["orpheus"] = domain
    if extensions:
        out["extensions"] = extensions
    return out


def _normalise_score(score: dict) -> dict:
    out = {
        "id": score.get("id") or score.get("score_id"),
        "objectTypeId": score.get("objectTypeId") or score.get("object_type_id"),
        "components": [
            {
                "conceptId": c.get("conceptId") or c.get("concept_id"),
                "scope": c.get("scope"),
                "weight": c.get("weight", 1.0),
            }
            for c in score.get("components", [])
        ],
    }
    for key, legacy in (("description", "description"), ("aggregation", "aggregation"),
                        ("thresholds", "thresholds")):
        value = score.get(key) or score.get(legacy)
        if value:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def load(source: str | Path | dict | None = None) -> dict:
    """Load and normalise a bundle from a path, a dict, or a JSON string."""
    if source is None:
        source = DEFAULT_BUNDLE
    if isinstance(source, dict):
        raw = source
    elif isinstance(source, (str, Path)) and Path(source).exists():
        raw = json.loads(Path(source).read_text())
    elif isinstance(source, str):
        raw = from_json(source)
        if raw is None:
            raise OrpheusError(f"No bundle at {source!r}, and it is not JSON.")
    else:
        raise OrpheusError(f"No bundle at {source!r}.")
    return normalise(raw)


def _schema_problems(bundle: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:      # pragma: no cover - validation degrades, load does not
        return []
    problems: list[str] = []
    for path in (SPEC_SCHEMA, ORPHEUS_SCHEMA):
        validator = jsonschema.Draft202012Validator(json.loads(path.read_text()))
        for error in sorted(validator.iter_errors(bundle), key=lambda e: list(e.path)):
            where = "/".join(str(p) for p in error.path) or "<root>"
            problems.append(f"{path.stem}: {where}: {error.message}")
    return problems


def validate(bundle: dict) -> dict:
    """Check the bundle against both schemas, then against what the code assumes.

    A schema says the shape is right. It cannot say that an object type
    implementing an interface actually carries the interface's properties, or
    that the domain block names a type that exists — and those are the failures
    that surface deep inside a query weeks later, as fewer rows rather than as
    an error. Both kinds are checked here, at load, where the message can name
    the bundle.
    """
    problems = _schema_problems(bundle)
    problems += _semantic_problems(bundle)
    if problems:
        listed = "\n  - ".join(problems)
        raise OrpheusError(f"Bundle is not valid:\n  - {listed}")
    return bundle


def _semantic_problems(bundle: dict) -> list[str]:
    problems: list[str] = []
    type_ids = {o["id"] for o in bundle.get("objects", [])}

    for obj in bundle.get("objects", []):
        oid = obj.get("id", "<unnamed>")
        prop_ids = [p["id"] for p in obj.get("properties", [])]
        if not prop_ids:
            problems.append(f"object type '{oid}': has no properties")

        seen = {p for p in prop_ids if prop_ids.count(p) > 1}
        if seen:
            problems.append(
                f"object type '{oid}': duplicate properties {sorted(seen)}. "
                "Two declarations of one column means whichever is written last wins."
            )

        managed = (obj.get("extensions", {}).get("orpheus", {}) or {}).get("managed")
        if managed and not (obj.get("source") or {}).get("table"):
            problems.append(f"object type '{oid}': managed but has no source.table")

        for key in obj.get("primaryKey", {}).get("properties", []):
            if key not in prop_ids:
                problems.append(
                    f"object type '{oid}': primaryKey '{key}' is not a declared property")

        for iface_id in obj.get("implements", []):
            iface = interface(bundle, iface_id)
            if iface is None:
                problems.append(
                    f"object type '{oid}': implements unknown interface '{iface_id}'")
                continue
            missing = [p["id"] for p in iface.get("requiredProperties", [])
                       if p["id"] not in prop_ids]
            if missing:
                problems.append(
                    f"object type '{oid}': implements '{iface_id}' but is missing "
                    + ", ".join(f"'{m}'" for m in missing)
                )

    for link in bundle.get("links", []):
        lid = link.get("id", "<unnamed>")
        for end in ("from", "to"):
            if link.get(end) not in type_ids:
                problems.append(
                    f"link type '{lid}': {end} references unknown object type "
                    f"'{link.get(end)}'")
        join = link.get("join") or {}
        if not join.get("fromKeys") or not join.get("toKeys"):
            problems.append(f"link type '{lid}': missing join keys")

    template_ids = {t["id"] for t in bundle.get("templates", [])}
    for concept in bundle.get("concepts", []):
        cid = concept.get("id", "<unnamed>")
        if not concept.get("sqlExpr"):
            problems.append(f"concept '{cid}': has no sqlExpr")
        if concept.get("objectTypeId") not in type_ids:
            problems.append(
                f"concept '{cid}': objectTypeId '{concept.get('objectTypeId')}' "
                "is not an object type in this bundle")
        if concept.get("templateId"):
            if concept["templateId"] not in template_ids:
                problems.append(
                    f"concept '{cid}': names unknown template '{concept['templateId']}'")
            else:
                # A templated concept carries the resolved SQL too, so the file
                # always shows what will actually run. That only helps if the
                # two agree -- an edit to one and not the other is exactly the
                # silent drift the R version's "exactly one of" rule was
                # guarding against, caught here instead.
                expected = resolve_template(
                    bundle, concept["templateId"], concept.get("parameterValues", {}))
                if expected is not None and expected != concept.get("sqlExpr"):
                    problems.append(
                        f"concept '{cid}': sqlExpr does not match template "
                        f"'{concept['templateId']}' resolved with its parameterValues"
                    )

    problems += _domain_problems(bundle)
    return problems


def _domain_problems(bundle: dict) -> list[str]:
    problems: list[str] = []
    d = domain(bundle)
    if not d:
        return ["bundle has no extensions.orpheus block, so the engine cannot "
                "tell what a document is about"]

    primary_id = d.get("primaryObjectType")
    primary = object_type(bundle, primary_id) if primary_id else None
    if primary_id and primary is None:
        problems.append(
            f"extensions.orpheus: primaryObjectType '{primary_id}' is not an "
            "object type in this bundle")
    elif primary is not None:
        prop_ids = [p["id"] for p in primary.get("properties", [])]
        for field in ("valueProperty", "currencyProperty"):
            if d.get(field) and d[field] not in prop_ids:
                problems.append(
                    f"extensions.orpheus: {field} '{d[field]}' is not a property "
                    f"of '{primary_id}'")

    for obj in bundle.get("objects", []):
        clashes = [p["id"] for p in obj.get("properties", [])
                   if p["id"] in RESERVED_PROPS and p["id"] not in ("instance_id",)]
        # instance_id is declared deliberately; the rest are written by the
        # platform and a bundle declaring one would fight it for the column.
        reserved_but_platform = set(clashes) - {"document_id", "source", "confidence",
                                                "status", "amended_by", "amended_at",
                                                "created_at"}
        if reserved_but_platform:
            problems.append(
                f"object type '{obj['id']}': declares reserved property "
                f"{sorted(reserved_but_platform)}")
    return problems


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def domain(bundle: dict) -> dict:
    return (bundle.get("extensions") or {}).get("orpheus") or {}


def document_types(bundle: dict) -> list[str]:
    return list(domain(bundle).get("documentTypes") or [])


def object_types(bundle: dict) -> list[dict]:
    return list(bundle.get("objects") or [])


def object_type(bundle: dict, type_id: str) -> dict | None:
    for obj in object_types(bundle):
        if obj.get("id") == type_id:
            return obj
    return None


def managed_object_types(bundle: dict) -> list[dict]:
    return [o for o in object_types(bundle)
            if (o.get("extensions", {}).get("orpheus", {}) or {}).get("managed")]


def table_name(obj: dict) -> str | None:
    return (obj.get("source") or {}).get("table")


def property_ids(obj: dict) -> list[str]:
    return [p["id"] for p in obj.get("properties", [])]


def interface(bundle: dict, interface_id: str) -> dict | None:
    for iface in bundle.get("interfaces", []):
        if iface.get("id") == interface_id:
            return iface
    return None


def interface_property_ids(iface: dict) -> list[str]:
    return [p["id"] for p in iface.get("requiredProperties", [])]


def implementing_types(bundle: dict, interface_id: str) -> list[str]:
    return [o["id"] for o in object_types(bundle)
            if interface_id in (o.get("implements") or [])]


def queries(bundle: dict) -> list[dict]:
    return list(bundle.get("queries") or [])


def actions(bundle: dict) -> list[dict]:
    return list(bundle.get("actions") or [])


def scores(bundle: dict) -> list[dict]:
    return list(domain(bundle).get("scores") or [])


def label(entry: dict, fallback: str | None = None) -> str:
    """Human name for an object type, property, link or concept."""
    display = entry.get("display") or {}
    return display.get("name") or fallback or entry.get("id", "")


def resolve_template(bundle: dict, template_id: str, values: dict) -> str | None:
    """Substitute `{{param}}` placeholders in a template's base expression."""
    for template in bundle.get("templates", []):
        if template.get("id") != template_id:
            continue
        sql = template.get("baseSqlExpr", "")
        for key, value in (values or {}).items():
            sql = sql.replace("{{" + str(key) + "}}", _sql_literal(value))
        return sql
    return None


def _sql_literal(value: Any) -> str:
    """Render a parameter for embedding in SQL.

    Numbers are formatted without exponent notation. R produced `5e+06` for
    five million, which is valid R and not valid SQLite, and the bug survived
    one fix because it was applied where the SQL was displayed rather than
    where it was stored.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))
    return "'" + str(value).replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Schema generation
# ---------------------------------------------------------------------------

_SQL_TYPES = {
    "string": "TEXT", "integer": "INTEGER", "number": "REAL",
    "boolean": "INTEGER", "date": "TEXT", "datetime": "TEXT", "json": "TEXT",
}


def ddl(bundle: dict) -> list[str]:
    """CREATE TABLE and index statements for every managed object type.

    One table per object type, generated rather than hand-written, so adding a
    type to the bundle is the whole of adding a type.
    """
    statements: list[str] = []
    for obj in managed_object_types(bundle):
        table = table_name(obj)
        if not table:
            continue
        columns = []
        for prop in obj.get("properties", []):
            sql_type = _SQL_TYPES.get(prop.get("type", "string"), "TEXT")
            null = "" if prop.get("nullable", True) else " NOT NULL"
            columns.append(f'  "{prop["id"]}" {sql_type}{null}')
        # Store bookkeeping, deliberately not a bundle property: declaring it
        # as one would put it in every object-set projection, where it means
        # nothing to anyone reading the data.
        if "created_at" not in property_ids(obj):
            columns.append('  "created_at" TEXT')
        keys = obj.get("primaryKey", {}).get("properties") or ["instance_id"]
        columns.append("  PRIMARY KEY (" + ", ".join(f'"{k}"' for k in keys) + ")")
        statements.append(
            f'CREATE TABLE IF NOT EXISTS "{table}" (\n' + ",\n".join(columns) + "\n)"
        )
        if "document_id" in property_ids(obj):
            statements.append(
                f'CREATE INDEX IF NOT EXISTS "idx_{table}_doc" '
                f'ON "{table}" ("document_id")'
            )
    return statements


# ---------------------------------------------------------------------------
# Registering a bundle in a store
# ---------------------------------------------------------------------------

def register(store, bundle: dict, actor_id: str | None = None,
             activate: bool = True, stage: str = "production") -> dict:
    """Store a bundle and, by default, make it the active one.

    Activating applies its schema: one table per managed object type, generated
    from the declaration rather than written by hand, so adding a type to the
    bundle is the whole of adding a type.
    """
    from .utils import now, to_json

    store.assert_writable()
    if stage not in ("production", "staging"):
        raise OrpheusError("stage must be 'production' or 'staging'.")
    if stage == "staging" and activate:
        raise OrpheusError(
            "A staging bundle cannot be activated. Promote it to production first.")

    validate(bundle)
    with store.transaction():
        store.execute(
            "INSERT INTO bundles (bundle_id, bundle_version, bundle_json, stage, "
            "created_at, created_by, is_active) VALUES (?, ?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(bundle_id, bundle_version) DO UPDATE SET "
            "bundle_json = excluded.bundle_json, stage = excluded.stage",
            (bundle["bundleId"], bundle["bundleVersion"], to_json(bundle), stage,
             now(), actor_id))
        if activate:
            store.execute("UPDATE bundles SET is_active = 0")
            store.execute(
                "UPDATE bundles SET is_active = 1 WHERE bundle_id = ? AND bundle_version = ?",
                (bundle["bundleId"], bundle["bundleVersion"]))
            apply_schema(store, bundle)
    return bundle


def apply_schema(store, bundle: dict) -> list[str]:
    """Create the instance tables, and add columns the bundle has gained.

    Additive only. A property removed from the bundle leaves its column in
    place, because dropping it would destroy data a reviewer may have corrected
    by hand.
    """
    statements = ddl(bundle)
    for statement in statements:
        store.execute(statement)

    for obj in managed_object_types(bundle):
        table = table_name(obj)
        if not table or not store.table_exists(table):
            continue
        existing = set(store.columns(table))
        if "created_at" not in existing:
            store.execute(f'ALTER TABLE "{table}" ADD COLUMN "created_at" TEXT')
            existing.add("created_at")
        for prop in obj.get("properties", []):
            if prop["id"] in existing:
                continue
            sql_type = _SQL_TYPES.get(prop.get("type", "string"), "TEXT")
            store.execute(f'ALTER TABLE "{table}" ADD COLUMN "{prop["id"]}" {sql_type}')
            statements.append(f'ALTER TABLE "{table}" ADD COLUMN "{prop["id"]}"')
    return statements


def active(store) -> dict | None:
    row = store.one("SELECT bundle_json FROM bundles WHERE is_active = 1")
    return normalise(from_json(row["bundle_json"])) if row else None
