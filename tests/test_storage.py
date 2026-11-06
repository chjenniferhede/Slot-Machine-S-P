"""Storage layer: schema constraints, transactions, and injection resistance."""

from __future__ import annotations

import sqlite3

import pytest

from slotmachine import storage
from slotmachine.money import Money


def test_schema_applies_twice(conn: sqlite3.Connection) -> None:
    storage.init_db(conn)
    storage.init_db(conn)
    assert storage.count_users(conn) == 0


def test_foreign_keys_enforced(conn: sqlite3.Connection) -> None:
    """Without PRAGMA foreign_keys = ON this silently succeeds."""
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction(conn):
            storage.create_account(conn, 9999)


def test_negative_balance_rejected(conn: sqlite3.Connection, player: int) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction(conn):
            storage.adjust_balance(conn, player, Money(-20_001))


def test_role_constraint(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction(conn):
            storage.create_user(conn, "mallory", "h", "superuser")


def test_transaction_rollback(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        with storage.transaction(conn):
            storage.create_user(conn, "bob", "h", "player")
            raise RuntimeError("boom")
    assert storage.count_users(conn) == 0


def test_transaction_rollback_on_keyboardinterrupt(conn: sqlite3.Connection) -> None:
    """KeyboardInterrupt is not an Exception.

    If transaction() caught Exception rather than BaseException, a player could
    press Ctrl-C after seeing a losing reel and keep their stake.
    """
    with pytest.raises(KeyboardInterrupt):
        with storage.transaction(conn):
            storage.create_user(conn, "bob", "h", "player")
            raise KeyboardInterrupt
    assert storage.count_users(conn) == 0


def test_transaction_commits(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        storage.create_user(conn, "bob", "h", "player")
    assert storage.count_users(conn) == 1


def test_username_case_insensitive(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        storage.create_user(conn, "Alice", "h", "player")
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction(conn):
            storage.create_user(conn, "alice", "h", "player")


def test_sql_injection_in_username(conn: sqlite3.Connection, player: int) -> None:
    """The classic payload is stored as a literal string, not executed.

    If any query built SQL by string formatting, this registration would either
    error or match every row on lookup. Instead it is just an odd username.
    """
    payload = "' OR '1'='1"
    with storage.transaction(conn):
        uid = storage.create_user(conn, payload, "h", "player")

    found = storage.get_user_by_username(conn, payload)
    assert found is not None
    assert found["id"] == uid
    assert found["username"] == payload
    # It must not match the pre-existing player.
    assert found["id"] != player
    assert storage.count_users(conn) == 2


def test_sql_injection_drop_table(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        storage.create_user(conn, "x'; DROP TABLE users; --", "h", "player")
    assert storage.count_users(conn) == 1  # table still exists


def test_money_roundtrip(conn: sqlite3.Connection, player: int) -> None:
    assert storage.get_balance(conn, player) == Money(20_000)
    with storage.transaction(conn):
        storage.adjust_balance(conn, player, Money(-1_250))
    assert storage.get_balance(conn, player) == Money(18_750)


def test_spin_constraints(conn: sqlite3.Connection, player: int) -> None:
    with storage.transaction(conn):
        storage.upsert_game(conn, "g", "Game", "{}")
    with pytest.raises(sqlite3.IntegrityError):
        with storage.transaction(conn):
            storage.record_spin(
                conn,
                session_id=None,
                user_id=player,
                game_id="g",
                bet=Money(0),  # bet_cents > 0
                seed=b"\x00" * 32,
                stops_json="[]",
                payout=Money(0),
            )


def test_seed_stored_as_bytes(conn: sqlite3.Connection, player: int) -> None:
    seed = bytes(range(32))
    with storage.transaction(conn):
        storage.upsert_game(conn, "g", "Game", "{}")
        spin_id = storage.record_spin(
            conn,
            session_id=None,
            user_id=player,
            game_id="g",
            bet=Money(100),
            seed=seed,
            stops_json="[1,2,3]",
            payout=Money(500),
        )
    row = conn.execute("SELECT seed FROM spins WHERE id = ?", (spin_id,)).fetchone()
    assert row["seed"] == seed
    assert isinstance(row["seed"], bytes)


def test_list_games_respects_enabled(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        storage.upsert_game(conn, "a", "Game A", "{}", enabled=True)
        storage.upsert_game(conn, "b", "Game B", "{}", enabled=False)
    assert [g["id"] for g in storage.list_games(conn)] == ["a"]
    assert [g["id"] for g in storage.list_games(conn, enabled_only=False)] == ["a", "b"]
