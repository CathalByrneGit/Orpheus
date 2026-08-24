"""The seam between Datasette's idea of a person and Orpheus's.

Datasette answers "who is this"; Orpheus answers "what may they see". These
tests hold the join to three things: an identity is provisioned once and only
once, the `actors` row is the authority for `is_admin`, and a deployment can
still pin a person to an actor id that already exists.

The Datasette `Database` object is stood in for -- it is a queue in front of a
connection, and the queue is not what is under test -- but the store, the
schema and `auth` are all the real ones.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib

from orpheus.auth import create_actor, get_actor

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "orpheus_datasette_under_test", ROOT / "plugins" / "orpheus_datasette.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()


class FakeDatasette:
    def __init__(self, **config):
        self._config = config

    def plugin_config(self, name):
        return self._config


class FakeRequest:
    def __init__(self, actor):
        self.actor = actor


class FakeDatabase:
    """Datasette's Database, minus the write thread.

    `execute_write_fn` opens its own transaction before calling, which is why
    the plugin adopts the connection with `owns_transaction=False`; that is
    reproduced here so the plugin is exercised under the nesting it really
    meets.
    """

    def __init__(self, store):
        self.store = store
        self.path = store.path
        self.writes = 0

    async def execute_fn(self, fn):
        return fn(self.store.conn)

    async def execute_write_fn(self, fn):
        self.writes += 1
        self.store.conn.execute("BEGIN IMMEDIATE")
        try:
            result = fn(self.store.conn)
        except BaseException:
            self.store.conn.rollback()
            raise
        self.store.conn.commit()
        return result


def identity_for(actor, **config):
    return plugin._datasette_identity(FakeDatasette(**config), FakeRequest(actor))


def resolve(database, identity):
    """Drive the plugin's one coroutine to completion.

    Rather than take a pytest-asyncio dependency: it only ever awaits the
    database, and the database is stood in for here, so there is no event loop
    behaviour left to test.
    """
    return asyncio.run(plugin._resolve_actor(database, identity))


# -- reading the identity ---------------------------------------------------

def test_no_datasette_actor_means_no_identity():
    assert identity_for(None) is None


def test_a_username_stands_in_for_a_missing_display_name():
    # datasette-accounts sends `username`; other providers send `name`.
    assert identity_for({"id": "u1", "username": "nuala"})["display_name"] == "nuala"
    assert identity_for({"id": "u1", "name": "Nuala Ryan"})["display_name"] == "Nuala Ryan"
    assert identity_for({"id": "u1"})["display_name"] == "u1"


def test_a_provider_with_no_opinion_on_admins_says_nothing_rather_than_no():
    # The distinction matters: False would demote someone promoted inside
    # Orpheus every time they signed in.
    assert identity_for({"id": "u1"})["is_admin"] is None
    assert identity_for({"id": "u1", "is_admin": False})["is_admin"] is False
    assert identity_for({"id": "u1", "is_admin": True})["is_admin"] is True


def test_root_and_the_configured_admin_are_administrators():
    assert identity_for({"id": "root"})["is_admin"] is True
    assert identity_for({"id": "u1"}, admin_id="u1")["is_admin"] is True
    # Configuring an admin does not demote everyone else.
    assert identity_for({"id": "u2"}, admin_id="u1")["is_admin"] is None


# -- provisioning -----------------------------------------------------------

def test_an_unknown_identity_is_provisioned_once(store):
    database = FakeDatabase(store)
    identity = identity_for({"id": "u1", "username": "nuala", "is_admin": False})

    first = resolve(database, identity)
    assert database.writes == 1
    assert get_actor(store, first["actor_id"])["display_name"] == "nuala"

    again = resolve(database, identity)
    assert again["actor_id"] == first["actor_id"]
    # Signing in again is a read, not a write: no second row, no second write.
    assert database.writes == 1
    assert store.scalar("SELECT COUNT(*) FROM actors") == 1


def test_the_same_username_under_two_providers_is_two_people(store):
    database = FakeDatabase(store)
    entra = resolve(database, identity_for({"id": "nuala"}, idp="entra"))
    local = resolve(database, identity_for({"id": "nuala"}, idp="datasette"))
    assert entra["actor_id"] != local["actor_id"]


def test_a_renamed_person_keeps_the_rows_they_created(store):
    database = FakeDatabase(store)
    before = resolve(database, identity_for({"id": "u1", "username": "nuala"}))
    after = resolve(database, identity_for({"id": "u1", "username": "nuala.ryan"}))
    assert after["actor_id"] == before["actor_id"]
    assert get_actor(store, after["actor_id"])["display_name"] == "nuala.ryan"


# -- who is an administrator ------------------------------------------------

def test_the_provider_promotes_and_demotes_the_actors_row(store):
    # permission_sql() can only read the row, so the row has to follow the
    # provider or the API and the browsing surface disagree about admins.
    database = FakeDatabase(store)
    promoted = resolve(database, identity_for({"id": "u1", "is_admin": True}))
    assert promoted["is_admin"] is True
    assert get_actor(store, promoted["actor_id"])["is_admin"] == 1

    demoted = resolve(database, identity_for({"id": "u1", "is_admin": False}))
    assert demoted["actor_id"] == promoted["actor_id"]
    assert demoted["is_admin"] is False
    assert get_actor(store, demoted["actor_id"])["is_admin"] == 0


def test_a_silent_provider_leaves_an_orpheus_promotion_alone(store):
    database = FakeDatabase(store)
    resolved = resolve(database, identity_for({"id": "u1"}))
    store.execute("UPDATE actors SET is_admin = 1 WHERE actor_id = ?",
                  (resolved["actor_id"],))
    store.conn.commit()

    again = resolve(database, identity_for({"id": "u1"}))
    assert again["is_admin"] is True


# -- pinning ----------------------------------------------------------------

def test_a_pinned_identity_lands_on_the_actor_it_names(store):
    existing = create_actor(store, "Demo", is_admin=True)
    store.conn.commit()
    database = FakeDatabase(store)

    resolved = resolve(database, identity_for({"id": "u1", "is_admin": False},
                                              actor_map={"u1": existing}))
    assert resolved["actor_id"] == existing
    # The pin is a deployment decision, so Orpheus's own row governs it and the
    # provider's admin flag does not overwrite it.
    assert resolved["is_admin"] is True
    assert database.writes == 0
    assert store.scalar("SELECT COUNT(*) FROM actors") == 1


def test_a_pin_naming_a_missing_actor_creates_it_rather_than_dangling(store):
    # documents.created_by references this row, so a typo'd pin must not
    # produce writes attributed to an actor that does not exist.
    database = FakeDatabase(store)
    resolved = resolve(database, identity_for({"id": "u1", "username": "nuala"},
                                              actor_map={"u1": "act_pinned"}))
    assert resolved["actor_id"] == "act_pinned"
    assert get_actor(store, "act_pinned")["display_name"] == "nuala"
