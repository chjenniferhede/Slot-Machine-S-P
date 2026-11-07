"""Balance, deposits and withdrawals."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from conftest import StubSession, make_player

from slotmachine import accounts, audit, payment, storage
from slotmachine.accounts import DAILY_DEPOSIT_LIMIT, DAILY_WITHDRAW_LIMIT, AccountError
from slotmachine.audit import Action
from slotmachine.money import Money
from slotmachine.payment import CardInput, StoredMethod

FUTURE_YEAR = datetime.now(timezone.utc).year + 3


def visa() -> CardInput:
    return CardInput(number="4539148803436467", exp_month=12, exp_year=FUTURE_YEAR, cvv="739")


def daily_totals(conn: sqlite3.Connection, user_id: int) -> tuple[int, int]:
    row = storage.get_account(conn, user_id)
    assert row is not None
    return int(row["daily_deposit_cents"]), int(row["daily_withdraw_cents"])


@pytest.fixture()
def alice_card(conn: sqlite3.Connection, alice: StubSession) -> StoredMethod:
    """A Visa card for alice, who starts with $200.00."""
    return payment.add_payment_method(alice, conn, visa())


# ---------------------------------------------------------------------------
# The normal path
# ---------------------------------------------------------------------------


def test_get_balance(conn: sqlite3.Connection, alice: StubSession) -> None:
    assert accounts.get_balance(alice, conn) == Money(20_000)


def test_deposit_increases_balance(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    new_balance = accounts.deposit(alice, conn, alice_card.id, Money(5_000))

    assert new_balance == Money(25_000)
    assert accounts.get_balance(alice, conn) == Money(25_000)
    assert daily_totals(conn, alice.user_id) == (5_000, 0)


def test_withdraw_decreases_balance(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    new_balance = accounts.withdraw(alice, conn, alice_card.id, Money(7_550))

    assert new_balance == Money(12_450)
    assert accounts.get_balance(alice, conn) == Money(12_450)
    assert daily_totals(conn, alice.user_id) == (0, 7_550)


def test_movements_are_audited(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    accounts.deposit(alice, conn, alice_card.id, Money(5_000))
    accounts.withdraw(alice, conn, alice_card.id, Money(1_234))

    [deposit] = audit.read(conn, action=Action.DEPOSIT)
    [withdrawal] = audit.read(conn, action=Action.WITHDRAW)
    method = f"method={alice_card.id}"
    assert deposit["detail"] == f"{method} amount=$50.00 balance=$250.00"
    assert withdrawal["detail"] == f"{method} amount=$12.34 balance=$237.66"
    assert deposit["actor"] == withdrawal["actor"] == "alice"
    assert audit.verify_chain(conn).ok


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("move", [accounts.deposit, accounts.withdraw])
def test_negative_amount_rejected(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod, move: object
) -> None:
    """The important one. A deposit of -$100 is a withdrawal that skips the
    withdrawal limit, and a withdrawal of -$100 is a deposit that skips the
    deposit limit."""
    with pytest.raises(AccountError, match="more than"):
        move(alice, conn, alice_card.id, Money(-10_000))  # type: ignore[operator]

    assert accounts.get_balance(alice, conn) == Money(20_000)
    assert daily_totals(conn, alice.user_id) == (0, 0)


@pytest.mark.parametrize("move", [accounts.deposit, accounts.withdraw])
def test_zero_rejected(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod, move: object
) -> None:
    with pytest.raises(AccountError, match="more than"):
        move(alice, conn, alice_card.id, Money(0))  # type: ignore[operator]


@pytest.mark.parametrize("amount", [5_000, "50.00", 50.0, None])
def test_amount_must_be_money(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod, amount: object
) -> None:
    """A bare number has no unit, and a float cannot hold cents exactly."""
    with pytest.raises(AccountError, match="enter an amount"):
        accounts.deposit(alice, conn, alice_card.id, amount)  # type: ignore[arg-type]


@pytest.mark.parametrize("method_id", ["1", 1.0, True, None])
def test_method_id_must_be_a_whole_number(
    conn: sqlite3.Connection, alice: StubSession, method_id: object
) -> None:
    with pytest.raises(AccountError, match="by its number"):
        accounts.deposit(alice, conn, method_id, Money(100))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Balance and limits
# ---------------------------------------------------------------------------


def test_overdraft_rejected(conn: sqlite3.Connection) -> None:
    """Withdrawing $100.01 from $100.00 fails; withdrawing $100.00 does not."""
    bob = make_player(conn, "bob")
    card = payment.add_payment_method(bob, conn, visa())
    accounts.deposit(bob, conn, card.id, Money(10_000))

    with pytest.raises(AccountError, match=r"only \$100\.00"):
        accounts.withdraw(bob, conn, card.id, Money(10_001))
    assert accounts.get_balance(bob, conn) == Money(10_000)

    assert accounts.withdraw(bob, conn, card.id, Money(10_000)) == Money(0)


def test_daily_deposit_limit(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    with pytest.raises(AccountError, match="daily deposit limit"):
        accounts.deposit(alice, conn, alice_card.id, DAILY_DEPOSIT_LIMIT + Money(1))

    accounts.deposit(alice, conn, alice_card.id, Money(150_000))
    accounts.deposit(alice, conn, alice_card.id, Money(50_000))  # exactly at the limit

    with pytest.raises(AccountError, match=r"\$0\.00 more today"):
        accounts.deposit(alice, conn, alice_card.id, Money(1))

    assert accounts.get_balance(alice, conn) == Money(220_000)
    assert daily_totals(conn, alice.user_id) == (200_000, 0)


def test_daily_withdraw_limit(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    accounts.deposit(alice, conn, alice_card.id, Money(200_000))  # $2,200.00 in the account
    accounts.withdraw(alice, conn, alice_card.id, DAILY_WITHDRAW_LIMIT)

    # $200.00 is still in the account, but today's withdrawals are used up.
    with pytest.raises(AccountError, match="daily withdrawal limit"):
        accounts.withdraw(alice, conn, alice_card.id, Money(1))
    assert accounts.get_balance(alice, conn) == Money(20_000)


def test_limit_resets_next_day(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    accounts.deposit(alice, conn, alice_card.id, DAILY_DEPOSIT_LIMIT)
    with pytest.raises(AccountError):
        accounts.deposit(alice, conn, alice_card.id, Money(100))

    conn.execute(
        "UPDATE accounts SET day_bucket = '2000-01-01' WHERE user_id = ?", (alice.user_id,)
    )

    accounts.deposit(alice, conn, alice_card.id, Money(10_000))
    assert daily_totals(conn, alice.user_id) == (10_000, 0)


def test_daily_totals_follow_the_utc_date(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    conn.execute(
        "UPDATE accounts SET day_bucket = '2000-01-01' WHERE user_id = ?", (alice.user_id,)
    )
    accounts.deposit(alice, conn, alice_card.id, Money(100))

    row = storage.get_account(conn, alice.user_id)
    assert row is not None
    assert row["day_bucket"] == datetime.now(timezone.utc).date().isoformat()


def test_refused_movement_changes_nothing(
    conn: sqlite3.Connection, alice: StubSession, alice_card: StoredMethod
) -> None:
    conn.execute(
        "UPDATE accounts SET day_bucket = '2000-01-01', daily_deposit_cents = 5"
        " WHERE user_id = ?",
        (alice.user_id,),
    )
    with pytest.raises(AccountError):
        accounts.withdraw(alice, conn, alice_card.id, Money(20_001))

    # Not even yesterday's totals were rolled over: the whole transaction went.
    row = storage.get_account(conn, alice.user_id)
    assert row is not None
    assert (row["day_bucket"], row["daily_deposit_cents"]) == ("2000-01-01", 5)
    assert audit.read(conn, action=Action.WITHDRAW) == []


# ---------------------------------------------------------------------------
# Whose card, and whose account
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("move", [accounts.deposit, accounts.withdraw])
def test_cannot_use_other_users_method(
    conn: sqlite3.Connection, alice: StubSession, move: object
) -> None:
    """IDOR, CWE-639: depositing from, or withdrawing to, someone else's card."""
    bob = make_player(conn, "bob")
    bobs_card = payment.add_payment_method(bob, conn, visa())

    with pytest.raises(AccountError) as theirs:
        move(alice, conn, bobs_card.id, Money(1_000))  # type: ignore[operator]
    with pytest.raises(AccountError) as missing:
        move(alice, conn, 9999, Money(1_000))  # type: ignore[operator]

    # Someone else's card and no card at all get the same answer.
    assert str(theirs.value) == str(missing.value)
    assert accounts.get_balance(alice, conn) == Money(20_000)
    assert len(audit.read(conn, actor="alice", action=Action.ACCESS_DENIED)) == 2
    assert audit.verify_chain(conn).ok


def test_expired_card_cannot_be_used(conn: sqlite3.Connection, alice: StubSession) -> None:
    """A card can expire after it was added, so it is checked again on every use."""
    with storage.transaction(conn):
        old_card = storage.insert_payment_method(
            conn,
            user_id=alice.user_id,
            token="tok_" + "0" * 32,
            last4="6467",
            brand="VISA",
            exp_month=1,
            exp_year=2020,
        )

    with pytest.raises(AccountError, match="expired"):
        accounts.deposit(alice, conn, old_card, Money(1_000))
    with pytest.raises(AccountError, match="expired"):
        accounts.withdraw(alice, conn, old_card, Money(1_000))
    assert accounts.get_balance(alice, conn) == Money(20_000)


def test_staff_have_no_account(conn: sqlite3.Connection) -> None:
    """Technicians and commissioners hold no balance, so nothing can be deposited for them."""
    with storage.transaction(conn):
        uid = storage.create_user(conn, "tech", "not-a-real-hash", "technician")
    tech = StubSession(uid, "tech", role="technician")
    card = payment.add_payment_method(tech, conn, visa())

    with pytest.raises(AccountError, match="no account"):
        accounts.get_balance(tech, conn)
    with pytest.raises(AccountError, match="no account"):
        accounts.deposit(tech, conn, card.id, Money(1_000))


# ---------------------------------------------------------------------------
# Atomicity and the database backstops
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("move", [accounts.deposit, accounts.withdraw])
def test_rollback_on_audit_failure(
    conn: sqlite3.Connection,
    alice: StubSession,
    alice_card: StoredMethod,
    move: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the record of a movement cannot be written, the money does not move."""

    def broken_write(*args: object, **kwargs: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(audit, "write", broken_write)

    with pytest.raises(RuntimeError, match="disk full"):
        move(alice, conn, alice_card.id, Money(1_000))  # type: ignore[operator]

    assert accounts.get_balance(alice, conn) == Money(20_000)
    assert daily_totals(conn, alice.user_id) == (0, 0)


def test_keyboard_interrupt_rolls_back(
    conn: sqlite3.Connection,
    alice: StubSession,
    alice_card: StoredMethod,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(audit, "write", interrupted)

    with pytest.raises(KeyboardInterrupt):
        accounts.withdraw(alice, conn, alice_card.id, Money(1_000))
    assert accounts.get_balance(alice, conn) == Money(20_000)


def test_balance_never_negative(conn: sqlite3.Connection, alice: StubSession) -> None:
    """Skip accounts.py's check entirely: the schema's CHECK still refuses."""
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        with storage.transaction(conn):
            storage.record_withdrawal(conn, alice.user_id, Money(20_001))

    assert accounts.get_balance(alice, conn) == Money(20_000)


@pytest.mark.parametrize("record", [storage.record_deposit, storage.record_withdrawal])
def test_storage_refuses_non_positive_amounts(
    conn: sqlite3.Connection, alice: StubSession, record: object
) -> None:
    """The second line behind accounts.py's check on negative amounts."""
    for amount in (Money(0), Money(-10_000)):
        with pytest.raises(storage.StorageError):
            record(conn, alice.user_id, amount)  # type: ignore[operator]
    assert accounts.get_balance(alice, conn) == Money(20_000)
