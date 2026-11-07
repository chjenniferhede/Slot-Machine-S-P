"""Payment cards: checking them, and keeping as little of them as possible.

The card number and security code are used once, to get a token from the
payment processor, and then dropped. Only the token, the last four digits,
the brand and the expiry are stored. A stolen database therefore holds no
card numbers at all, which is stronger than holding them encrypted.

The processor here is a mock with the same shape as a real one. Its token is
random, not worked out from the card number: a hash of the number could be
checked against guessed numbers until one matched, and there are few enough
card numbers for that to be practical.

Ownership is checked in every query. Player access and the forced password
change gate are enforced by ``auth.require_role`` before each account action.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Final, Protocol, TypeGuard

from slotmachine import audit, auth, rng, storage
from slotmachine.audit import Action

VISA: Final[str] = "VISA"
MASTERCARD: Final[str] = "MASTERCARD"
AMEX: Final[str] = "AMEX"
DISCOVER: Final[str] = "DISCOVER"
UNKNOWN: Final[str] = "UNKNOWN"
SUPPORTED_BRANDS: Final[frozenset[str]] = frozenset({VISA, MASTERCARD, AMEX, DISCOVER})

MIN_DIGITS: Final[int] = 13
MAX_DIGITS: Final[int] = 19
MAX_YEARS_AHEAD: Final[int] = 20

# Digits 0-9 only. str.isdigit() also accepts superscripts and digits from
# other writing systems, none of which belong in a card number.
_DIGITS_RE: Final[re.Pattern[str]] = re.compile(r"[0-9]+", re.ASCII)


class PaymentError(ValueError):
    """A card or request was refused. The message is safe to show the user.

    It never contains card data: an error message is printed, and may be
    logged, and a card number must end up in neither.
    """


class Caller(Protocol):
    """Who is asking. Layer 2's ``auth.Session`` has these fields."""

    @property
    def user_id(self) -> int: ...

    @property
    def username(self) -> str: ...


@dataclass(frozen=True)
class CardInput:
    """A card as the player typed it, kept in memory for one call only.

    Never stored, logged or returned. The number and security code are left
    out of ``repr()``, so this object can show up in a traceback or a debugger
    without revealing them.
    """

    number: str = field(repr=False)
    exp_month: int
    exp_year: int
    cvv: str = field(repr=False)


@dataclass(frozen=True)
class StoredMethod:
    """What the rest of the system may know about a card."""

    id: int
    token: str = field(repr=False)
    last4: str
    brand: str
    exp_month: int
    exp_year: int

    def __str__(self) -> str:
        return f"{self.brand} ending {self.last4}"


# ---------------------------------------------------------------------------
# Checking a card
# ---------------------------------------------------------------------------


def luhn_valid(number: str) -> bool:
    """True if the number passes the Luhn checksum.

    This catches typing mistakes. It is not a security check: anyone can make
    up a number that passes.
    """
    if not isinstance(number, str) or _DIGITS_RE.fullmatch(number) is None:
        return False

    total = 0
    # From the rightmost digit leftwards, double every second digit.
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2 == 1:
            digit = digit * 2
            if digit > 9:
                digit = digit - 9
        total = total + digit
    return total % 10 == 0


def detect_brand(number: str) -> str:
    """The card network, worked out from the number's leading digits."""
    if not isinstance(number, str) or _DIGITS_RE.fullmatch(number) is None:
        return UNKNOWN
    if len(number) < 4:
        return UNKNOWN

    first_two = int(number[:2])
    first_three = int(number[:3])
    first_four = int(number[:4])

    if first_two in (34, 37):
        return AMEX
    if number[0] == "4":
        return VISA
    if 51 <= first_two <= 55 or 2221 <= first_four <= 2720:
        return MASTERCARD
    if first_four == 6011 or first_two == 65 or 644 <= first_three <= 649:
        return DISCOVER
    return UNKNOWN


def validate_card(card: CardInput, today: date | None = None) -> str:
    """Check every field of a card and return its brand, or raise PaymentError.

    ``today`` is there for tests. It defaults to the current UTC date.
    """
    digits = _card_digits(card.number)

    brand = detect_brand(digits)
    if brand not in SUPPORTED_BRANDS:
        raise PaymentError(
            "only Visa, Mastercard, American Express and Discover cards are accepted"
        )

    if today is None:
        today = datetime.now(timezone.utc).date()
    _check_expiry(card.exp_month, card.exp_year, today)
    _check_cvv(card.cvv, brand)
    return brand


def has_expired(exp_month: int, exp_year: int, today: date | None = None) -> bool:
    """True once the card's expiry month is over, by the UTC date."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    # A card works until the end of its expiry month.
    return (exp_year, exp_month) < (today.year, today.month)


def tokenize(card: CardInput) -> tuple[str, str, str]:
    """The mock payment processor. Returns (token, last4, brand).

    A real processor refuses a bad card, so this one checks it first. The
    token is random and has no connection to the card number.
    """
    brand = validate_card(card)
    last4 = _card_digits(card.number)[-4:]
    token = "tok_" + rng.random_token()
    return token, last4, brand


def _card_digits(number: object) -> str:
    """The card number with spaces and dashes removed, if it is a valid one."""
    if not isinstance(number, str):
        raise PaymentError("enter the card number")

    digits = number.replace(" ", "").replace("-", "")
    if not digits:
        raise PaymentError("enter the card number")
    if _DIGITS_RE.fullmatch(digits) is None:
        raise PaymentError("a card number contains only digits, spaces and dashes")
    if not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        raise PaymentError(f"a card number has {MIN_DIGITS} to {MAX_DIGITS} digits")
    if not luhn_valid(digits):
        raise PaymentError("that card number is not valid; check it was typed correctly")
    return digits


def _check_expiry(month: object, year: object, today: date) -> None:
    if not _is_int(month) or not 1 <= month <= 12:
        raise PaymentError("the expiry month must be 1 to 12")
    if not _is_int(year) or year < 2000:
        raise PaymentError(f"enter the expiry year in full, for example {today.year + 3}")

    if has_expired(month, year, today):
        raise PaymentError("this card has expired")
    if year > today.year + MAX_YEARS_AHEAD:
        raise PaymentError("the expiry year is too far in the future")


def _check_cvv(cvv: object, brand: str) -> None:
    """The security code is checked for shape, used once, and never kept."""
    length = 4 if brand == AMEX else 3
    if not isinstance(cvv, str) or len(cvv) != length or _DIGITS_RE.fullmatch(cvv) is None:
        raise PaymentError(f"the security code on this card is {length} digits")


def _is_int(value: object) -> TypeGuard[int]:
    # bool is a subclass of int, and True is not a month.
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# A player's saved cards
# ---------------------------------------------------------------------------


@auth.require_role(auth.Role.PLAYER)
def add_payment_method(session: auth.Session, conn: sqlite3.Connection, card: CardInput) -> StoredMethod:
    """Link a card to the caller's account.

    The processor sees the full card; the database and the audit log get the
    display details only. Python cannot wipe a string from memory, so the
    defence is never copying the number anywhere that outlives this call.
    """
    token, last4, brand = tokenize(card)

    with storage.transaction(conn):
        method_id = storage.insert_payment_method(
            conn,
            user_id=session.user_id,
            token=token,
            last4=last4,
            brand=brand,
            exp_month=card.exp_month,
            exp_year=card.exp_year,
        )
        audit.write(
            conn,
            session.username,
            Action.PAYMENT_METHOD_ADDED,
            f"method={method_id} {brand} ending {last4}",
        )

    return StoredMethod(
        id=method_id,
        token=token,
        last4=last4,
        brand=brand,
        exp_month=card.exp_month,
        exp_year=card.exp_year,
    )


@auth.require_role(auth.Role.PLAYER)
def list_payment_methods(session: auth.Session, conn: sqlite3.Connection) -> list[StoredMethod]:
    """The caller's own saved cards, oldest first."""
    return [_to_method(row) for row in storage.list_payment_methods(conn, session.user_id)]


@auth.require_role(auth.Role.PLAYER)
def remove_payment_method(session: auth.Session, conn: sqlite3.Connection, method_id: int) -> None:
    """Remove one of the caller's saved cards.

    A card that does not exist and a card that belongs to someone else get
    the same answer, so the reply cannot be used to find other players' ids.
    The attempt is logged either way.
    """
    if not _is_int(method_id):
        raise PaymentError("choose a payment method by its number")

    with storage.transaction(conn):
        removed = storage.delete_payment_method(conn, session.user_id, method_id)
        if removed:
            audit.write(
                conn, session.username, Action.PAYMENT_METHOD_REMOVED, f"method={method_id}"
            )
        else:
            audit.write(
                conn,
                session.username,
                Action.ACCESS_DENIED,
                f"remove_payment_method method={method_id}",
            )

    if not removed:
        raise PaymentError("you have no payment method with that number")


def _to_method(row: sqlite3.Row) -> StoredMethod:
    return StoredMethod(
        id=int(row["id"]),
        token=str(row["token"]),
        last4=str(row["last4"]),
        brand=str(row["brand"]),
        exp_month=int(row["exp_month"]),
        exp_year=int(row["exp_year"]),
    )
