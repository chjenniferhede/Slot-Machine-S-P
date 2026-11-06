"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from slotmachine import storage
from slotmachine.money import Money


@pytest.fixture()
def conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """A fresh, initialised database per test.

    A real file rather than ':memory:' so that WAL mode and the file-backed
    behaviour under test match production.
    """
    db = tmp_path / "test.db"
    c = storage.connect(db)
    storage.init_db(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def player(conn: sqlite3.Connection) -> int:
    """A player with an account and a $200.00 balance."""
    with storage.transaction(conn):
        uid = storage.create_user(conn, "alice", "not-a-real-hash", "player")
        storage.create_account(conn, uid)
        storage.adjust_balance(conn, uid, Money(20_000))
    return uid


def dump_all_text(conn: sqlite3.Connection) -> str:
    """Every value in every table, concatenated.

    Used to assert that a secret -- a card number, a CVV, a password -- appears
    nowhere in storage. It lives in the test suite rather than in the package
    because it interpolates a table name into SQL, which production code is
    never permitted to do; here the names come from sqlite_master, not from
    user input, and SQLite cannot parameterise an identifier.
    """
    out: list[str] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table['name']}"):  # noqa: S608
            out.extend(str(v) for v in tuple(row))
    return "\n".join(out)
