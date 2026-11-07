"""Menus, tables, and the one place where errors become messages.

Each role's interface is a set of nested menus. A menu action that fails is
turned into one line here and the menu carries on:

- A refusal from a lower layer (MoneyError, AccountError, PaymentError) is
  shown as its message, which those layers write to be shown as-is.
- Anything else is a bug. The user gets a fixed line that says nothing about
  the cause: a traceback would show file paths, library versions and the
  shape of the code (CWE-209).
- Ctrl-C cancels the action and goes back to the menu. It has already passed
  through any open storage.transaction, which rolled back.
- EOFError (Ctrl-D, or the end of piped input) is not caught: no more input
  will ever come, so it goes up to run(), which says goodbye and exits.

run() wraps a whole interface the same way, for anything that escapes a menu.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from slotmachine.accounts import AccountError
from slotmachine.cli.prompts import INDENT, clean, error, info, prompt_int
from slotmachine.money import MoneyError
from slotmachine.payment import Caller, PaymentError

# Errors whose message is written to be shown to the user as it is. Layer 2
# adds AccessDenied and AuthError here when it lands.
USER_ERRORS: Final[tuple[type[Exception], ...]] = (MoneyError, AccountError, PaymentError)

UNEXPECTED: Final[str] = "Something went wrong. Please try again."

# A cell that reads as a number, amount, percentage or multiple, such as
# "12,043", "-$23.75", "94.1%" or "4.82x". Columns of these are right-aligned.
_NUMERIC: Final[re.Pattern[str]] = re.compile(r"[-+]?\$?[0-9][0-9,]*(?:\.[0-9]+)?[%x]?")


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: Callable[[], object]


def menu(
    title: str,
    items: Sequence[MenuItem],
    session: Caller | None = None,
    *,
    back_label: str = "Back",
) -> None:
    """Show ``items`` as a numbered list until the user picks the last entry.

    The last entry is always ``back_label`` ("Back", "Log out", "Quit"), so
    every menu is left the same way. Ctrl-C at the menu's own prompt leaves it
    too. With ``session``, the title shows who is logged in.
    """
    back = len(items) + 1
    while True:
        print()
        heading = title if session is None else f"{title} — {session.username}"
        print(f"{INDENT}{clean(heading)}")
        for number, item in enumerate(items, start=1):
            print(f"{INDENT}  {number}. {clean(item.label)}")
        print(f"{INDENT}  {back}. {clean(back_label)}")

        try:
            choice = prompt_int("Choose", minimum=1, maximum=back)
        except KeyboardInterrupt:
            print()
            return
        if choice == back:
            return
        run_action(items[choice - 1].action)


def run_action(action: Callable[[], object]) -> None:
    """Run one menu action, and turn whatever goes wrong into one line."""
    try:
        action()
    except EOFError:
        # An Exception, so it must be let through before the catch-all below.
        raise
    except KeyboardInterrupt:
        print()
        info("Cancelled.")
    except USER_ERRORS as exc:
        error(exc)
    except Exception:
        error(UNEXPECTED)


def run(app: Callable[[], object]) -> int:
    """Run a whole interface, and return the exit status for the process.

    Nothing raised inside ``app`` reaches the terminal as a traceback.
    """
    _make_streams_safe()
    try:
        app()
    except EOFError:
        print()
        info("Goodbye.")
        return 0
    except KeyboardInterrupt:
        print()
        info("Goodbye.")
        return 130  # the usual status for a program stopped by Ctrl-C
    except USER_ERRORS as exc:
        error(exc)
        return 1
    except Exception:
        error(UNEXPECTED)
        return 1
    info("Goodbye.")
    return 0


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    footer: Sequence[Sequence[object]] = (),
    align: str | None = None,
    indent: str = INDENT,
) -> str:
    """Rows lined up in columns, as one string ready to print.

    Every cell goes through clean(), since cells often hold usernames, audit
    details and other values that came from users. ``align`` has one
    character per column, "<" for left or ">" for right; without it, a column
    is right-aligned when every cell in it reads as a number. ``footer`` rows,
    such as a TOTAL, are drawn below a rule.
    """
    columns = len(headers)
    head = [clean(cell) for cell in headers]
    body = [_clean_row(row, columns) for row in rows]
    foot = [_clean_row(row, columns) for row in footer]

    if align is None:
        align = "".join(_column_alignment([row[i] for row in body + foot]) for i in range(columns))
    elif len(align) != columns or set(align) - {"<", ">"}:
        raise ValueError("align needs one '<' or '>' per column")

    widths = [max(len(row[i]) for row in [head, *body, *foot]) for i in range(columns)]

    def line(cells: list[str]) -> str:
        parts = [
            cell.rjust(width) if side == ">" else cell.ljust(width)
            for cell, width, side in zip(cells, widths, align)
        ]
        return (indent + "   ".join(parts)).rstrip()

    lines = [line(head)] + [line(row) for row in body]
    if foot:
        lines.append(indent + "-" * (sum(widths) + 3 * (columns - 1)))
        lines.extend(line(row) for row in foot)
    return "\n".join(lines)


def _clean_row(row: Sequence[object], columns: int) -> list[str]:
    if len(row) != columns:
        raise ValueError(f"table row has {len(row)} cells, expected {columns}")
    return [clean(cell) for cell in row]


def _column_alignment(cells: list[str]) -> str:
    filled = [cell for cell in cells if cell]
    if filled and all(_NUMERIC.fullmatch(cell) for cell in filled):
        return ">"
    return "<"


def _make_streams_safe() -> None:
    """Make an unprintable character print as '?', and unreadable input read as '�'.

    A Windows console can show any character. Redirected to a file or a
    pipe, though, output is encoded in the locale's code page, and cp1252 has
    no '✓' or box-drawing characters at all: printing one would raise
    UnicodeEncodeError, and the program would stop with a traceback. Input
    has the same problem the other way round, with bytes that are not valid
    text, and those can be typed or piped in on purpose.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="replace")
