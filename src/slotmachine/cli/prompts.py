"""Reading what the user types, and showing text that came from a user.

Every piece of untrusted input enters the system through this module, so this
is where bad input is stopped. Each prompt has a length limit, checks against
an allowlist where there is one, and asks again until the answer is valid. A
bad answer gets a one-line message, never a traceback (CWE-209).

Prompts do not catch EOFError (Ctrl-D, or the end of piped input) or
KeyboardInterrupt (Ctrl-C). Both travel up to the menu loop in menus.py, and
on the way they pass through any open storage.transaction, which rolls back.
Catching Ctrl-C here would let a spin finish that the player tried to stop.

Text from a user goes through clean() before it is shown, so a value holding
terminal escape codes cannot rewrite the screen (CWE-150).

The username and password rules here are for the person typing: they get told
straight away. Layer 2's auth module must enforce the same rules itself,
because a check in the interface is only as strong as the interface.
"""

from __future__ import annotations

import getpass
import re
from collections.abc import Callable, Sequence
from typing import Final, TypeVar

from slotmachine.game.spec import GAME_ID_PATTERN
from slotmachine.money import Money

T = TypeVar("T")

# Every line of output starts with this, so the screens line up.
INDENT: Final[str] = "  "

USERNAME_MIN: Final[int] = 3
USERNAME_MAX: Final[int] = 32
PASSWORD_MIN: Final[int] = 12
PASSWORD_MAX: Final[int] = 128
GAME_ID_MAX: Final[int] = 32
TEXT_MAX: Final[int] = 128  # any other free text
SHORT_MAX: Final[int] = 32  # numbers, amounts, menu keys and y/n answers

# Allowlists, not denylists: anything not listed is refused. Used with
# fullmatch, because "$" in a pattern also matches just before a final
# newline, so a match() against "^...$" would accept "alice\n".
USERNAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.-]{3,32}")

# Digits 0-9 only, optionally grouped as 1,000,000. int() alone would also
# accept "1_000", surrounding spaces, and digits from other scripts like "٥".
_INT_PATTERN: Final[re.Pattern[str]] = re.compile(r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)")

_UNREADABLE: Final[str] = "that could not be read; please type it again"


# ---------------------------------------------------------------------------
# Showing text
# ---------------------------------------------------------------------------


def clean(text: object) -> str:
    """``text`` made safe to print: each unprintable character becomes '?'.

    That covers ESC, which starts every terminal escape code; the C1 controls
    some terminals also obey; carriage return and backspace, which let a line
    write over itself; line separators; and invisible Unicode such as the
    right-to-left override, which changes the order text is shown in. They are
    replaced rather than deleted, so a value that was tampered with still
    looks tampered with.
    """
    return "".join(ch if ch.isprintable() else "?" for ch in str(text))


def info(message: object) -> None:
    print(f"{INDENT}{clean(message)}")


def success(message: object) -> None:
    print(f"{INDENT}✓ {clean(message)}")


def error(message: object) -> None:
    print(f"{INDENT}✗ {clean(message)}")


# ---------------------------------------------------------------------------
# Validators: return the value, or raise ValueError with a message to show
# ---------------------------------------------------------------------------


def check_username(text: str) -> str:
    if USERNAME_PATTERN.fullmatch(text) is None:
        raise ValueError(
            f"a username is {USERNAME_MIN} to {USERNAME_MAX} characters:"
            " letters, digits, '.', '_' and '-'"
        )
    return text


def check_game_id(text: str) -> str:
    if len(text) > GAME_ID_MAX or GAME_ID_PATTERN.fullmatch(text) is None:
        raise ValueError(
            f"a game id is 1 to {GAME_ID_MAX} characters: lower-case letters, digits and '_'"
        )
    return text


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def prompt_text(
    label: str,
    *,
    validator: Callable[[str], str] | None = None,
    allow_empty: bool = False,
    max_length: int = TEXT_MAX,
) -> str:
    """A line of text, with surrounding spaces removed.

    Control characters are always refused. ``validator`` gets the stripped
    text and returns the value to use, or raises ValueError to ask again.
    With ``allow_empty``, pressing Enter returns "" without validating.
    """

    def parse(raw: str) -> str:
        text = raw.strip()
        if not text:
            if allow_empty:
                return ""
            raise ValueError("please type something")
        if not text.isprintable():
            raise ValueError("letters, digits, spaces and punctuation only")
        if validator is None:
            return text
        return validator(text)

    return _ask(label, parse, max_length=max_length)


def prompt_username(label: str = "Username") -> str:
    return prompt_text(label, validator=check_username, max_length=USERNAME_MAX)


def prompt_int(label: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """A whole number between ``minimum`` and ``maximum``, inclusive."""

    def parse(raw: str) -> int:
        text = raw.strip()
        if _INT_PATTERN.fullmatch(text) is None:
            raise ValueError(_int_hint(minimum, maximum))
        value = int(text.replace(",", ""))
        if minimum is not None and value < minimum:
            raise ValueError(_int_hint(minimum, maximum))
        if maximum is not None and value > maximum:
            raise ValueError(_int_hint(minimum, maximum))
        return value

    return _ask(label, parse, max_length=SHORT_MAX)


def prompt_money(
    label: str, *, minimum: Money | None = None, maximum: Money | None = None
) -> Money:
    """An amount of money. The least it accepts is one cent unless ``minimum`` says otherwise.

    Money.parse never reads a minus sign, so a negative amount cannot be typed.
    """
    least = Money(1) if minimum is None else minimum

    def parse(raw: str) -> Money:
        amount = Money.parse(raw)  # MoneyError is a ValueError with a message to show
        if amount < least:
            raise ValueError(f"enter at least {least}")
        if maximum is not None and amount > maximum:
            raise ValueError(f"enter at most {maximum}")
        return amount

    return _ask(label, parse, max_length=SHORT_MAX)


def prompt_choice(label: str, options: Sequence[tuple[str, str]]) -> str:
    """One of ``options``, given as (key, description) pairs. Returns the key.

    Keys are matched ignoring case. ("s", "Spin") is shown as "[S]pin".
    """
    keys = [key.lower() for key, _ in options]
    if not keys or "" in keys or len(set(keys)) != len(keys):
        raise ValueError("prompt_choice needs distinct, non-empty keys")

    shown = "  ".join(_option_text(key, text) for key, text in options)
    full_label = f"{label}  {shown}" if label else shown

    def parse(raw: str) -> str:
        answer = raw.strip().lower()
        if answer in keys:
            return options[keys.index(answer)][0]
        raise ValueError("choose one of: " + ", ".join(key.upper() for key, _ in options))

    return _ask(full_label, parse, max_length=SHORT_MAX)


def prompt_yes_no(label: str, *, default: bool | None = None) -> bool:
    """Yes or no. With a ``default``, pressing Enter gives it."""
    if default is None:
        hint = "y/n"
    elif default:
        hint = "Y/n"
    else:
        hint = "y/N"

    def parse(raw: str) -> bool:
        answer = raw.strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if not answer and default is not None:
            return default
        raise ValueError("answer y or n")

    return _ask(f"{label} [{hint}]", parse, max_length=SHORT_MAX)


def prompt_password(label: str = "Password", *, new: bool = False) -> str:
    """A password, read with getpass so it is never shown or kept in shell history.

    With ``new`` the password is being chosen: it must be 12 to 128
    characters, and it is typed twice. Without it (logging in) only the upper
    limit applies. Whether the password is right is for the login check to
    decide, and every attempt that check sees counts toward lockout.

    Spaces count as part of the password; nothing is stripped.
    """

    def parse(raw: str) -> str:
        if not raw:
            raise ValueError("please type a password")
        if not raw.isprintable():
            raise ValueError("a password cannot contain control characters")
        if new and len(raw) < PASSWORD_MIN:
            raise ValueError(f"a password needs at least {PASSWORD_MIN} characters")
        return raw

    while True:
        password = _ask(label, parse, max_length=PASSWORD_MAX, secret=True)
        if not new:
            return password
        again = _ask("Type it again", parse, max_length=PASSWORD_MAX, secret=True)
        if again == password:
            return password
        error("the two passwords did not match; please choose it again")


# ---------------------------------------------------------------------------
# The loop every prompt shares
# ---------------------------------------------------------------------------


def _ask(
    label: str, parse: Callable[[str], T], *, max_length: int, secret: bool = False
) -> T:
    """Ask until ``parse`` accepts the answer.

    The length is checked before ``parse`` sees the text, so no parser ever
    works on, or quotes back, an answer longer than the limit.
    """
    prompt = f"{INDENT}{clean(label)} > "
    while True:
        try:
            raw = getpass.getpass(prompt) if secret else input(prompt)
        except UnicodeDecodeError:
            # Bytes that are not text in the terminal's encoding. menus.run()
            # sets the streams up so this does not happen; this is the backstop.
            error(_UNREADABLE)
            continue

        if len(raw) > max_length:
            error(f"too long: at most {max_length} characters")
            continue

        try:
            return parse(raw)
        except ValueError as exc:
            error(exc)


def _int_hint(minimum: int | None, maximum: int | None) -> str:
    if minimum is not None and maximum is not None:
        return f"enter a whole number from {minimum:,} to {maximum:,}"
    if minimum is not None:
        return f"enter a whole number of at least {minimum:,}"
    if maximum is not None:
        return f"enter a whole number of at most {maximum:,}"
    return "enter a whole number"


def _option_text(key: str, text: str) -> str:
    """("s", "Spin") -> "[S]pin"; ("x", "Cash out") -> "[X] Cash out"."""
    if len(key) == 1 and text[:1].lower() == key.lower():
        return f"[{text[0].upper()}]{text[1:]}"
    return f"[{key.upper()}] {text}"
