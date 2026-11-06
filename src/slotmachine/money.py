"""Money, counted in whole cents.

Every amount in this system is an integer number of cents. 
"""

from __future__ import annotations

import re
from typing import Final

# The biggest and smallest whole numbers a database INTEGER column can hold.
MAX_CENTS: Final[int] = 2**63 - 1
MIN_CENTS: Final[int] = -(2**63)

# Accepts "12", "12.5", "12.50", "$12.50", "1,234.56". Digits 0-9 only: plain
# \d would also match digits from other writing systems, so "٥" would be
# read as $5.00 while looking like nothing recognisable in a log.
_AMOUNT_RE: Final[re.Pattern[str]] = re.compile(
    r"""
    ^[ \t]*
    \$?                                          # optional dollar sign
    [ \t]*
    (?P<whole>[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)   # 1234 or 1,234
    (?:\.(?P<frac>[0-9]{1,2}))?                  # at most two decimals
    [ \t]*$
    """,
    re.VERBOSE | re.ASCII,
)


class MoneyError(ValueError):
    """A bad amount. The message is safe to show the user as-is."""


class Money:
    """An exact amount of money, stored as a whole number of cents.

    Cannot be changed once created, so one part of the program cannot alter a
    balance another part is holding.
    """

    __slots__ = ("_cents",)

    # Declared for the type checker. Set with object.__setattr__ below,
    # because normal assignment is blocked.
    _cents: int

    def __init__(self, cents: int) -> None:
        # Check that the input is a valid integer.
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise MoneyError(f"amount must be a whole number of cents, got {cents!r}")
        object.__setattr__(self, "_cents", _checked(cents))

    # -- construction ----------------------------------------------------

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def parse(cls, text: str) -> Money:
        """Turn something the user typed into an exact amount."""
        if not isinstance(text, str):
            raise MoneyError("enter an amount, for example 12.50")

        stripped = text.strip()
        if not stripped:
            raise MoneyError("enter an amount, for example 12.50")

        # Checked on its own so the user gets a useful message rather than
        # the generic "not a valid amount".
        if re.match(r"^[ \t]*\$?[ \t]*[0-9,]+\.[0-9]{3,}[ \t]*$", stripped, re.ASCII):
            raise MoneyError("amounts cannot be smaller than one cent")

        match = _AMOUNT_RE.match(stripped)
        if match is None:
            raise MoneyError(f"{stripped!r} is not a valid amount")

        whole = match.group("whole").replace(",", "")  # "1,234" -> "1234"

        # ".5" and ".50" both mean 50 cents; no dot at all means 0 cents.
        frac = match.group("frac")
        if frac is None:
            frac = "00"
        elif len(frac) == 1:
            frac = frac + "0"

        cents = int(whole) * 100 + int(frac)
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
        """Multiply by a payout multiplier, such as 20 for a 20x win.

        Whole numbers only, so the result cannot be part of a cent.
        """
        if isinstance(multiplier, bool) or not isinstance(multiplier, int):
            raise MoneyError("payout multipliers must be whole numbers")
        return Money(_checked(self._cents * multiplier))

    def percent(self, percentage: int) -> Money:
        """A whole percentage of this amount, rounded down to the cent.

        Rounded down on purpose: any leftover stays with the house.
        """
        if isinstance(percentage, bool) or not isinstance(percentage, int):
            raise MoneyError("percentages must be whole numbers")
        if not 0 <= percentage <= 100:
            raise MoneyError("percentage must be between 0 and 100")
        return Money(self._cents * percentage // 100)

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
        """Format as '$12.50'. Always two decimal places."""
        if self._cents < 0:
            sign = "-"
        else:
            sign = ""

        whole = abs(self._cents) // 100
        frac = abs(self._cents) % 100

        return f"{sign}${whole:,}.{frac:02d}"

    def __repr__(self) -> str:
        return f"Money({self._cents})"

    # -- immutability ----------------------------------------------------

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Money is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("Money is immutable")


def _checked(cents: int) -> int:
    """Return cents, or raise if it will not fit in a database INTEGER."""
    if cents > MAX_CENTS or cents < MIN_CENTS:
        raise MoneyError("amount is outside the representable range")
    return cents
