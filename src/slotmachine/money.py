"""Exact money arithmetic.

Every monetary value in this system is an integer number of cents, wrapped in
the `Money` type defined here. Nothing else is permitted.

WHY NOT float
-------------
`0.1 + 0.2 == 0.30000000000000004`. Binary floating point cannot represent most
decimal fractions exactly, so error accumulates as payouts are added to a
balance. In a system that performs hundreds of thousands of small additions the
drift becomes real money, and an attacker who understands the rounding can
provoke it deliberately.

WHY NOT Decimal
---------------
`Decimal` is exact, but it permits fractional cents. A payout of $0.005 is not a
value this system is allowed to hold, and a type that can represent it invites a
rounding decision somewhere downstream. Integer cents cannot express an illegal
amount in the first place.

OVERFLOW
--------
Python integers are arbitrary precision and never overflow, but the SQLite
INTEGER columns they are stored in are signed 64-bit. A value beyond that range
fails or corrupts at the database boundary, far from its cause, so every
operation here checks the range up front.
"""

from __future__ import annotations

import re
from typing import Final

# SQLite INTEGER is a signed 64-bit value.
MAX_CENTS: Final[int] = 2**63 - 1
MIN_CENTS: Final[int] = -(2**63)

# Accepts: "12", "12.5", "12.50", "$12.50", "1,234.56", " 12.50 "
# Rejects: "12.567" (sub-cent), "1e5", "0x10", "12.50.50", "" and anything else.
#
# NOTE re.ASCII. Without it, Python's \d matches every Unicode decimal digit --
# Arabic-Indic, Devanagari, fullwidth and so on -- and int() happily converts
# them. "\u0665" would parse as $5.00 while being visually unrecognisable to
# anyone reading a log or an audit trail. Money input is ASCII digits only.
# (CWE-176, improper handling of Unicode encoding.)
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^[ \t]*                   # leading space
    \$?                       # optional dollar sign
    [ \t]*
    (?P<whole>[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)   # 1234 or 1,234
    (?:\.(?P<frac>[0-9]{1,2}))?  # at most two decimals -- 12.567 is rejected
    [ \t]*$
    """,
    re.VERBOSE | re.ASCII,
)


class MoneyError(ValueError):
    """Invalid monetary amount.

    Messages are written to be shown to a user verbatim, so the CLI never has to
    invent its own wording and never has to surface a traceback.
    """


class Money:
    """An exact amount of money, stored as an integer number of cents.

    Immutable. A balance held by one part of the system cannot be mutated from
    another.
    """

    __slots__ = ("_cents",)

    # Declared for the type checker. The value is set via object.__setattr__
    # in __init__ because __setattr__ is overridden to enforce immutability.
    _cents: int

    def __init__(self, cents: int) -> None:
        if isinstance(cents, bool) or not isinstance(cents, int):
            # bool is a subclass of int; Money(True) is always a bug.
            raise MoneyError(f"amount must be a whole number of cents, got {cents!r}")
        _check_range(cents)
        object.__setattr__(self, "_cents", cents)

    # -- construction ----------------------------------------------------

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def parse(cls, text: str) -> Money:
        """Parse user input into an exact amount.

        Raises MoneyError with a user-facing message on anything invalid.
        """
        if not isinstance(text, str):
            raise MoneyError("enter an amount, for example 12.50")

        stripped = text.strip()
        if not stripped:
            raise MoneyError("enter an amount, for example 12.50")

        # Reject sub-cent precision explicitly so the message is useful. Without
        # this, "12.567" would simply fail the regex with a generic error.
        if re.match(r"^[ \t]*\$?[ \t]*[0-9,]+\.[0-9]{3,}[ \t]*$", stripped, re.ASCII):
            raise MoneyError("amounts cannot be smaller than one cent")

        match = _AMOUNT_RE.match(stripped)
        if match is None:
            raise MoneyError(f"{text.strip()!r} is not a valid amount")

        whole = match.group("whole").replace(",", "")
        frac = (match.group("frac") or "").ljust(2, "0")

        try:
            cents = int(whole) * 100 + int(frac)
        except ValueError:  # pragma: no cover - regex already guarantees digits
            raise MoneyError(f"{text.strip()!r} is not a valid amount") from None

        if cents > MAX_CENTS:
            raise MoneyError("amount is too large")
        return cls(cents)

    # -- access ----------------------------------------------------------

    @property
    def cents(self) -> int:
        return self._cents

    def is_positive(self) -> bool:
        return self._cents > 0

    def is_zero(self) -> bool:
        return self._cents == 0

    def is_negative(self) -> bool:
        return self._cents < 0

    # -- arithmetic ------------------------------------------------------

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(_checked(self._cents + other._cents))

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(_checked(self._cents - other._cents))

    def __neg__(self) -> Money:
        return Money(_checked(-self._cents))

    def times(self, multiplier: int) -> Money:
        """Multiply by an integer payout multiplier.

        Integer only. A fractional multiplier would produce sub-cent amounts,
        which is precisely what this type exists to prevent.
        """
        if isinstance(multiplier, bool) or not isinstance(multiplier, int):
            raise MoneyError("payout multipliers must be whole numbers")
        return Money(_checked(self._cents * multiplier))

    def percent(self, pct: int) -> Money:
        """A whole-percent fraction, rounded down to the cent.

        Used for things like a hold costing 50% of the original bet. Rounding
        down is deliberate: where a split is inexact, the remainder stays with
        the house rather than being created out of nothing.
        """
        if isinstance(pct, bool) or not isinstance(pct, int):
            raise MoneyError("percentages must be whole numbers")
        if not 0 <= pct <= 100:
            raise MoneyError("percentage must be between 0 and 100")
        return Money(self._cents * pct // 100)

    # -- comparison ------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents == other._cents

    def __lt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents < other._cents

    def __le__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents <= other._cents

    def __gt__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents > other._cents

    def __ge__(self, other: Money) -> bool:
        if not isinstance(other, Money):
            return NotImplemented
        return self._cents >= other._cents

    def __hash__(self) -> int:
        return hash((Money, self._cents))

    # -- display ---------------------------------------------------------

    def __str__(self) -> str:
        """'$12.50'. Always two decimals, always signed consistently."""
        sign = "-" if self._cents < 0 else ""
        whole, frac = divmod(abs(self._cents), 100)
        return f"{sign}${whole:,}.{frac:02d}"

    def __repr__(self) -> str:
        return f"Money({self._cents})"

    # -- immutability ----------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Money is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Money is immutable")


def _check_range(cents: int) -> None:
    if cents > MAX_CENTS or cents < MIN_CENTS:
        raise MoneyError("amount is outside the representable range")


def _checked(cents: int) -> int:
    _check_range(cents)
    return cents
