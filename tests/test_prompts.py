"""Prompts: every answer is checked, every prompt asks again, nothing crashes."""

from __future__ import annotations

import random
from collections.abc import Callable

import pytest
from conftest import Keyboard

from slotmachine.cli.prompts import (
    GAME_ID_MAX,
    check_game_id,
    check_username,
    clean,
    error,
    info,
    prompt_choice,
    prompt_int,
    prompt_money,
    prompt_password,
    prompt_text,
    prompt_username,
    prompt_yes_no,
    success,
)
from slotmachine.money import Money

GOOD_PASSWORD = "correct horse battery"


def printable(out: str) -> bool:
    """True if ``out`` holds nothing a terminal would act on, only text and newlines."""
    return all(ch.isprintable() or ch == "\n" for ch in out)


def error_count(out: str) -> int:
    return sum(1 for line in out.splitlines() if line.startswith("  ✗ "))


# ---------------------------------------------------------------------------
# Every prompt asks again until the answer is valid
# ---------------------------------------------------------------------------

CHOICES = [("s", "Spin"), ("b", "Bet"), ("x", "Cash out")]


@pytest.mark.parametrize(
    ("ask", "bad", "good", "expected"),
    [
        (lambda: prompt_int("Pick", minimum=1, maximum=3), "four", "2", 2),
        (lambda: prompt_int("Pick", minimum=1, maximum=3), "4", "3", 3),
        (lambda: prompt_int("Pick", minimum=1, maximum=3), "0", "1", 1),
        (lambda: prompt_money("Deposit"), "-5", "12.50", Money(1250)),
        (lambda: prompt_money("Deposit"), "0", "0.01", Money(1)),
        (lambda: prompt_money("Deposit"), "1.234", "$1,234.56", Money(123_456)),
        (lambda: prompt_money("Deposit", maximum=Money(10_000)), "100.01", "100", Money(10_000)),
        (lambda: prompt_username(), "al ice", "alice", "alice"),
        (lambda: prompt_choice("Next", CHOICES), "q", "X", "x"),
        (lambda: prompt_yes_no("Sure?"), "maybe", "n", False),
        (lambda: prompt_text("Note"), "", "hello", "hello"),
        (lambda: prompt_text("Note"), "bell\x07", "hello", "hello"),
    ],
)
def test_prompt_rejects_then_accepts(
    keyboard: Keyboard,
    capsys: pytest.CaptureFixture[str],
    ask: Callable[[], object],
    bad: str,
    good: str,
    expected: object,
) -> None:
    keyboard.type(bad, good)
    assert ask() == expected
    assert not keyboard.script, "both answers should have been read"
    assert error_count(capsys.readouterr().out) == 1


def test_prompt_keeps_asking(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    """There is no limit on attempts: a prompt never gives up and raises."""
    keyboard.type(*["nope"] * 50, "7")
    assert prompt_int("Pick", minimum=1, maximum=9) == 7
    assert error_count(capsys.readouterr().out) == 50


@pytest.mark.parametrize("answer", ["1_000", "+5", "1,00", "٥", "5.0", "0x10", "1e3", "--1", ""])
def test_int_accepts_plain_digits_only(keyboard: Keyboard, answer: str) -> None:
    keyboard.type(answer, "9")
    assert prompt_int("Pick") == 9


@pytest.mark.parametrize(("answer", "value"), [("1,000,000", 1_000_000), (" 42 ", 42), ("-3", -3)])
def test_int_reads(keyboard: Keyboard, answer: str, value: int) -> None:
    keyboard.type(answer)
    assert prompt_int("Pick") == value


def test_yes_no_default(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    keyboard.type("", "")
    assert prompt_yes_no("Sure?", default=True) is True
    assert prompt_yes_no("Sure?", default=False) is False

    keyboard.type("", "YES")
    assert prompt_yes_no("Sure?") is True  # no default, so Enter asks again
    assert error_count(capsys.readouterr().out) == 1


def test_choice_shows_its_keys(keyboard: Keyboard) -> None:
    keyboard.type("b")
    assert prompt_choice("Credits: $47.00", CHOICES) == "b"
    assert "[S]pin  [B]et  [X] Cash out" in keyboard.prompts[0]


@pytest.mark.parametrize("options", [[], [("s", "Spin"), ("S", "Stop")], [("", "Nothing")]])
def test_choice_needs_distinct_keys(options: list[tuple[str, str]]) -> None:
    with pytest.raises(ValueError, match="distinct"):
        prompt_choice("Next", options)


def test_text_allow_empty(keyboard: Keyboard) -> None:
    keyboard.type("   ")
    assert prompt_text("Filter", allow_empty=True) == ""


def test_unreadable_input_asks_again(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    keyboard.type(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), "5")
    assert prompt_int("Pick") == 5
    assert error_count(capsys.readouterr().out) == 1


# ---------------------------------------------------------------------------
# Length limits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ask", "too_long", "longest"),
    [
        (prompt_username, "a" * 33, "a" * 32),
        (lambda: prompt_text("Note"), "a" * 129, "a" * 128),
        (
            lambda: prompt_text("Game", validator=check_game_id, max_length=GAME_ID_MAX),
            "a" * 33,
            "a" * 32,
        ),
        (lambda: prompt_int("Rounds"), "1" * 33, "1" * 32),
        (lambda: prompt_money("Amount"), "1" * 33, "1" * 15),
        (prompt_password, "p" * 129, "p" * 128),
    ],
)
def test_length_limits(
    keyboard: Keyboard,
    capsys: pytest.CaptureFixture[str],
    ask: Callable[[], object],
    too_long: str,
    longest: str,
) -> None:
    keyboard.type(too_long, longest)
    assert ask() not in (None, "")
    assert not keyboard.script
    out = capsys.readouterr().out
    assert error_count(out) == 1
    assert "too long" in out


def test_length_minimums(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    keyboard.type("ab", "abc")
    assert prompt_username() == "abc"

    keyboard.type("x" * 11, "x" * 12, "x" * 12)
    assert prompt_password(new=True) == "x" * 12

    assert error_count(capsys.readouterr().out) == 2


def test_too_long_answer_is_not_quoted_back(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    """The length is checked before anything parses the answer, and parsers quote what they reject."""
    keyboard.type("9" * 100_000, "5")
    assert prompt_money("Amount") == Money(500)
    assert len(capsys.readouterr().out) < 200


# ---------------------------------------------------------------------------
# The username allowlist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "al ice",
        "al'ice",
        'al"ice',
        "alice;--",
        "al/ice",
        "al\\ice",
        "<b>alice",
        "al\nice",
        "alice\n",  # "$" in a regex matches before a final newline; fullmatch does not
        "al\tice",
        "al\x00ice",
        "\x1b[31malice",
        "alicе",  # the last letter is Cyrillic
        "ａlice",  # fullwidth a
        "ab",
        "a" * 33,
        "",
    ],
)
def test_username_allowlist_refuses(name: str) -> None:
    with pytest.raises(ValueError, match="username"):
        check_username(name)


@pytest.mark.parametrize("name", ["alice", "Bob_99", "a.b-c", "abc", "x" * 32, "tech-admin"])
def test_username_allowlist_accepts(name: str) -> None:
    assert check_username(name) == name


def test_username_prompt_strips_surrounding_spaces(keyboard: Keyboard) -> None:
    keyboard.type("  alice  ")
    assert prompt_username() == "alice"


@pytest.mark.parametrize("game_id", ["", "Classic", "classic-sevens", "a" * 33, "abc\n", "abc def"])
def test_game_id_allowlist_refuses(game_id: str) -> None:
    with pytest.raises(ValueError, match="game id"):
        check_game_id(game_id)


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def test_password_is_read_with_getpass(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    keyboard.type(GOOD_PASSWORD)
    assert prompt_password() == GOOD_PASSWORD
    assert keyboard.prompts == keyboard.secret_prompts  # input() was never used
    assert GOOD_PASSWORD not in capsys.readouterr().out


def test_new_password_is_typed_twice(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    keyboard.type("first choice here", "a different one!", GOOD_PASSWORD, GOOD_PASSWORD)
    assert prompt_password("New password", new=True) == GOOD_PASSWORD
    assert len(keyboard.secret_prompts) == 4
    out = capsys.readouterr().out
    assert "did not match" in out
    assert "first choice" not in out


def test_password_keeps_spaces(keyboard: Keyboard) -> None:
    keyboard.type("  spaced out pw  ", "  spaced out pw  ")
    assert prompt_password(new=True) == "  spaced out pw  "


def test_login_password_has_no_minimum(keyboard: Keyboard) -> None:
    """Too short to be anyone's password, but that is for the login check to say."""
    keyboard.type("short")
    assert prompt_password() == "short"


def test_password_refuses_control_characters(keyboard: Keyboard) -> None:
    keyboard.type("pass\x00word-long-enough", GOOD_PASSWORD)
    assert prompt_password() == GOOD_PASSWORD


# ---------------------------------------------------------------------------
# Untrusted text is made safe before it is shown
# ---------------------------------------------------------------------------

NASTY = [
    "\x1b[2J\x1b[H",  # clear the screen, cursor home
    "\x1b]0;owned\x07",  # set the window title
    "\x9b31m",  # C1 CSI: an escape code without ESC
    "\r",  # back to the start of the line, to write over it
    "\b\b\b",
    "\x00",
    "\x7f",
    "\t",
    "‮",  # right-to-left override
    "​",  # zero-width space
    " ",  # line separator
    "\ud800",  # a lone surrogate, which cannot even be encoded
]


@pytest.mark.parametrize("nasty", NASTY)
def test_control_chars_stripped_on_display(
    nasty: str, capsys: pytest.CaptureFixture[str]
) -> None:
    shown = clean(f"alice{nasty}bob")
    assert printable(shown)
    assert shown.startswith("alice") and shown.endswith("bob")
    assert "?" in shown  # replaced, not silently dropped

    info(f"alice{nasty}bob")
    error(f"alice{nasty}bob")
    success(f"alice{nasty}bob")
    assert printable(capsys.readouterr().out)


def test_clean_leaves_ordinary_text_alone() -> None:
    for text in ["Classic Sevens", "*** THREE SEVENS — WIN $100.00 ***", "✓ Chain intact", "张三"]:
        assert clean(text) == text


def test_prompt_label_is_cleaned(keyboard: Keyboard) -> None:
    keyboard.type("5")
    prompt_int("Bet on \x1b[2JClassic")
    assert printable(keyboard.prompts[0].rstrip("\n"))


def test_rejected_answer_is_cleaned_when_quoted_back(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    keyboard.type("\x1b[2J‮", "5")
    prompt_money("Amount")
    assert printable(capsys.readouterr().out)


# ---------------------------------------------------------------------------
# Ctrl-C and Ctrl-D pass straight through, so a transaction can roll back
# ---------------------------------------------------------------------------

ALL_PROMPTS: list[Callable[[], object]] = [
    lambda: prompt_text("Note"),
    prompt_username,
    lambda: prompt_int("Pick", minimum=1, maximum=3),
    lambda: prompt_money("Amount"),
    lambda: prompt_choice("Next", CHOICES),
    lambda: prompt_yes_no("Sure?"),
    prompt_password,
    lambda: prompt_password(new=True),
]


@pytest.mark.parametrize("ask", ALL_PROMPTS)
def test_ctrl_c_is_not_swallowed(keyboard: Keyboard, ask: Callable[[], object]) -> None:
    keyboard.type("\x1b", KeyboardInterrupt)  # one bad answer first, then Ctrl-C
    with pytest.raises(KeyboardInterrupt):
        ask()


@pytest.mark.parametrize("ask", ALL_PROMPTS)
def test_eof_is_not_swallowed(keyboard: Keyboard, ask: Callable[[], object]) -> None:
    keyboard.type("\x1b")
    with pytest.raises(EOFError):
        ask()


# ---------------------------------------------------------------------------
# Fuzzing
# ---------------------------------------------------------------------------

# Each prompt, a type its answer must be, and valid answers to follow the fuzz.
# Enough follow so that a fuzz string that happens to be valid still finishes:
# a new password reads up to four lines.
FUZZ_TARGETS: list[tuple[str, Callable[[], object], type, tuple[str, ...]]] = [
    ("text", lambda: prompt_text("Note"), str, ("hello",)),
    ("username", prompt_username, str, ("alice",)),
    (
        "game id",
        lambda: prompt_text("Game", validator=check_game_id, max_length=GAME_ID_MAX),
        str,
        ("classic_sevens",),
    ),
    ("int", lambda: prompt_int("Rounds", minimum=1, maximum=1_000_000), int, ("10",)),
    ("money", lambda: prompt_money("Amount", maximum=Money(200_000)), Money, ("12.50",)),
    ("choice", lambda: prompt_choice("Next", CHOICES), str, ("s",)),
    ("yes/no", lambda: prompt_yes_no("Sure?"), bool, ("y",)),
    ("password", prompt_password, str, (GOOD_PASSWORD,)),
    ("new password", lambda: prompt_password(new=True), str, (GOOD_PASSWORD,) * 3),
]


def fuzz_strings(count: int = 100) -> list[str]:
    """``count`` random byte strings, read the way a terminal that cannot decode them would.

    surrogateescape turns each undecodable byte into a lone surrogate, which
    is the nastiest thing input() can hand back: it cannot even be printed.
    """
    rnd = random.Random(20261106)  # fixed, so a failure can be reproduced
    strings = []
    for i in range(count):
        length = rnd.randint(0, 300) if i % 10 == 0 else rnd.randint(0, 40)
        raw = bytes(rnd.randrange(256) for _ in range(length))
        strings.append(raw.decode("utf-8", errors="surrogateescape"))
    return strings


@pytest.mark.parametrize(
    ("ask", "answer_type", "follow"),
    [target[1:] for target in FUZZ_TARGETS],
    ids=[target[0] for target in FUZZ_TARGETS],
)
def test_no_traceback_on_bad_input(
    keyboard: Keyboard,
    capsys: pytest.CaptureFixture[str],
    ask: Callable[[], object],
    answer_type: type,
    follow: tuple[str, ...],
) -> None:
    for fuzz in fuzz_strings():
        keyboard.script.clear()
        keyboard.type(fuzz, *follow)
        assert isinstance(ask(), answer_type), f"fuzz input {fuzz!r}"
        out = capsys.readouterr().out
        assert "Traceback" not in out
        assert printable(out), f"fuzz input {fuzz!r} reached the screen unescaped"
