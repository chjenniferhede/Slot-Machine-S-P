"""SQLite storage: connection, schema, transactions, and repository functions.

Three rules hold throughout this module and every module that uses it.

1. EVERY query uses ``?`` bound parameters. Never an f-string, never ``%``,
   never ``.format()``, never concatenation. The driver sends the statement and
   the data over separate channels, so data is never parsed as SQL. The unsafe
   form is syntactically easy in Python, which is exactly why the rule is
   absolute rather than a matter of judgement. (OWASP A03, CWE-89.)

2. Anything that moves money runs inside ``transaction()``. A debit without its
   matching credit is lost money.

3. Repository functions are plain functions taking the connection first. No
   ORM, no session objects, no lazy loading -- a reviewer can follow any single
   query from the call site to the SQL without indirection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import cast

from slotmachine.money import Money

DEFAULT_DB_PATH = "casino.db"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a connection configured the way the rest of the system expects.

    Three settings worth understanding, because each is a real decision:

    ``isolation_level=None``
        Turns off the sqlite3 module's implicit transaction management so we
        issue BEGIN ourselves. The default would open and commit transactions
        at times of its choosing, which is not the same as the boundaries our
        money operations actually need.

    ``PRAGMA foreign_keys = ON``
        SQLite ignores foreign keys by default, for backwards compatibility
        with databases written before it supported them. Without this line the
        REFERENCES clauses in schema.sql do nothing at all. (OWASP A05,
        CWE-1188: an insecure default that must be explicitly overridden.)

    ``PRAGMA journal_mode = WAL``
        Better crash behaviour and readers do not block the writer.
    """
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the schema. Idempotent -- safe on an already-initialised database."""
    sql = resources.files("slotmachine").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(sql)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one atomic unit.

    BEGIN IMMEDIATE rather than plain BEGIN: it takes the write lock up front,
    so a second concurrent writer fails immediately instead of doing work and
    then failing at COMMIT.

    Note the ``except BaseException``. Catching ``Exception`` would be a bug:
    ``KeyboardInterrupt`` does not inherit from it, so a player pressing Ctrl-C
    after seeing a losing reel would leave the transaction open and could keep
    their stake. Interrupts must roll back like anything else.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def utc_now() -> str:
    """Current time as ISO 8601 UTC, e.g. '2026-11-06T18:14:02Z'.

    SQLite has no date type. ISO 8601 in UTC sorts correctly as plain text,
    which is the only property we actually need from a timestamp here. UTC
    throughout: a local-time daily bucket would let a user in the right
    timezone reset their deposit limit twice in one day.
    """
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_bucket() -> str:
    """The UTC calendar date, used to scope daily deposit and withdrawal limits."""
    return datetime.now(timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def create_user(
    conn: sqlite3.Connection,
    username: str,
    pw_hash: str,
    role: str,
    must_change_pw: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO users (username, pw_hash, role, must_change_pw, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (username, pw_hash, role, int(must_change_pw), utc_now()),
    )
    user_id = cur.lastrowid
    assert user_id is not None
    return user_id


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return cast("sqlite3.Row | None", row)


def get_user(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return cast("sqlite3.Row | None", row)


def count_users(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def set_password_hash(conn: sqlite3.Connection, user_id: int, pw_hash: str) -> None:
    conn.execute(
        "UPDATE users SET pw_hash = ?, must_change_pw = 0 WHERE id = ?",
        (pw_hash, user_id),
    )


def record_login_failure(conn: sqlite3.Connection, user_id: int, locked_until: str | None) -> None:
    conn.execute(
        "UPDATE users SET failed_count = failed_count + 1, locked_until = ? WHERE id = ?",
        (locked_until, user_id),
    )


def reset_login_failures(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET failed_count = 0, locked_until = NULL WHERE id = ?",
        (user_id,),
    )


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def create_account(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "INSERT INTO accounts (user_id, balance_cents, day_bucket) VALUES (?, 0, ?)",
        (user_id, today_bucket()),
    )


def get_account(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM accounts WHERE user_id = ?", (user_id,)).fetchone()
    return cast("sqlite3.Row | None", row)


def get_balance(conn: sqlite3.Connection, user_id: int) -> Money:
    row = get_account(conn, user_id)
    if row is None:
        raise StorageError(f"no account for user {user_id}")
    return Money(int(row["balance_cents"]))


def adjust_balance(conn: sqlite3.Connection, user_id: int, delta: Money) -> Money:
    """Move the balance by ``delta`` and return the new balance.

    The read and the write happen in one statement so there is no window
    between checking the balance and changing it (CWE-367, TOCTOU). The
    ``balance_cents >= 0`` CHECK constraint in the schema rejects an overdraft
    even if an application-level check is missing or has been removed.
    """
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ? WHERE user_id = ?",
        (delta.cents, user_id),
    )
    return get_balance(conn, user_id)


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


def upsert_game(
    conn: sqlite3.Connection, game_id: str, name: str, config_json: str, enabled: bool = True
) -> None:
    conn.execute(
        "INSERT INTO games (id, name, config_json, enabled, created_at) VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
        " config_json = excluded.config_json",
        (game_id, name, config_json, int(enabled), utc_now()),
    )


def get_game(conn: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    return cast("sqlite3.Row | None", row)


def list_games(conn: sqlite3.Connection, enabled_only: bool = True) -> list[sqlite3.Row]:
    if enabled_only:
        return list(conn.execute("SELECT * FROM games WHERE enabled = 1 ORDER BY name"))
    return list(conn.execute("SELECT * FROM games ORDER BY name"))


def set_game_enabled(conn: sqlite3.Connection, game_id: str, enabled: bool) -> None:
    conn.execute("UPDATE games SET enabled = ? WHERE id = ?", (int(enabled), game_id))


# ---------------------------------------------------------------------------
# Spins
# ---------------------------------------------------------------------------


def record_spin(
    conn: sqlite3.Connection,
    *,
    session_id: int | None,
    user_id: int,
    game_id: str,
    bet: Money,
    seed: bytes,
    stops_json: str,
    payout: Money,
    is_hold_respin: bool = False,
    is_test: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO spins (session_id, user_id, game_id, bet_cents, seed, stops_json,"
        " payout_cents, is_hold_respin, is_test, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            session_id,
            user_id,
            game_id,
            bet.cents,
            seed,
            stops_json,
            payout.cents,
            int(is_hold_respin),
            int(is_test),
            utc_now(),
        ),
    )
    spin_id = cur.lastrowid
    assert spin_id is not None
    return spin_id


class StorageError(Exception):
    """A storage-layer invariant was violated."""
