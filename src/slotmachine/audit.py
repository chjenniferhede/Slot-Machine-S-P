"""Audit log that cannot be quietly edited.

Records logins, money moving, spins and games being turned on and off. Each
entry stores the hash of the entry before it, so editing an old one breaks
every link after it and ``verify_chain`` finds it. Deleting the newest
entries is not detectable.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from typing import Final

#: Stands in for the previous hash of the very first entry, which has none.
GENESIS_HASH: Final[str] = "0" * 64


class Action:
    """Event names, in one place so two modules cannot spell one differently."""

    USER_REGISTER: Final[str] = "user.register"
    LOGIN_SUCCESS: Final[str] = "auth.login.success"
    LOGIN_FAILURE: Final[str] = "auth.login.failure"
    LOCKOUT: Final[str] = "auth.lockout"
    PASSWORD_CHANGE: Final[str] = "auth.password_change"
    ACCESS_GRANTED: Final[str] = "access.granted"
    ACCESS_DENIED: Final[str] = "access.denied"
    PAYMENT_METHOD_ADDED: Final[str] = "payment.method_added"
    PAYMENT_METHOD_REMOVED: Final[str] = "payment.method_removed"
    DEPOSIT: Final[str] = "account.deposit"
    WITHDRAW: Final[str] = "account.withdraw"
    SESSION_BUYIN: Final[str] = "session.buyin"
    SESSION_CASHOUT: Final[str] = "session.cashout"
    SPIN: Final[str] = "game.spin"
    GAME_ENABLED: Final[str] = "game.enabled"
    GAME_DISABLED: Final[str] = "game.disabled"
    TEST_SESSION: Final[str] = "test.session"


@dataclass(frozen=True)
class VerifyResult:
    """What a check of the chain found. The last two are None if it is intact."""

    ok: bool
    entries_checked: int
    first_bad_id: int | None = None
    reason: str | None = None


def _entry_hash(
    previous_hash: str, timestamp: str, actor: str, action: str, detail: str
) -> str:
    """Return the hash of one entry, as 64 hex characters."""
    fields = [previous_hash, timestamp, actor, action, detail]

    payload = ""
    for field in fields:
        payload = payload + str(len(field)) + ":" + field

    return hashlib.sha256(payload.encode()).hexdigest()


def write(
    conn: sqlite3.Connection, actor: str, action: str, detail: str = ""
) -> None:
    """Add one entry to the end of the log, stamped with the current UTC time."""
    from slotmachine import storage  # imported here to avoid a circular import

    timestamp = storage.utc_now()

    last_entry = conn.execute(
        "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if last_entry is None:
        previous_hash = GENESIS_HASH
    else:
        previous_hash = last_entry["hash"]

    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, detail, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            timestamp,
            actor,
            action,
            detail,
            previous_hash,
            _entry_hash(previous_hash, timestamp, actor, action, detail),
        ),
    )


def verify_chain(conn: sqlite3.Connection) -> VerifyResult:
    """Check every entry in order and stop at the first problem found."""
    expected_previous_hash = GENESIS_HASH
    expected_id = 1
    checked = 0

    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        checked = checked + 1

        if row["id"] != expected_id:
            return VerifyResult(False, checked, int(row["id"]), "id gap")

        if not hmac.compare_digest(row["prev_hash"], expected_previous_hash):
            return VerifyResult(False, checked, int(row["id"]), "broken link")

        recomputed = _entry_hash(
            row["prev_hash"], row["ts"], row["actor"], row["action"], row["detail"]
        )
        if not hmac.compare_digest(row["hash"], recomputed):
            return VerifyResult(False, checked, int(row["id"]), "hash mismatch")

        expected_previous_hash = row["hash"]
        expected_id = expected_id + 1

    return VerifyResult(True, checked)


def read(
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Return entries, newest first. Filters left as None are ignored."""
    clauses: list[str] = []
    params: list[object] = []

    if actor is not None:
        clauses.append("actor = ?")
        params.append(actor)
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until)

    # Only the fixed strings above go into the query text. Every value the
    # caller passed is bound with ?, so none of it becomes SQL.
    query = "SELECT * FROM audit_log"
    if clauses:
        query = query + " WHERE " + " AND ".join(clauses)
    query = query + " ORDER BY id DESC LIMIT ? OFFSET ?"

    params.append(limit)
    params.append(offset)

    return list(conn.execute(query, tuple(params)))
