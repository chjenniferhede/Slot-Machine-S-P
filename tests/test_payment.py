"""Payment cards: validation, tokens, and keeping card data out of storage."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import traceback
from datetime import date, datetime, timezone

import pytest
from conftest import StubSession, dump_all_text, make_player

from slotmachine import audit, payment
from slotmachine.audit import Action
from slotmachine.payment import CardInput, PaymentError, detect_brand, luhn_valid, validate_card

# Published test numbers. Each passes Luhn and belongs to no real account.
VISA = "4539148803436467"
MASTERCARD = "5500005555555559"
AMEX = "371449635398431"
DISCOVER = "6011111111111117"

CVV = "739"
FUTURE_YEAR = datetime.now(timezone.utc).year + 3


def card(
    number: str = VISA, exp_month: int = 12, exp_year: int = FUTURE_YEAR, cvv: str = CVV
) -> CardInput:
    return CardInput(number=number, exp_month=exp_month, exp_year=exp_year, cvv=cvv)


def with_check_digit(body: str) -> str:
    """``body`` plus the one final digit that makes it pass Luhn."""
    for digit in "0123456789":
        if luhn_valid(body + digit):
            return body + digit
    raise AssertionError("unreachable: one check digit always fits")


# ---------------------------------------------------------------------------
# Luhn and brands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("number", [VISA, MASTERCARD, AMEX, DISCOVER])
def test_luhn(number: str) -> None:
    """Each published number passes, and changing any one digit breaks it."""
    assert luhn_valid(number)
    for position, char in enumerate(number):
        changed = number[:position] + str((int(char) + 1) % 10) + number[position + 1 :]
        assert not luhn_valid(changed), f"a typo at position {position} went unnoticed"


@pytest.mark.parametrize("value", ["", "4539 1488 0343 6467", "453914880343646x", "４５３９"])
def test_luhn_needs_plain_digits(value: str) -> None:
    assert not luhn_valid(value)


@pytest.mark.parametrize(
    ("number", "brand"),
    [
        (VISA, payment.VISA),
        (MASTERCARD, payment.MASTERCARD),
        ("2221000000000009", payment.MASTERCARD),  # the newer 2-series range
        (AMEX, payment.AMEX),
        ("341111111111111", payment.AMEX),
        (DISCOVER, payment.DISCOVER),
        ("6500000000000002", payment.DISCOVER),
        ("6440000000000000", payment.DISCOVER),
        ("9000000000000000", payment.UNKNOWN),
        ("3000000000000004", payment.UNKNOWN),
        ("123", payment.UNKNOWN),
    ],
)
def test_brand_detection(number: str, brand: str) -> None:
    assert detect_brand(number) == brand


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_valid_cards_accepted() -> None:
    assert validate_card(card()) == payment.VISA
    assert validate_card(card(number=MASTERCARD)) == payment.MASTERCARD
    assert validate_card(card(number=AMEX, cvv="7391")) == payment.AMEX
    assert validate_card(card(number=DISCOVER)) == payment.DISCOVER


def test_spaces_and_dashes_accepted() -> None:
    assert validate_card(card(number="4539 1488 0343 6467")) == payment.VISA
    assert validate_card(card(number="4539-1488-0343-6467")) == payment.VISA


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"number": ""}, "enter the card number"),
        ({"number": 4539148803436467}, "enter the card number"),
        ({"number": "4539 1488 0343 646x"}, "only digits"),
        ({"number": "４５３９１４８８０３４３６４６７"}, "only digits"),  # full-width digits
        ({"number": "453914880343"}, "13 to 19 digits"),
        ({"number": "45391488034364670000"}, "13 to 19 digits"),
        ({"number": "4539148803436468"}, "not valid"),
        ({"number": with_check_digit("900000000000000")}, "only Visa"),
        ({"exp_month": 0}, "month"),
        ({"exp_month": 13}, "month"),
        ({"exp_month": True}, "month"),
        ({"exp_year": 29}, "in full"),
        ({"exp_year": FUTURE_YEAR + 30}, "too far"),
        ({"cvv": "73"}, "3 digits"),
        ({"cvv": "7391"}, "3 digits"),
        ({"cvv": "७३९"}, "3 digits"),  # Devanagari digits
        ({"cvv": 739}, "3 digits"),
        ({"number": AMEX, "cvv": "739"}, "4 digits"),
    ],
)
def test_rejects(changes: dict[str, object], message: str) -> None:
    bad = card(**changes)  # type: ignore[arg-type]
    with pytest.raises(PaymentError, match=message) as exc:
        validate_card(bad)
    # The message is shown to the user; it must never repeat the card.
    assert "4539" not in str(exc.value)
    assert "1488" not in str(exc.value)


def test_expired_card_rejected() -> None:
    today = date(2030, 6, 15)
    with pytest.raises(PaymentError, match="expired"):
        validate_card(card(exp_month=5, exp_year=2030), today)
    with pytest.raises(PaymentError, match="expired"):
        validate_card(card(exp_month=12, exp_year=2029), today)


def test_card_works_until_the_end_of_its_expiry_month() -> None:
    assert validate_card(card(exp_month=6, exp_year=2030), date(2030, 6, 30))


def test_expiry_uses_utc_by_default() -> None:
    now = datetime.now(timezone.utc)
    assert validate_card(card(exp_month=now.month, exp_year=now.year))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def test_token_is_random() -> None:
    """The same card twice gives two unrelated tokens, so the token cannot be
    worked out from the number, and the number cannot be confirmed from it."""
    first, last4, brand = payment.tokenize(card())
    second, _, _ = payment.tokenize(card())

    assert first != second
    assert (last4, brand) == ("6467", payment.VISA)
    assert re.fullmatch(r"tok_[0-9a-f]{32}", first)
    assert hashlib.sha256(VISA.encode()).hexdigest()[:32] not in first


def test_tokenize_refuses_a_bad_card() -> None:
    with pytest.raises(PaymentError):
        payment.tokenize(card(number="4539148803436468"))


def test_card_repr_hides_number_and_cvv() -> None:
    shown = repr(card()) + str(card())
    assert VISA not in shown
    assert CVV not in shown


def test_stored_method_shows_only_brand_and_last4(conn: sqlite3.Connection, alice: StubSession) -> None:
    method = payment.add_payment_method(alice, conn, card())
    assert str(method) == "VISA ending 6467"
    assert method.token not in repr(method)


# ---------------------------------------------------------------------------
# Card data never stored, logged or raised
# ---------------------------------------------------------------------------


def test_only_display_details_have_a_column(conn: sqlite3.Connection) -> None:
    """A column for the number or the CVV would be the first step to storing it."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(payment_methods)")}
    assert columns == {
        "id", "user_id", "token", "last4", "brand", "exp_month", "exp_year", "added_at",
    }


def test_pan_never_stored(conn: sqlite3.Connection, alice: StubSession) -> None:
    payment.add_payment_method(alice, conn, card(number="4539 1488 0343 6467"))
    payment.add_payment_method(alice, conn, card(number=AMEX, cvv="7391"))

    everything = dump_all_text(conn)
    assert VISA not in everything
    assert AMEX not in everything
    assert "6467" in everything  # the last four are kept, on purpose


def test_cvv_never_stored(conn: sqlite3.Connection, alice: StubSession) -> None:
    payment.add_payment_method(alice, conn, card())

    # Hashes and tokens are hex, so "739" can turn up inside one by chance.
    # Look for it as a number on its own instead.
    everything = dump_all_text(conn)
    assert re.search(rf"(?<![0-9A-Za-z]){CVV}(?![0-9A-Za-z])", everything) is None


def test_pan_not_in_audit_log(conn: sqlite3.Connection, alice: StubSession) -> None:
    method = payment.add_payment_method(alice, conn, card())

    [entry] = audit.read(conn, action=Action.PAYMENT_METHOD_ADDED)
    logged = " ".join(str(value) for value in tuple(entry))
    assert VISA not in logged
    assert CVV not in entry["detail"]
    assert method.token not in logged
    assert entry["actor"] == "alice"
    assert "VISA ending 6467" in entry["detail"]


def test_pan_not_in_exception(conn: sqlite3.Connection) -> None:
    """Fail after the card has been read, and check the whole traceback."""
    nobody = StubSession(user_id=999, username="ghost")  # no such user: the insert fails
    # Built on its own line: a traceback quotes the source of each call, and
    # the number written out in this file is not what is being checked.
    typed = card(number="4539 1488 0343 6467")

    with pytest.raises(sqlite3.IntegrityError) as exc:
        payment.add_payment_method(nobody, conn, typed)

    text = "".join(traceback.format_exception(exc.value))
    assert VISA not in text
    assert "4539 1488 0343 6467" not in text
    assert CVV not in str(exc.value)


def test_refused_card_leaves_no_trace(conn: sqlite3.Connection, alice: StubSession) -> None:
    with pytest.raises(PaymentError, match="expired"):
        payment.add_payment_method(alice, conn, card(exp_year=2020))

    assert payment.list_payment_methods(alice, conn) == []
    assert audit.read(conn, action=Action.PAYMENT_METHOD_ADDED) == []


# ---------------------------------------------------------------------------
# Listing and removing
# ---------------------------------------------------------------------------


def test_list_shows_only_own_methods(conn: sqlite3.Connection, alice: StubSession) -> None:
    bob = make_player(conn, "bob")
    mine = payment.add_payment_method(alice, conn, card())
    payment.add_payment_method(bob, conn, card(number=MASTERCARD))

    assert payment.list_payment_methods(alice, conn) == [mine]


def test_remove_own_method(conn: sqlite3.Connection, alice: StubSession) -> None:
    method = payment.add_payment_method(alice, conn, card())
    payment.remove_payment_method(alice, conn, method.id)

    assert payment.list_payment_methods(alice, conn) == []
    [entry] = audit.read(conn, action=Action.PAYMENT_METHOD_REMOVED)
    assert entry["detail"] == f"method={method.id}"


def test_cannot_remove_other_users_method(conn: sqlite3.Connection, alice: StubSession) -> None:
    """IDOR, CWE-639: knowing a method's id must not be enough to remove it."""
    bob = make_player(conn, "bob")
    bobs_card = payment.add_payment_method(bob, conn, card(number=MASTERCARD))

    with pytest.raises(PaymentError) as theirs:
        payment.remove_payment_method(alice, conn, bobs_card.id)
    assert payment.list_payment_methods(bob, conn) == [bobs_card]

    # Someone else's card and a card that does not exist look the same.
    with pytest.raises(PaymentError) as missing:
        payment.remove_payment_method(alice, conn, 9999)
    assert str(theirs.value) == str(missing.value)

    # Both attempts are on record.
    assert len(audit.read(conn, actor="alice", action=Action.ACCESS_DENIED)) == 2


@pytest.mark.parametrize("method_id", ["1", 1.0, True, None])
def test_remove_needs_a_whole_number(
    conn: sqlite3.Connection, alice: StubSession, method_id: object
) -> None:
    payment.add_payment_method(alice, conn, card())
    with pytest.raises(PaymentError, match="by its number"):
        payment.remove_payment_method(alice, conn, method_id)  # type: ignore[arg-type]
    assert len(payment.list_payment_methods(alice, conn)) == 1


def test_audit_chain_still_verifies(conn: sqlite3.Connection, alice: StubSession) -> None:
    method = payment.add_payment_method(alice, conn, card())
    payment.remove_payment_method(alice, conn, method.id)
    with pytest.raises(PaymentError):
        payment.remove_payment_method(alice, conn, method.id)

    result = audit.verify_chain(conn)
    assert result.ok
    assert result.entries_checked == 3
