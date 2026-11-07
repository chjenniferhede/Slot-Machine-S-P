"""Saving and loading data with SQLite.

Every query uses ``?`` placeholders for values, never string joining, 
and anything that moves money runs inside ``transaction()``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
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
    """Open the database with the settings this system needs.

    Foreign keys have to be switched on explicitly; SQLite ignores them
    otherwise. Transactions are handled by ``transaction()``, not by Python.
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
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    """Run a block of work so it either all happens or none of it does.

    Catches BaseException, not Exception, so that Ctrl-C mid-spin rolls back
    instead of letting a player cancel a losing bet.
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
    """The time now, as text like '2026-11-06T18:14:02Z'. Always UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def today_bucket() -> str:
    """Today's date in UTC, used to reset daily deposit and withdrawal limits."""
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
    """Change the balance by ``delta`` and return the new balance.

    The schema refuses a negative balance, so an overdraft fails here even
    if a check in the calling code is missing.
    """
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ? WHERE user_id = ?",
        (delta.cents, user_id),
    )
    return get_balance(conn, user_id)


def roll_over_daily_totals(conn: sqlite3.Connection, user_id: int, today: str) -> None:
    """Start new daily deposit and withdrawal totals if the UTC date has changed."""
    conn.execute(
        "UPDATE accounts SET daily_deposit_cents = 0, daily_withdraw_cents = 0, day_bucket = ?"
        " WHERE user_id = ? AND day_bucket <> ?",
        (today, user_id, today),
    )


def record_deposit(conn: sqlite3.Connection, user_id: int, amount: Money) -> Money:
    """Add a deposit to the balance and to today's deposit total. Returns the new balance."""
    _require_positive(amount)
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents + ?,"
        " daily_deposit_cents = daily_deposit_cents + ? WHERE user_id = ?",
        (amount.cents, amount.cents, user_id),
    )
    return get_balance(conn, user_id)


def record_withdrawal(conn: sqlite3.Connection, user_id: int, amount: Money) -> Money:
    """Take a withdrawal off the balance and add it to today's total. Returns the new balance.

    The schema refuses a negative balance, so an overdraft fails here even if
    the caller forgot to check.
    """
    _require_positive(amount)
    conn.execute(
        "UPDATE accounts SET balance_cents = balance_cents - ?,"
        " daily_withdraw_cents = daily_withdraw_cents + ? WHERE user_id = ?",
        (amount.cents, amount.cents, user_id),
    )
    return get_balance(conn, user_id)


def _require_positive(amount: Money) -> None:
    # A negative deposit is a withdrawal that skips the withdrawal checks.
    # accounts.py refuses one first; this is the second line.
    if not amount.is_positive():
        raise StorageError("a deposit or withdrawal must be a positive amount")


# ---------------------------------------------------------------------------
# Payment methods
# ---------------------------------------------------------------------------
# Every lookup takes the owner's user_id as well as the method id. Finding a
# method by its id alone would let one player use or remove another's card
# by guessing ids (IDOR, CWE-639).


def insert_payment_method(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    token: str,
    last4: str,
    brand: str,
    exp_month: int,
    exp_year: int,
) -> int:
    cur = conn.execute(
        "INSERT INTO payment_methods (user_id, token, last4, brand, exp_month, exp_year, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, token, last4, brand, exp_month, exp_year, utc_now()),
    )
    method_id = cur.lastrowid
    assert method_id is not None
    return method_id


def get_payment_method(
    conn: sqlite3.Connection, user_id: int, method_id: int
) -> sqlite3.Row | None:
    """One of this user's methods, or None if they have no method with that id."""
    row = conn.execute(
        "SELECT * FROM payment_methods WHERE id = ? AND user_id = ?", (method_id, user_id)
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def list_payment_methods(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute("SELECT * FROM payment_methods WHERE user_id = ? ORDER BY id", (user_id,))
    )


def delete_payment_method(conn: sqlite3.Connection, user_id: int, method_id: int) -> bool:
    """Remove one of this user's methods. False if they have no method with that id."""
    cur = conn.execute(
        "DELETE FROM payment_methods WHERE id = ? AND user_id = ?", (method_id, user_id)
    )
    return cur.rowcount == 1


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
# Game sessions
# ---------------------------------------------------------------------------
# A session is one visit to a machine. Like payment methods, it is looked up
# by its id together with its owner's user_id.


def create_session(
    conn: sqlite3.Connection, user_id: int, game_id: str, credits: Money
) -> int:
    """Open a session. The schema allows only one open session per user."""
    cur = conn.execute(
        "INSERT INTO sessions (user_id, game_id, credits_cents, started_at) VALUES (?, ?, ?, ?)",
        (user_id, game_id, credits.cents, utc_now()),
    )
    session_id = cur.lastrowid
    assert session_id is not None
    return session_id


def get_session(conn: sqlite3.Connection, user_id: int, session_id: int) -> sqlite3.Row | None:
    """One of this user's sessions, open or closed, or None."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (session_id, user_id)
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def get_open_session(conn: sqlite3.Connection, user_id: int) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT * FROM sessions WHERE user_id = ? AND ended_at IS NULL", (user_id,)
    ).fetchone()
    return cast("sqlite3.Row | None", row)


def list_open_sessions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY id"))


def adjust_credits(conn: sqlite3.Connection, session_id: int, delta: Money) -> Money:
    """Change an open session's credits by ``delta`` and return the new credits.

    The schema refuses negative credits, so betting more than is there fails
    here even if a check in the calling code is missing.
    """
    cur = conn.execute(
        "UPDATE sessions SET credits_cents = credits_cents + ? WHERE id = ? AND ended_at IS NULL",
        (delta.cents, session_id),
    )
    if cur.rowcount != 1:
        raise StorageError(f"session {session_id} is not open")
    row = conn.execute("SELECT credits_cents FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return Money(int(row["credits_cents"]))


def close_session(conn: sqlite3.Connection, session_id: int) -> None:
    """Mark a session ended with no credits left in it.

    The caller moves the credits to the account first, in the same
    transaction. Leaving them here as well would count the money twice.
    """
    cur = conn.execute(
        "UPDATE sessions SET credits_cents = 0, ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (utc_now(), session_id),
    )
    if cur.rowcount != 1:
        raise StorageError(f"session {session_id} is not open")


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


def recent_spins(conn: sqlite3.Connection, session_id: int, limit: int) -> list[sqlite3.Row]:
    """A session's latest spins, newest first."""
    return list(
        conn.execute(
            "SELECT * FROM spins WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        )
    )


def session_totals(conn: sqlite3.Connection, session_id: int) -> tuple[int, Money, Money]:
    """Spins played, total staked and total paid out in one session."""
    row = conn.execute(
        "SELECT COUNT(*) AS spins, COALESCE(SUM(bet_cents), 0) AS staked,"
        " COALESCE(SUM(payout_cents), 0) AS paid FROM spins WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row["spins"]), Money(int(row["staked"])), Money(int(row["paid"]))


class StorageError(Exception):
    """A storage-layer invariant was violated."""
