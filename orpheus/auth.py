"""Who is asking, and what they may see.

Two layers, deliberately separate. **Identity** — actors and the tokens that
authenticate them — is thin and replaceable: `upsert_actor()` is where any
external provider lands, and `datasette-accounts` is the candidate for taking it
over entirely. **Authorisation** — who may see which document — is Orpheus's
own, because it is about documents rather than about people.

The permission rule is written once, in `can()`, and emitted as SQL by
`permission_sql()` for Datasette's `permission_resources_sql` hook. One rule,
two consumers, so the API and the browsing surface cannot drift apart.
"""

from __future__ import annotations

import hashlib
import secrets

from .audit import record_edit
from .rubric import ACTIONS, SHARE_ROLES, VISIBILITY
from .store import Store
from .utils import (NotFound, PermissionDenied, new_id, now, 
                    require_choice, require_string, to_json)


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------

def create_actor(store: Store, display_name: str, email: str | None = None,
                 idp: str | None = None, external_id: str | None = None,
                 departments: list[str] | None = None, is_admin: bool = False,
                 actor_id: str | None = None) -> str:
    store.assert_writable()
    require_string(display_name, "display_name")
    actor_id = actor_id or new_id("act")
    store.insert("actors", {
        "actor_id": actor_id,
        "display_name": display_name,
        "email": email,
        "idp": idp,
        "external_id": external_id,
        "departments_json": to_json(departments) if departments else None,
        "is_admin": 1 if is_admin else 0,
        "created_at": now(),
    })
    return actor_id


def upsert_actor(store: Store, idp: str, external_id: str, display_name: str,
                 email: str | None = None,
                 departments: list[str] | None = None) -> str:
    """Where an external identity provider lands.

    Kept as one function so that adopting a provider — `datasette-accounts`,
    an SSO, anything — is a change here and nowhere else.
    """
    store.assert_writable()
    existing = store.one(
        "SELECT actor_id FROM actors WHERE idp = ? AND external_id = ?",
        (idp, external_id))
    if existing:
        store.execute(
            "UPDATE actors SET display_name = ?, email = ?, departments_json = ? "
            "WHERE actor_id = ?",
            (display_name, email, to_json(departments) if departments else None,
             existing["actor_id"]))
        return existing["actor_id"]
    return create_actor(store, display_name, email, idp, external_id, departments)


def get_actor(store: Store, actor_id: str) -> dict | None:
    return store.one("SELECT * FROM actors WHERE actor_id = ?", (actor_id,))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def hash_token(token: str) -> str:
    """Only the hash is stored, so the database never holds a usable credential."""
    return hashlib.sha256(f"orpheus:{token}".encode()).hexdigest()


def create_token(store: Store, actor_id: str, label: str | None = None,
                 expires_at: str | None = None) -> dict:
    """Mint a token. The raw value is returned once and never stored."""
    store.assert_writable()
    if get_actor(store, actor_id) is None:
        raise NotFound(f"No actor {actor_id!r}.")
    raw = secrets.token_urlsafe(32)
    token_id = new_id("tok")
    store.insert("actor_tokens", {
        "token_id": token_id,
        "actor_id": actor_id,
        "token_hash": hash_token(raw),
        "label": label,
        "created_at": now(),
        "expires_at": expires_at,
    })
    return {"token_id": token_id, "actor_id": actor_id, "token": raw,
            "label": label, "expires_at": expires_at}


def revoke_token(store: Store, token_id: str, actor_id: str | None = None) -> bool:
    store.assert_writable()
    cursor = store.execute(
        "UPDATE actor_tokens SET revoked_at = ? WHERE token_id = ? AND revoked_at IS NULL",
        (now(), token_id))
    return cursor.rowcount > 0


def authenticate(store: Store, token: str | None) -> dict | None:
    """Resolve a token to an actor, or None.

    Returns None rather than raising for every failure mode — missing, revoked,
    expired, unknown — because the caller turns all of them into the same 401
    and distinguishing them in the reply tells an attacker which tokens exist.
    """
    if not token:
        return None
    row = store.one(
        "SELECT actor_id, expires_at, revoked_at FROM actor_tokens WHERE token_hash = ?",
        (hash_token(token),))
    if row is None or row["revoked_at"]:
        return None
    if row["expires_at"] and row["expires_at"] < now():
        return None
    return get_actor(store, row["actor_id"])


# ---------------------------------------------------------------------------
# Per-document permissions
# ---------------------------------------------------------------------------

def can(store: Store, actor: dict | None, document_id: str, action: str) -> bool:
    """Whether this actor may do this to this document.

    The one rule. `permission_sql()` below emits the same logic as SQL for
    Datasette, and the tests hold the two to agreeing.
    """
    require_choice(action, ACTIONS, "action")
    if not actor or not actor.get("actor_id"):
        return False
    if actor.get("is_admin"):
        return True

    document = store.one(
        "SELECT created_by, visibility FROM documents WHERE document_id = ?",
        (document_id,))
    if document is None:
        return False
    if document["created_by"] and document["created_by"] == actor["actor_id"]:
        return True

    # Sharing and deleting stay with the owner and administrators: a share
    # cannot be used to widen a share.
    if action in ("share", "delete"):
        return False

    share = store.one(
        "SELECT role FROM document_shares WHERE document_id = ? AND actor_id = ?",
        (document_id, actor["actor_id"]))
    if share:
        if action == "view":
            return True
        if action == "edit":
            return share["role"] == "editor"

    visibility = document["visibility"] or "private"
    if visibility == "link-view":
        return action == "view"
    if visibility == "link-edit":
        return action in ("view", "edit")
    return False


def require(store: Store, actor: dict | None, document_id: str, action: str) -> None:
    if not can(store, actor, document_id, action):
        raise PermissionDenied(
            f"Not permitted to {action} document {document_id}. Ask its owner for access.")


def share_document(store: Store, document_id: str, actor_id: str,
                   role: str, granted_by: dict | str) -> str:
    store.assert_writable()
    require_choice(role, SHARE_ROLES, "role")
    granter = granted_by if isinstance(granted_by, dict) else get_actor(store, granted_by)
    require(store, granter, document_id, "share")
    if get_actor(store, actor_id) is None:
        raise NotFound(f"No actor {actor_id!r}.")

    with store.transaction():
        store.execute(
            "INSERT INTO document_shares (document_id, actor_id, role, "
            "granted_by, granted_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(document_id, actor_id) DO UPDATE SET role = excluded.role, "
            "granted_by = excluded.granted_by, granted_at = excluded.granted_at",
            (document_id, actor_id, role, granter["actor_id"], now()))
        # The share has no id of its own -- (document, actor) is the key -- so
        # the history is keyed on the pair.
        record_edit(store, "document_shares", f"{document_id}:{actor_id}",
                    document_id, "share", new={"actor_id": actor_id, "role": role},
                    actor_id=granter["actor_id"])
    return f"{document_id}:{actor_id}"


def unshare_document(store: Store, document_id: str, actor_id: str,
                     revoked_by: dict | str) -> bool:
    store.assert_writable()
    revoker = revoked_by if isinstance(revoked_by, dict) else get_actor(store, revoked_by)
    require(store, revoker, document_id, "share")
    with store.transaction():
        cursor = store.execute(
            "DELETE FROM document_shares WHERE document_id = ? AND actor_id = ?",
            (document_id, actor_id))
        if cursor.rowcount:
            record_edit(store, "document_shares", f"{document_id}:{actor_id}",
                        document_id, "unshare",
                        previous={"actor_id": actor_id}, actor_id=revoker["actor_id"])
    return cursor.rowcount > 0


def set_visibility(store: Store, document_id: str, visibility: str,
                   actor_id: str) -> str:
    store.assert_writable()
    require_choice(visibility, VISIBILITY, "visibility")
    actor = get_actor(store, actor_id)
    require(store, actor, document_id, "share")
    before = store.scalar("SELECT visibility FROM documents WHERE document_id = ?",
                          (document_id,))
    with store.transaction():
        store.execute("UPDATE documents SET visibility = ? WHERE document_id = ?",
                      (visibility, document_id))
        record_edit(store, "documents", document_id, document_id, "visibility",
                    previous={"visibility": before}, new={"visibility": visibility},
                    actor_id=actor_id)
    return visibility


def visible_documents(store: Store, actor: dict | None, limit: int = 100) -> list[dict]:
    """Documents this actor may view, via the same rule as `can()`."""
    if not actor or not actor.get("actor_id"):
        return []
    sql = permission_sql("view").replace(
        "SELECT d.document_id AS resource",
        "SELECT d.document_id, d.filename, d.doc_type, d.review_status, d.date_added")
    return store.query(sql + " ORDER BY d.date_added DESC LIMIT :limit",
                       {"actor_id": actor["actor_id"], "limit": limit})


def permission_sql(action: str = "view") -> str:
    """The same rule as `can()`, as SQL for Datasette.

    Datasette's core `allow` blocks gate a whole table or database; per-document
    rules need a plugin implementing `permission_resources_sql`, and this is the
    query that hook runs. Generated from one place so the API and the browsing
    surface enforce one rule rather than two copies that drift.
    """
    require_choice(action, ("view", "edit"), "action")
    visibility = "('link-view', 'link-edit')" if action == "view" else "('link-edit')"
    share_clause = "" if action == "view" else " AND s.role = 'editor'"
    return (
        f"-- Documents an actor may {action}. Bind :actor_id.\n"
        "SELECT d.document_id AS resource\n"
        "FROM documents d\n"
        "LEFT JOIN actors a ON a.actor_id = :actor_id\n"
        "WHERE a.actor_id IS NOT NULL\n"
        "  AND (\n"
        "    a.is_admin = 1\n"
        "    OR d.created_by = :actor_id\n"
        f"    OR d.visibility IN {visibility}\n"
        "    OR EXISTS (\n"
        "      SELECT 1 FROM document_shares s\n"
        f"      WHERE s.document_id = d.document_id AND s.actor_id = :actor_id{share_clause}\n"
        "    )\n"
        "  )"
    )
