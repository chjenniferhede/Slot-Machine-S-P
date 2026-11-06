"""Audit log that cannot be quietly edited.

Every entry stores the hash of the entry before it, so the entries form a
chain:

    entry 1:  prev_hash = 000...0
    entry 2:  prev_hash = hash of entry 1
    entry 3:  prev_hash = hash of entry 2

Change an old entry and its hash changes too, which breaks every link after
it. ``verify_chain`` walks the chain and reports the first break.

What it cannot catch: deleting the newest entries. Nothing comes after them to
disagree, so what is left still looks fine.

The database also blocks UPDATE and DELETE on this table. That stops tampering
through the app; the chain catches tampering that goes around it.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from typing import Final

GENESIS_HASH: Final[str] = "0" * 64


class Action:
    """The names of things we log.

    Kept in one place so two modules cannot spell the same event differently,
    which would make the log impossible to filter or count.
    """

    USER_REGISTER: Final[str] = "user.register"
    LOGIN_SUCCESS: Final[str] = "auth.login.success"
    LOGIN_FAILURE: Final[str] = "auth.login.failure"
    LOCKOUT: Final[str] = "auth.lockout"
    PASSWORD_CHANGE: Final[str] = "auth.password_change"
    ACCESS_GRANTED: Final[str] = "access.granted"
    ACCESS_DENIED: Final[str] = "access.denied"
    PAYMENT_METHOD_ADDED: Final[str] = "payment.method_added"
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
    ok: bool
    entries_checked: int
    first_bad_id: int | None = None
    reason: str | None = None


def _entry_hash(prev_hash: str, ts: str, actor: str, action: str, detail: str) -> str:
    """Hash one entry.

    Each field is written as "length:value". Without the lengths, the fields
    ("ab", "c") and ("a", "bc") both join into "abc" and hash the same, so two
    different entries could share a hash.
    """
    payload = "".join(f"{len(p)}:{p}" for p in (prev_hash, ts, actor, action, detail))
    return hashlib.sha256(payload.encode()).hexdigest()


def write(
    conn: sqlite3.Connection, actor: str, action: str, detail: str = ""
) -> None:
    """Add one entry to the end of the log.

    Call this inside the caller's transaction, so the action and the record of
    it are saved or undone together.

    Never put a secret in ``detail`` -- no passwords, card numbers or tokens.
    """
    from slotmachine import storage  # imported here to avoid a circular import

    ts = storage.utc_now()
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row is not None else GENESIS_HASH
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, detail, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts, actor, action, detail, prev, _entry_hash(prev, ts, actor, action, detail)),
    )


def verify_chain(conn: sqlite3.Connection) -> VerifyResult:
    """Check the whole chain and report the first problem found.

    * ``hash mismatch`` -- the row was edited.
    * ``broken link``   -- this row does not point at the one before it.
    * ``id gap``        -- an id is missing, so a row was deleted.
    """
    prev_hash = GENESIS_HASH
    expected_id = 1
    checked = 0

    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        checked += 1

        if row["id"] != expected_id:
            return VerifyResult(False, checked, int(row["id"]), "id gap")

        if not hmac.compare_digest(row["prev_hash"], prev_hash):
            return VerifyResult(False, checked, int(row["id"]), "broken link")

        recomputed = _entry_hash(
            row["prev_hash"], row["ts"], row["actor"], row["action"], row["detail"]
        )
        if not hmac.compare_digest(row["hash"], recomputed):
            return VerifyResult(False, checked, int(row["id"]), "hash mismatch")

        prev_hash = row["hash"]
        expected_id += 1

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
    """Read entries, newest first, with optional filters."""
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

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend((limit, offset))
    # The WHERE text is built only from the fixed strings above. Every value
    # the caller passed is bound with ?, so none of it becomes SQL.
    return list(
        conn.execute(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
    )
