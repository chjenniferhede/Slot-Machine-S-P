"""Money type: parsing, formatting, arithmetic, and the rules that protect it."""

from __future__ import annotations

import ast
import inspect

import pytest

from slotmachine import money as money_module
from slotmachine.money import MAX_CENTS, Money, MoneyError


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("12", 1200),
        ("12.5", 1250),
        ("12.50", 1250),
        ("$12.50", 1250),
        (" $12.50 ", 1250),
        ("1,234.56", 123456),
        ("$1,234.56", 123456),
        ("0", 0),
        ("0.01", 1),
        ("0.1", 10),
    ],
)
def test_parse_valid(text: str, cents: int) -> None:
    assert Money.parse(text).cents == cents


@pytest.mark.parametrize(
    "text",
    [
        "12.567",   # sub-cent precision
        "abc",
        "",
        "   ",
        "$",
        "1e5",      # scientific notation
        "0x10",     # hex
        "12.50.50",
        "-5",       # callers handle sign explicitly; parse never returns negative
        "12,34",    # malformed thousands separator
        "1" * 30,   # absurdly large
        "٥",        # non-ASCII digit
    ],
)
def test_parse_rejects(text: str) -> None:
    with pytest.raises(MoneyError):
        Money.parse(text)


@pytest.mark.parametrize(
    ("cents", "shown"),
    [(0, "$0.00"), (5, "$0.05"), (1250, "$12.50"), (123456, "$1,234.56"), (-150, "-$1.50")],
)
def test_str(cents: int, shown: str) -> None:
    assert str(Money(cents)) == shown


@pytest.mark.parametrize("cents", [0, 1, 99, 100, 1250, 123456, 10**12])
def test_str_parse_roundtrip(cents: int) -> None:
    assert Money.parse(str(Money(cents))) == Money(cents)


def test_arithmetic() -> None:
    assert Money(1000) + Money(250) == Money(1250)
    assert Money(1000) - Money(250) == Money(750)
    assert Money(100) - Money(250) == Money(-150)
    assert Money(1250).times(20) == Money(25_000)
    assert -Money(500) == Money(-500)


def test_percent_rounds_down() -> None:
    # 50% of $0.05 is half a cent. Rounding down keeps the remainder with the
    # house rather than creating a fraction of a cent out of nothing.
    assert Money(5).percent(50) == Money(2)
    assert Money(1000).percent(100) == Money(1000)
    assert Money(1000).percent(0) == Money(0)


def test_fractional_multiplier_rejected() -> None:
    with pytest.raises(MoneyError):
        Money(1000).times(1.5)  # type: ignore[arg-type]


def test_bool_is_not_an_int_here() -> None:
    # bool subclasses int, so Money(True) would silently become one cent.
    with pytest.raises(MoneyError):
        Money(True)  # type: ignore[arg-type]


def test_overflow_raises() -> None:
    with pytest.raises(MoneyError):
        Money(MAX_CENTS).times(2)
    with pytest.raises(MoneyError):
        Money(MAX_CENTS) + Money(1)


def test_immutable() -> None:
    m = Money(100)
    with pytest.raises(AttributeError):
        m.cents = 999  # type: ignore[misc]
    with pytest.raises(AttributeError):
        m._cents = 999  # type: ignore[attr-defined]


def test_hashable_and_comparable() -> None:
    assert {Money(100), Money(100)} == {Money(100)}
    assert Money(100) < Money(200) <= Money(200) < Money(300)
    assert max(Money(1), Money(500), Money(50)) == Money(500)


def test_no_float_in_money_module() -> None:
    """The money module must contain no floating point arithmetic at all.

    Checked by walking the AST rather than grepping the text, so that prose in
    docstrings does not trip it and a real float literal cannot hide inside a
    comment-stripped line. A single float division introduced later would
    reintroduce drift silently, which is exactly the failure this type exists
    to prevent.
    """
    tree = ast.parse(inspect.getsource(money_module))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            offenders.append(f"float literal {node.value!r} on line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "float":
            offenders.append(f"reference to float() on line {node.lineno}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            # True division produces a float even from two ints. Integer
            # division (//) is the only form permitted here.
            offenders.append(f"true division (/) on line {node.lineno}")
    assert not offenders, "money.py must be float-free: " + "; ".join(offenders)
