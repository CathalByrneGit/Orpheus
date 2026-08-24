"""Identity, and who may see which document.

The permission rule exists twice on purpose — once in `can()` for the API, once
as SQL for Datasette's row-level hook — so the last tests here hold the two to
agreeing on the same store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orpheus.auth import (authenticate, can, create_actor, create_token,
                          get_actor, hash_token, permission_sql, require,
                          revoke_token, set_visibility, share_document,
                          unshare_document, upsert_actor, visible_documents)
from orpheus.ingest import ingest
from orpheus.utils import NotFound, OrpheusError, PermissionDenied

PDF = Path(__file__).parent / "fixtures" / "services-agreement.pdf"


@pytest.fixture
def cast(store, tmp_path):
    owner = create_actor(store, "Owner", actor_id="act_owner")
    other = create_actor(store, "Other", actor_id="act_other")
    admin = create_actor(store, "Admin", actor_id="act_admin", is_admin=True)
    document_id = ingest(store, PDF, actor_id=owner,
                         storage_root=tmp_path / "storage")["document_id"]
    return store, document_id, {"owner": owner, "other": other, "admin": admin}


def actor(store, actor_id):
    return get_actor(store, actor_id)


# -- identity ---------------------------------------------------------------

def test_an_external_identity_upserts_rather_than_duplicating(store):
    first = upsert_actor(store, "entra", "ext-1", "Nuala Ryan", "n@dept.ie")
    again = upsert_actor(store, "entra", "ext-1", "Nuala Ryan-Smith", "n@dept.ie")
    assert first == again
    assert store.scalar("SELECT COUNT(*) FROM actors") == 1
    assert get_actor(store, first)["display_name"] == "Nuala Ryan-Smith"


def test_a_provider_that_says_nothing_about_admins_does_not_demote(store):
    # Not every provider has the concept. One that does not must leave an
    # Orpheus promotion standing rather than reset it on every sign-in.
    actor_id = upsert_actor(store, "entra", "ext-1", "Nuala Ryan", is_admin=True)
    upsert_actor(store, "entra", "ext-1", "Nuala Ryan")
    assert get_actor(store, actor_id)["is_admin"] == 1


def test_a_provider_that_does_say_is_the_authority(store):
    # `permission_sql()` can only read the actors row, so a provider that
    # tracks admins has to be able to move that column both ways -- otherwise
    # the API and the browsing surface disagree about who is an administrator.
    actor_id = upsert_actor(store, "entra", "ext-1", "Nuala Ryan", is_admin=True)
    assert get_actor(store, actor_id)["is_admin"] == 1
    upsert_actor(store, "entra", "ext-1", "Nuala Ryan", is_admin=False)
    assert get_actor(store, actor_id)["is_admin"] == 0


def test_only_the_hash_of_a_token_is_stored(store):
    actor_id = create_actor(store, "Ada")
    minted = create_token(store, actor_id, label="cli")
    stored = store.scalar("SELECT token_hash FROM actor_tokens")
    # The database never holds a usable credential.
    assert minted["token"] not in stored
    assert stored == hash_token(minted["token"])


def test_a_token_authenticates_its_actor(store):
    actor_id = create_actor(store, "Ada")
    minted = create_token(store, actor_id)
    assert authenticate(store, minted["token"])["actor_id"] == actor_id


@pytest.mark.parametrize("bad", [None, "", "not-a-token"])
def test_a_bad_token_is_nobody_rather_than_an_error(store, bad):
    # Every failure mode becomes the same 401; distinguishing them in the reply
    # would tell an attacker which tokens exist.
    assert authenticate(store, bad) is None


def test_a_revoked_token_stops_working(store):
    actor_id = create_actor(store, "Ada")
    minted = create_token(store, actor_id)
    revoke_token(store, minted["token_id"])
    assert authenticate(store, minted["token"]) is None


def test_an_expired_token_stops_working(store):
    actor_id = create_actor(store, "Ada")
    minted = create_token(store, actor_id, expires_at="2020-01-01T00:00:00Z")
    assert authenticate(store, minted["token"]) is None


def test_a_token_for_an_unknown_actor_is_refused(store):
    with pytest.raises(NotFound):
        create_token(store, "act_nobody")


# -- permissions ------------------------------------------------------------

def test_the_owner_may_do_everything_to_their_own_document(cast):
    store, document_id, ids = cast
    for action in ("view", "edit", "share", "delete"):
        assert can(store, actor(store, ids["owner"]), document_id, action)


def test_a_stranger_may_do_nothing_to_a_private_document(cast):
    store, document_id, ids = cast
    for action in ("view", "edit", "share", "delete"):
        assert not can(store, actor(store, ids["other"]), document_id, action)


def test_an_administrator_may_do_everything(cast):
    store, document_id, ids = cast
    for action in ("view", "edit", "share", "delete"):
        assert can(store, actor(store, ids["admin"]), document_id, action)


def test_nobody_is_not_an_actor(cast):
    store, document_id, _ = cast
    assert not can(store, None, document_id, "view")
    assert not can(store, {}, document_id, "view")


def test_a_viewer_share_grants_view_and_not_edit(cast):
    store, document_id, ids = cast
    share_document(store, document_id, ids["other"], "viewer", ids["owner"])
    other = actor(store, ids["other"])
    assert can(store, other, document_id, "view")
    assert not can(store, other, document_id, "edit")


def test_an_editor_share_grants_edit(cast):
    store, document_id, ids = cast
    share_document(store, document_id, ids["other"], "editor", ids["owner"])
    assert can(store, actor(store, ids["other"]), document_id, "edit")


def test_a_share_cannot_be_used_to_widen_a_share(cast):
    # Sharing and deleting stay with the owner and administrators.
    store, document_id, ids = cast
    share_document(store, document_id, ids["other"], "editor", ids["owner"])
    other = actor(store, ids["other"])
    assert not can(store, other, document_id, "share")
    assert not can(store, other, document_id, "delete")
    with pytest.raises(PermissionDenied):
        share_document(store, document_id, ids["admin"], "editor", other)


def test_unsharing_takes_the_access_away(cast):
    store, document_id, ids = cast
    share_document(store, document_id, ids["other"], "viewer", ids["owner"])
    unshare_document(store, document_id, ids["other"], ids["owner"])
    assert not can(store, actor(store, ids["other"]), document_id, "view")


def test_link_visibility_opens_a_document_without_naming_anyone(cast):
    store, document_id, ids = cast
    other = actor(store, ids["other"])

    set_visibility(store, document_id, "link-view", ids["owner"])
    assert can(store, other, document_id, "view")
    assert not can(store, other, document_id, "edit")

    set_visibility(store, document_id, "link-edit", ids["owner"])
    assert can(store, other, document_id, "edit")

    set_visibility(store, document_id, "private", ids["owner"])
    assert not can(store, other, document_id, "view")


def test_an_unknown_visibility_is_refused(cast):
    store, document_id, ids = cast
    with pytest.raises(OrpheusError, match="visibility must be one of"):
        set_visibility(store, document_id, "public-to-the-world", ids["owner"])


def test_require_names_the_action_it_refused(cast):
    store, document_id, ids = cast
    with pytest.raises(PermissionDenied, match="Not permitted to edit"):
        require(store, actor(store, ids["other"]), document_id, "edit")


# -- one rule, two consumers ------------------------------------------------

@pytest.mark.parametrize("action", ["view", "edit"])
def test_the_sql_rule_and_the_python_rule_agree(cast, action):
    # The rule is written once in can() and emitted as SQL for Datasette's
    # permission_resources_sql hook. If these two ever disagree, the API and the
    # browsing surface are enforcing different things.
    store, document_id, ids = cast
    share_document(store, document_id, ids["other"], "editor", ids["owner"])

    sql = permission_sql(action)
    for name, actor_id in ids.items():
        allowed_by_sql = {r["resource"] for r in
                          store.query(sql, {"actor_id": actor_id})}
        allowed_by_python = can(store, actor(store, actor_id), document_id, action)
        assert (document_id in allowed_by_sql) == allowed_by_python, (name, action)


def test_visible_documents_uses_the_same_rule(cast):
    store, document_id, ids = cast
    assert [d["document_id"] for d in visible_documents(store, actor(store, ids["owner"]))] \
        == [document_id]
    assert visible_documents(store, actor(store, ids["other"])) == []
    share_document(store, document_id, ids["other"], "viewer", ids["owner"])
    assert [d["document_id"] for d in visible_documents(store, actor(store, ids["other"]))] \
        == [document_id]
