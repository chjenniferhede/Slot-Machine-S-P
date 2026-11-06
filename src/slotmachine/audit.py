"""Audit log that cannot be quietly edited.

Records security-relevant events: logins, money moving, spins, games being
turned on and off. Every entry also stores the hash of the entry before it, so
the entries form a chain. Editing an old entry changes its hash and breaks
every link after it, which ``verify_chain`` will find and report.

It cannot catch deletion of the newest entries, because nothing is left after
them to disagree. The database separately refuses UPDATE and DELETE on this
table, so tampering has to go around the program entirely.
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
    """The names of the events we log.

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
    """What a check of the chain found.

    ok
        True if the whole chain is intact.
    entries_checked
        How many entries were looked at. On failure this is how far the check
        got before stopping.
    first_bad_id
        The id of the first bad entry, or None if the chain is intact.
    reason
        Why that entry is bad, or None if the chain is intact.
    """

    ok: bool
    entries_checked: int
    first_bad_id: int | None = None
    reason: str | None = None


def _entry_hash(
    previous_hash: str, timestamp: str, actor: str, action: str, detail: str
) -> str:
    """Return the hash of one entry, as 64 hex characters.

    The five fields are joined as "length:value" before hashing. Without the
    lengths, the fields ("ab", "c") and ("a", "bc") would both join into "abc"
    and hash the same, letting two different entries share a hash.
    """
    fields = [previous_hash, timestamp, actor, action, detail]

    payload = ""
    for field in fields:
        payload = payload + str(len(field)) + ":" + field

    return hashlib.sha256(payload.encode()).hexdigest()


def write(
    conn: sqlite3.Connection, actor: str, action: str, detail: str = ""
) -> None:
    """Add one entry to the end of the log, stamped with the current UTC time.

    ``actor`` is the username responsible, ``action`` is one of the names on
    :class:`Action`, and ``detail`` is free text describing what happened,
    such as an amount or a game name.

    Call this inside the caller's transaction, so that the action and the
    record of it are saved together or undone together.

    Never put a secret in ``detail`` -- no passwords, card numbers or tokens.
    """
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
    """Check every entry in order and stop at the first problem.

    Returns a :class:`VerifyResult`. If the chain is intact, ``ok`` is True
    and ``entries_checked`` is the number of entries. Otherwise ``ok`` is
    False and ``reason`` is one of:

    ``id gap``
        An id is missing, so an entry was deleted.
    ``broken link``
        This entry does not point at the one before it, so entries were
        deleted or reordered.
    ``hash mismatch``
        This entry's own fields were edited after it was written.
    """
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
    """Return entries, newest first.

    Each filter that is given narrows the result, and filters left as None are
    ignored. ``actor`` and ``action`` must match exactly. ``since`` and
    ``until`` are UTC timestamps in the same format as the stored ones and are
    both inclusive.

    ``limit`` caps how many entries come back and ``offset`` skips that many
    first, so pages of 100 are read with offset 0, 100, 200 and so on.
    """
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
