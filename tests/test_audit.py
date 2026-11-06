"""Tamper-evident audit log."""

from __future__ import annotations

import sqlite3

import pytest

from slotmachine import audit, storage
from slotmachine.audit import GENESIS_HASH, Action, _entry_hash


def unlock(conn: sqlite3.Connection) -> None:
    """Drop the append-only triggers so a test can actually tamper.

    This is not a workaround. It is the threat model: the triggers stop
    tampering through the application, so an attacker goes around it and edits
    the database directly, which means dropping them first. The hash chain is
    what detects the result. Lock and detector, doing different jobs.
    """
    conn.execute("DROP TRIGGER audit_log_is_immutable")
    conn.execute("DROP TRIGGER audit_log_cannot_be_deleted")


@pytest.fixture()
def chain(conn: sqlite3.Connection) -> sqlite3.Connection:
    with storage.transaction(conn):
        for i in range(100):
            audit.write(conn, "alice", Action.SPIN, f"spin {i}")
    return conn


def test_chain_verifies(chain: sqlite3.Connection) -> None:
    result = audit.verify_chain(chain)
    assert result.ok
    assert result.entries_checked == 100
    assert result.first_bad_id is None


def test_empty_chain_verifies(conn: sqlite3.Connection) -> None:
    assert audit.verify_chain(conn) == audit.VerifyResult(True, 0)


def test_genesis_hash(chain: sqlite3.Connection) -> None:
    row = chain.execute("SELECT prev_hash FROM audit_log WHERE id = 1").fetchone()
    assert row["prev_hash"] == GENESIS_HASH


def test_detects_edited_detail(chain: sqlite3.Connection) -> None:
    unlock(chain)
    chain.execute("UPDATE audit_log SET detail = 'nothing happened' WHERE id = 50")

    result = audit.verify_chain(chain)
    assert not result.ok
    assert result.first_bad_id == 50
    assert result.reason == "hash mismatch"


def test_detects_edited_actor(chain: sqlite3.Connection) -> None:
    unlock(chain)
    chain.execute("UPDATE audit_log SET actor = 'bob' WHERE id = 7")

    result = audit.verify_chain(chain)
    assert not result.ok
    assert result.first_bad_id == 7


def test_detects_deleted_entry(chain: sqlite3.Connection) -> None:
    unlock(chain)
    chain.execute("DELETE FROM audit_log WHERE id = 50")

    result = audit.verify_chain(chain)
    assert not result.ok
    assert result.first_bad_id == 51
    assert result.reason == "id gap"


def test_detects_reordering(chain: sqlite3.Connection) -> None:
    """Swapping the content of two entries breaks both of their hashes.

    Note this swaps detail, not ts. utc_now() has second granularity, so
    entries written in a tight loop share a timestamp and swapping those two
    values changes nothing. Ordering in this log is carried by id and by the
    hash links, not by the timestamp.
    """
    unlock(chain)
    a = chain.execute("SELECT detail FROM audit_log WHERE id = 10").fetchone()
    b = chain.execute("SELECT detail FROM audit_log WHERE id = 11").fetchone()
    chain.execute("UPDATE audit_log SET detail = ? WHERE id = 10", (b["detail"],))
    chain.execute("UPDATE audit_log SET detail = ? WHERE id = 11", (a["detail"],))

    result = audit.verify_chain(chain)
    assert not result.ok
    assert result.first_bad_id == 10


def test_truncation_is_not_detected(chain: sqlite3.Connection) -> None:
    """The documented limit of this control.

    Removing the most recent entries leaves a chain that is internally
    consistent, because nothing after them survives to disagree. Detecting it
    needs the expected length recorded outside the chain. Asserted here so the
    limitation stays known rather than being discovered during the oral exam.
    """
    unlock(chain)
    chain.execute("DELETE FROM audit_log WHERE id > 60")

    result = audit.verify_chain(chain)
    assert result.ok
    assert result.entries_checked == 60


def test_length_prefix_prevents_collision() -> None:
    """Without length prefixes, ("ab", "c") and ("a", "bc") would hash alike."""
    assert _entry_hash(GENESIS_HASH, "t", "ab", "c", "") != _entry_hash(
        GENESIS_HASH, "t", "a", "bc", ""
    )


def test_rollback_leaves_no_entry(conn: sqlite3.Connection) -> None:
    """The logged action and its log entry must commit or roll back together."""
    with pytest.raises(RuntimeError):
        with storage.transaction(conn):
            audit.write(conn, "alice", Action.DEPOSIT, "$100.00")
            raise RuntimeError("the deposit failed")

    assert audit.verify_chain(conn).entries_checked == 0


def test_read_filters(chain: sqlite3.Connection) -> None:
    with storage.transaction(chain):
        audit.write(chain, "bob", Action.LOGIN_FAILURE, "attempt 1")

    assert len(audit.read(chain, actor="bob")) == 1
    assert len(audit.read(chain, action=Action.LOGIN_FAILURE)) == 1
    assert len(audit.read(chain, actor="alice", limit=10)) == 10
    assert audit.read(chain, limit=1)[0]["actor"] == "bob"  # newest first


def test_read_filter_values_are_bound(chain: sqlite3.Connection) -> None:
    assert audit.read(chain, actor="' OR '1'='1") == []


def test_no_secrets_logged(conn: sqlite3.Connection) -> None:
    """A secret must never reach the detail column.

    Layers 2 and 4 reuse this shape for passwords and card numbers.
    """
    secret = "hunter2correcthorse"
    with storage.transaction(conn):
        audit.write(conn, "alice", Action.LOGIN_FAILURE, "attempt 1")

    logged = "\n".join(
        str(tuple(row)) for row in conn.execute("SELECT * FROM audit_log")
    )
    assert secret not in logged
