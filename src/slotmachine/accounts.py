"""A player's money: the balance, deposits and withdrawals.

Each movement checks the balance and changes it inside one transaction, and
writes its audit entry in that same transaction, so the movement and its
record commit or roll back together. Checking first and changing the balance
in a later step would let two requests both pass the check (TOCTOU, CWE-367).

Every lookup of a card includes the caller's user_id. The players-only role
check is added by Layer 2's ``@require_role`` once it lands.

Game credits (buying in and cashing out) are Component C and come next.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from slotmachine import audit, storage
from slotmachine.audit import Action
from slotmachine.money import Money
from slotmachine.payment import Caller, has_expired

# Per player, per UTC calendar day. Module constants so the technician
# interface can show them.
DAILY_DEPOSIT_LIMIT: Final[Money] = Money(200_000)  # $2,000.00
DAILY_WITHDRAW_LIMIT: Final[Money] = Money(200_000)  # $2,000.00

_NO_SUCH_METHOD: Final[str] = "you have no payment method with that number"


class AccountError(ValueError):
    """A request to move money was refused. The message is safe to show the user."""


class _NotYourMethod(Exception):
    """The card is not the caller's. Logged, then turned into AccountError."""


def get_balance(session: Caller, conn: sqlite3.Connection) -> Money:
    account = storage.get_account(conn, session.user_id)
    if account is None:
        raise AccountError("this login has no account")
    return Money(int(account["balance_cents"]))


def deposit(session: Caller, conn: sqlite3.Connection, method_id: int, amount: Money) -> Money:
    """Move money from one of the caller's cards into their account.

    Returns the new balance.
    """
    _check_request(method_id, amount)

    try:
        with storage.transaction(conn):
            _check_method(session, conn, method_id)
            account = _account_for_today(session, conn)

            deposited_today = Money(int(account["daily_deposit_cents"]))
            room = DAILY_DEPOSIT_LIMIT - deposited_today
            if amount > room:
                raise AccountError(
                    f"the daily deposit limit is {DAILY_DEPOSIT_LIMIT};"
                    f" you can deposit {room} more today"
                )

            balance = storage.record_deposit(conn, session.user_id, amount)
            audit.write(
                conn,
                session.username,
                Action.DEPOSIT,
                f"method={method_id} amount={amount} balance={balance}",
            )
    except _NotYourMethod:
        _record_refusal(session, conn, "deposit", method_id)
        raise AccountError(_NO_SUCH_METHOD) from None

    return balance


def withdraw(session: Caller, conn: sqlite3.Connection, method_id: int, amount: Money) -> Money:
    """Move money from the caller's account back to one of their cards.

    Returns the new balance.
    """
    _check_request(method_id, amount)

    try:
        with storage.transaction(conn):
            _check_method(session, conn, method_id)
            account = _account_for_today(session, conn)

            # Read inside the transaction that changes it, so nothing can
            # spend the same money between this check and the debit.
            balance = Money(int(account["balance_cents"]))
            if amount > balance:
                raise AccountError(f"you have only {balance} in your account")

            withdrawn_today = Money(int(account["daily_withdraw_cents"]))
            room = DAILY_WITHDRAW_LIMIT - withdrawn_today
            if amount > room:
                raise AccountError(
                    f"the daily withdrawal limit is {DAILY_WITHDRAW_LIMIT};"
                    f" you can withdraw {room} more today"
                )

            balance = storage.record_withdrawal(conn, session.user_id, amount)
            audit.write(
                conn,
                session.username,
                Action.WITHDRAW,
                f"method={method_id} amount={amount} balance={balance}",
            )
    except _NotYourMethod:
        _record_refusal(session, conn, "withdraw", method_id)
        raise AccountError(_NO_SUCH_METHOD) from None

    return balance


def _check_request(method_id: object, amount: object) -> None:
    if not isinstance(amount, Money):
        raise AccountError("enter an amount, for example 50.00")
    if not amount.is_positive():
        # Zero does nothing. A negative deposit would be a withdrawal that
        # skips every withdrawal check, and a negative withdrawal a deposit
        # that skips the deposit limit.
        raise AccountError("the amount must be more than $0.00")
    if not isinstance(method_id, int) or isinstance(method_id, bool):
        raise AccountError("choose a payment method by its number")


def _check_method(session: Caller, conn: sqlite3.Connection, method_id: int) -> None:
    """Refuse a card that is not the caller's, or that has expired since it was added."""
    method = storage.get_payment_method(conn, session.user_id, method_id)
    if method is None:
        raise _NotYourMethod
    if has_expired(int(method["exp_month"]), int(method["exp_year"])):
        raise AccountError("this card has expired; add a new one")


def _account_for_today(session: Caller, conn: sqlite3.Connection) -> sqlite3.Row:
    """The caller's account, with the daily totals reset if the UTC date has changed.

    UTC everywhere: a reset at local midnight would let someone who changes
    their timezone start a new day twice.
    """
    storage.roll_over_daily_totals(conn, session.user_id, storage.today_bucket())
    account = storage.get_account(conn, session.user_id)
    if account is None:
        raise AccountError("this login has no account")
    return account


def _record_refusal(
    session: Caller, conn: sqlite3.Connection, action: str, method_id: int
) -> None:
    # A transaction of its own: the one the request ran in was rolled back.
    # Someone else's card and a card that does not exist are logged, and
    # answered, the same way.
    with storage.transaction(conn):
        audit.write(conn, session.username, Action.ACCESS_DENIED, f"{action} method={method_id}")
