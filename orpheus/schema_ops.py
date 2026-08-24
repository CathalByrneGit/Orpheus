"""Renaming, dropping and retyping a property on a table that already holds rows.

`bundle.apply_schema()` is deliberately **additive**: it adds columns the bundle
has gained, which is what makes accepting a `new_property` amendment a live
operation rather than a redeployment. It cannot remove or rename one, so a
mistake in an ontology — a property named wrongly, a property that turned out to
be two — was permanent in any store that had already run.

SQLite cannot do it in place either. The safe path is create-new, copy, drop,
rename, preserving indexes and foreign keys, and it is easy to get subtly wrong.
sqlite-utils' `transform()` implements exactly that, so it is used rather than
reimplemented. Optional install (`pip install 'orpheus[schema]'`).

**The bundle stays the authority.** Every operation here changes the table *and*
the bundle together, in one transaction, and bumps the bundle's patch version.
A store whose tables and bundle disagree is worse than one that cannot rename:
extraction would write to a column the ontology does not declare, and the
amendment queue exists precisely to stop that.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from .audit import record_edit
from .review import bump_patch
from .rubric import RESERVED_PROPS
from .store import Store
from .utils import OrpheusError, require_string


def _database(store: Store):
    try:
        import sqlite_utils
    except ImportError as exc:
        raise OrpheusError(
            "Changing a property on an existing table needs sqlite-utils. "
            "`pip install 'orpheus[schema]'`."
        ) from exc
    return sqlite_utils.Database(store.conn)


def _locate(store: Store, type_id: str, property_id: str) -> tuple[dict, dict, str]:
    bundle = bundle_mod.active(store)
    if bundle is None:
        raise OrpheusError("No bundle is registered, so there is nothing to change.")
    obj = bundle_mod.object_type(bundle, type_id)
    if obj is None:
        raise OrpheusError(f"The active bundle has no object type {type_id!r}.")
    if property_id in RESERVED_PROPS:
        raise OrpheusError(
            f"{property_id!r} is provenance the store owns, not a property of "
            f"{type_id!r}. Renaming or dropping it would break every row's "
            "audit trail."
        )
    if property_id not in bundle_mod.property_ids(obj):
        raise OrpheusError(
            f"{type_id!r} has no property {property_id!r}.")
    table = bundle_mod.table_name(obj)
    if not table:
        raise OrpheusError(f"{type_id!r} is not backed by a managed table.")
    return bundle, obj, table


def _reregister(store: Store, bundle: dict, actor_id: str | None) -> str:
    version = bump_patch(bundle["bundleVersion"])
    bundle["bundleVersion"] = version
    bundle_mod.register(store, bundle, actor_id=actor_id, activate=True)
    return version


def rename_property(store: Store, type_id: str, property_id: str, new_id: str,
                    actor_id: str | None = None) -> dict:
    """Rename a property, keeping every row's value.

    Not a drop and an add: those would lose the data, and losing it is exactly
    what a rename is chosen to avoid.
    """
    store.assert_writable()
    require_string(new_id, "new_id")
    bundle, obj, table = _locate(store, type_id, property_id)
    if new_id in bundle_mod.property_ids(obj):
        raise OrpheusError(f"{type_id!r} already has a property {new_id!r}.")
    if new_id in RESERVED_PROPS:
        raise OrpheusError(
            f"{new_id!r} is a name the store reserves for provenance.")

    db = _database(store)
    with store.transaction():
        db[table].transform(rename={property_id: new_id})
        for prop in obj["properties"]:
            if prop["id"] == property_id:
                prop["id"] = new_id
                if isinstance(prop.get("source"), dict):
                    prop["source"]["column"] = new_id
                break
        version = _reregister(store, bundle, actor_id)
        record_edit(store, table, f"{type_id}.{property_id}", None, "schema",
                    previous={"property_id": property_id},
                    new={"property_id": new_id, "bundle_version": version},
                    actor_id=actor_id,
                    note=f"Renamed {type_id}.{property_id} to {new_id}.")
    return {"type_id": type_id, "renamed": {property_id: new_id},
            "table": table, "bundle_version": version}


def drop_property(store: Store, type_id: str, property_id: str,
                  actor_id: str | None = None) -> dict:
    """Remove a property, and the values under it.

    Destructive, and the only thing in the store that is. Everything else is
    append-only or supersede-in-place, so this refuses unless the column is
    genuinely empty or `force=True` is passed by a caller that has said out loud
    it means to discard data.
    """
    return _drop(store, type_id, property_id, actor_id, force=False)


def force_drop_property(store: Store, type_id: str, property_id: str,
                        actor_id: str | None = None) -> dict:
    """Drop a property that still holds values, discarding them."""
    return _drop(store, type_id, property_id, actor_id, force=True)


def _drop(store: Store, type_id: str, property_id: str, actor_id: str | None,
          force: bool) -> dict:
    store.assert_writable()
    bundle, obj, table = _locate(store, type_id, property_id)

    populated = store.scalar(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{property_id}" IS NOT NULL '
        f'AND "{property_id}" != \'\'') or 0
    if populated and not force:
        raise OrpheusError(
            f"{type_id}.{property_id} still holds {populated} value(s). "
            "Dropping it discards them, and nothing else in this store deletes "
            "anything -- rejected rows are kept precisely because they are "
            "evidence. Use force_drop_property() to say you mean it."
        )

    db = _database(store)
    with store.transaction():
        db[table].transform(drop={property_id})
        obj["properties"] = [p for p in obj["properties"] if p["id"] != property_id]
        version = _reregister(store, bundle, actor_id)
        record_edit(store, table, f"{type_id}.{property_id}", None, "schema",
                    previous={"property_id": property_id,
                              "values_discarded": populated},
                    new={"bundle_version": version}, actor_id=actor_id,
                    note=f"Dropped {type_id}.{property_id}"
                         + (f", discarding {populated} value(s)." if populated else "."))
    return {"type_id": type_id, "dropped": property_id, "table": table,
            "values_discarded": populated, "bundle_version": version}


def available() -> bool:
    import importlib.util
    return importlib.util.find_spec("sqlite_utils") is not None
