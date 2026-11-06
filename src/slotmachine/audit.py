"""Tamper-evident audit log.

The spec asks for "system logging for auditing purposes". A plain append-only
table meets the letter of that and not the purpose: anyone with write access
can edit or delete a row and leave no trace, and a log an insider can rewrite
is not evidence of anything.

Each entry therefore stores the hash of the entry before it, forming a chain:

    entry 1:  prev_hash = 000...0            hash = H(prev | ts | actor | ...)
    entry 2:  prev_hash = <hash of entry 1>  hash = H(prev | ts | actor | ...)
    entry 3:  prev_hash = <hash of entry 2>  hash = H(prev | ts | actor | ...)

Changing any historical entry changes its hash, which breaks every link after
it. ``verify_chain`` finds the first break.

WHAT THIS DOES NOT DETECT
-------------------------
Truncation of the most recent entries. Deleting the last fifty rows leaves a
chain that is internally consistent, because nothing after them survives to
disagree. Detecting that requires recording the expected length somewhere
outside the chain. Know this limit and state it rather than overclaiming the
control -- the ``id gap`` check below catches deletions in the middle, not at
the end.

The schema also carries BEFORE UPDATE and BEFORE DELETE triggers on this
table, so tampering has to happen outside the application entirely. The
triggers are the lock; this chain is the detector.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from typing import Final

GENESIS_HASH: Final[str] = "0" * 64


class Action:
    """Canonical action names.

    Defined in one place so that Layers 2-5 cannot invent variants -- a log
    that calls the same event ``login_fail`` in one module and
    ``auth.login.failure`` in another cannot be filtered or counted reliably.
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
    """Hash one entry, with every field length-prefixed.

    The prefixes are not decoration. Without them the fields ("ab", "c") and
    ("a", "bc") both concatenate to "abc" and hash identically, so an attacker
    who controls part of a detail string could craft two different entries
    sharing one hash. Length prefixing makes the encoding unambiguous.
    """
    payload = "".join(f"{len(p)}:{p}" for p in (prev_hash, ts, actor, action, detail))
    return hashlib.sha256(payload.encode()).hexdigest()


def write(
    conn: sqlite3.Connection, actor: str, action: str, detail: str = ""
) -> None:
    """Append one entry.

    Must be called inside the caller's transaction, so that the action being
    logged and the record of it commit or roll back together. An audit entry
    for something that did not happen is as bad as a missing one.

    NEVER pass a secret in ``detail``: no passwords, no password hashes, no
    card numbers, no CVVs, no tokens. (CWE-532.)
    """
    from slotmachine import storage  # local import: storage imports nothing here

    ts = storage.utc_now()
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row["hash"] if row is not None else GENESIS_HASH
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, detail, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ts, actor, action, detail, prev, _entry_hash(prev, ts, actor, action, detail)),
    )


def verify_chain(conn: sqlite3.Connection) -> VerifyResult:
    """Walk the chain and report the first break.

    Three distinct failure modes:

    * ``hash mismatch`` -- a field in the row was edited, so the recomputed
      hash no longer matches the stored one.
    * ``broken link``   -- this row's prev_hash does not match the previous
      row's hash, which is what a deletion or a reordering looks like.
    * ``id gap``        -- the id sequence skips, which catches a deletion
      whose neighbours were also rewritten.
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
    # The WHERE clause is assembled from fixed literals above; every value is
    # bound. No caller input reaches the statement text.
    return list(
        conn.execute(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params),
        )
    )
