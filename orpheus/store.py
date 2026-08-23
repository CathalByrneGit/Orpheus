"""SQLite, used natively, with two constraints made explicit rather than left
to convention.

**One writer.** SQLite permits exactly one, and with concurrent users that
stops being a theoretical limit. An advisory lock file next to the database
records the owning process, so a second writer fails loudly at open time
instead of being discovered under load.

**WAL from the start**, so readers are never blocked by the writer. That has a
consequence worth knowing before you deploy anything: a reader opened with
SQLite's `immutable=1` skips the WAL entirely and sees the database as of the
last checkpoint, which on a live store means an empty site with no error. See
`docs/deployment.md`.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Iterable, Iterator, Sequence

from .schema import MIGRATIONS
from .utils import OrpheusError, from_json, now, to_json


def writer_lock_path(path: str | os.PathLike) -> Path:
    return Path(str(path) + ".writer.lock")


def _process_alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by someone else
    return True


def acquire_writer_lock(path: str | os.PathLike, force: bool = False) -> Path:
    """Take the advisory single-writer lock, or explain who holds it."""
    lock = writer_lock_path(path)
    if lock.exists():
        holder = from_json(lock.read_text() or "") or {}
        pid = holder.get("pid")
        if _process_alive(pid):
            # Held by this process is a different mistake from held by another,
            # and it is worth saying which. The R implementation allowed the
            # same-process case, because there the API was the one writer and
            # nothing else ran in it. Here the writer is Datasette, where a
            # plugin opening its own Store alongside the application's is an
            # easy and quiet way to end up with two write connections.
            if pid == os.getpid():
                raise OrpheusError(
                    f"This process already holds the writer lock on {path}. "
                    "Reuse the open Store rather than opening a second one."
                )
            raise OrpheusError(
                f"The single-writer lock on {path} is held by pid {pid}. "
                "Only one process may write to the store — send writes through "
                "the running Datasette instead of opening a second writer. "
                "If that process is gone, re-open with force_lock=True."
            )
        if not force:
            raise OrpheusError(
                f"A stale writer lock from pid {pid} remains on {path}. "
                "The owning process is not running. Re-open with "
                "force_lock=True to take over."
            )
    lock.write_text(to_json({"pid": os.getpid(), "acquired_at": now()}) or "")
    return lock


def release_writer_lock(path: str | os.PathLike) -> None:
    lock = writer_lock_path(path)
    if not lock.exists():
        return
    holder = from_json(lock.read_text() or "") or {}
    if holder.get("pid") in (None, os.getpid()):
        lock.unlink(missing_ok=True)


class Store:
    """A connection to the Orpheus store, carrying its own mode.

    `mode="write"` holds the advisory lock and applies migrations;
    `mode="read"` opens read-only and refuses writes before SQLite sees them,
    so a read connection fails with a sentence rather than an opaque error from
    deep inside a statement.
    """

    def __init__(self, path: str | os.PathLike, mode: str = "write",
                 force_lock: bool = False, migrate: bool = True):
        if mode not in ("read", "write"):
            raise OrpheusError("mode must be 'read' or 'write'.")
        self.path = str(path)
        self.mode = mode
        self._tx_depth = 0
        self._holds_lock = False

        parent = Path(self.path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)

        if mode == "write":
            acquire_writer_lock(self.path, force=force_lock)
            self._holds_lock = True
            try:
                # isolation_level=None: transactions are managed explicitly by
                # transaction() below, not by the driver's implicit BEGIN.
                self.conn = sqlite3.connect(self.path, isolation_level=None,
                                            check_same_thread=False)
                self.conn.row_factory = sqlite3.Row
                self.conn.execute("PRAGMA journal_mode = WAL")
                self.conn.execute("PRAGMA foreign_keys = ON")
                self.conn.execute("PRAGMA busy_timeout = 5000")
                self.conn.execute("PRAGMA synchronous = NORMAL")
                if migrate:
                    self.migrate()
            except Exception:
                # A failure here must not leave the lock held on a database
                # nobody is using; that would shut every later attempt out.
                try:
                    self.conn.close()
                except Exception:
                    pass
                release_writer_lock(self.path)
                self._holds_lock = False
                raise
        else:
            if not Path(self.path).exists():
                raise OrpheusError(f"No database at {self.path}.")
            self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True,
                                        isolation_level=None,
                                        check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA busy_timeout = 5000")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            if self._holds_lock:
                release_writer_lock(self.path)
                self._holds_lock = False

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- guards ------------------------------------------------------------

    def assert_writable(self) -> None:
        if self.mode != "write":
            raise OrpheusError(
                "This connection is read-only. All writes go through the "
                "single writer."
            )

    # -- queries -----------------------------------------------------------

    @staticmethod
    def _bind(params: Sequence[Any] | Mapping[str, Any]) -> Any:
        """Positional or named, whichever the caller used.

        The permission rules are written with `:actor_id` appearing several
        times; binding those positionally means counting the occurrences, and
        the count changes when the rule does.
        """
        return params if isinstance(params, Mapping) else tuple(params)

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, self._bind(params))

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, self._bind(params)).fetchall()]

    def one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> dict | None:
        row = self.conn.execute(sql, self._bind(params)).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        row = self.conn.execute(sql, self._bind(params)).fetchone()
        return row[0] if row is not None else None

    def insert(self, table: str, values: dict) -> None:
        self.assert_writable()
        cols = list(values)
        placeholders = ", ".join("?" for _ in cols)
        quoted = ", ".join(f'"{c}"' for c in cols)
        self.conn.execute(
            f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})',
            tuple(values[c] for c in cols),
        )

    def table_exists(self, table: str) -> bool:
        return self.scalar(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ) is not None

    def columns(self, table: str) -> list[str]:
        return [r["name"] for r in self.query(f'PRAGMA table_info("{table}")')]

    # -- transactions ------------------------------------------------------

    @classmethod
    def adopt(cls, conn: sqlite3.Connection, path: str = "",
              owns_transaction: bool = True) -> "Store":
        """Wrap a connection somebody else opened, and may already be driving.

        This exists for the Datasette plugin, which runs inside the process
        that holds the database and is handed a connection off Datasette's own
        write thread. Opening a second connection there would make a second
        writer — the exact thing the whole single-writer design is for — so the
        store borrows the one it is given.

        `owns_transaction=False` says the caller has already issued a BEGIN and
        will COMMIT or ROLLBACK itself. The store then joins that transaction
        instead of starting one (SQLite has no nested BEGIN), and never commits:
        the outer owner decides. Datasette's `execute_write_fn` is exactly that
        caller — it wraps each task in `BEGIN IMMEDIATE`.

        No advisory lock is taken. The lock exists to stop a *second process*
        opening the same file for writing; the borrower is by construction
        already inside the one that did.
        """
        store = cls.__new__(cls)
        store.conn = conn
        store.path = path
        store.mode = "write"
        store._holds_lock = False
        # Depth 1 means "a transaction is already open, joined not owned".
        store._tx_depth = 0 if owns_transaction else 1
        conn.row_factory = sqlite3.Row
        return store

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """Re-entrant transaction.

        Higher-level operations compose lower-level ones — accepting a schema
        amendment registers a bundle, which is itself transactional — and a
        nested BEGIN is an error in SQLite. The outermost call owns the
        transaction and inner calls join it, so the whole operation still
        commits or rolls back as one unit.
        """
        self.assert_writable()
        if self._tx_depth > 0:
            self._tx_depth += 1
            try:
                yield self
            finally:
                self._tx_depth -= 1
            return

        self.conn.execute("BEGIN")
        self._tx_depth = 1
        try:
            yield self
        except Exception:
            self._tx_depth = 0
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            self._tx_depth = 0
            self.conn.execute("COMMIT")

    # -- maintenance -------------------------------------------------------

    def checkpoint(self, mode: str = "PASSIVE") -> None:
        """Fold the WAL back into the main database file.

        Called after writes because readers that cannot see the WAL — anything
        opened immutable, and any tool copying the `.sqlite` file alone —
        otherwise see the store as of the last checkpoint.
        """
        if mode not in ("PASSIVE", "TRUNCATE", "FULL", "RESTART"):
            raise OrpheusError(f"Unknown checkpoint mode {mode!r}.")
        self.conn.execute(f"PRAGMA wal_checkpoint({mode})")

    def migrate(self) -> list[int]:
        self.assert_writable()
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        applied = {r["version"] for r in self.query("SELECT version FROM schema_migrations")}
        run: list[int] = []
        for migration in MIGRATIONS:
            if migration["version"] in applied:
                continue
            with self.transaction():
                for statement in migration.get("statements", ()):
                    self.conn.execute(statement)
                # A data migration cannot always be written as SQL: recomputing
                # a derived column needs the Python that derives it. Running it
                # here, inside the same transaction and recorded in the same
                # table, means it happens exactly once like any other migration
                # rather than becoming a maintenance command someone has to
                # remember.
                if migration.get("run"):
                    migration["run"](self)
                self.conn.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (?, ?, ?)",
                    (migration["version"], migration["name"], now()),
                )
            run.append(migration["version"])
        return run

    # -- org settings ------------------------------------------------------

    def setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM org_settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any, actor_id: str | None = None) -> None:
        self.assert_writable()
        self.conn.execute(
            "INSERT INTO org_settings (key, value, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "updated_by = excluded.updated_by",
            (key, str(value), now(), actor_id),
        )


def connect(path: str | os.PathLike, mode: str = "write",
            force_lock: bool = False, migrate: bool = True) -> Store:
    return Store(path, mode=mode, force_lock=force_lock, migrate=migrate)
