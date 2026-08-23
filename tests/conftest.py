"""Shared fixtures.

Every test runs against a real SQLite store with the real schema. Only the
model and the population engine are ever doubled, because those are the two
things that need a network and a GPU; WAL, transactions, the permission rules
and the concept SQL are all exercised as they actually run.
"""

from __future__ import annotations

import pytest

from orpheus.store import connect


@pytest.fixture
def store(tmp_path):
    s = connect(tmp_path / "orpheus.sqlite")
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "orpheus.sqlite"
