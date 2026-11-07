"""Menus, tables, and turning every kind of failure into one clean line."""

from __future__ import annotations

import io
import sqlite3
import sys
from collections.abc import Callable

import pytest
from conftest import Keyboard, StubSession

from slotmachine import accounts, storage
from slotmachine.accounts import AccountError
from slotmachine.audit import Action
from slotmachine.cli.menus import UNEXPECTED, MenuItem, menu, run, table
from slotmachine.cli.prompts import prompt_int, success
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money, MoneyError
from slotmachine.payment import PaymentError
from slotmachine.tools.rtpcalc import load_shipped_specs


def printable(out: str) -> bool:
    return all(ch.isprintable() or ch == "\n" for ch in out)


def raiser(exc: BaseException | type[BaseException]) -> Callable[..., None]:
    """A function that takes any arguments and raises ``exc``."""

    def action(*args: object, **kwargs: object) -> None:
        raise exc

    return action


# ---------------------------------------------------------------------------
# The menu loop
# ---------------------------------------------------------------------------


def test_menu_numbers_items_and_back_is_last(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    keyboard.type("1", "2", "3")
    menu(
        "MY ACCOUNT",
        [
            MenuItem("Deposit", lambda: calls.append("deposit")),
            MenuItem("Withdraw", lambda: calls.append("withdraw")),
        ],
    )
    assert calls == ["deposit", "withdraw"]
    out = capsys.readouterr().out
    assert "MY ACCOUNT" in out
    assert "1. Deposit" in out and "2. Withdraw" in out and "3. Back" in out


def test_menu_back_label(keyboard: Keyboard, capsys: pytest.CaptureFixture[str]) -> None:
    keyboard.type("2")
    menu("PLAYER", [MenuItem("Play a game", lambda: None)], back_label="Log out")
    assert "2. Log out" in capsys.readouterr().out


def test_menu_shows_who_is_logged_in(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    keyboard.type("1", "1")
    menu("TECHNICIAN", [], StubSession(1, "tech-admin"))
    menu("TECHNICIAN", [], StubSession(2, "evil\x1b[2J"))
    out = capsys.readouterr().out
    assert "TECHNICIAN — tech-admin" in out
    assert printable(out)


def test_menu_asks_again_on_a_bad_choice(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []
    keyboard.type("0", "3", "x", "", "-1", "1", "2")
    menu("MENU", [MenuItem("Thing", lambda: calls.append("thing"))])
    assert calls == ["thing"]
    assert capsys.readouterr().out.count("enter a whole number from 1 to 2") == 5


def test_ctrl_c_in_an_action_goes_back_to_the_menu(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    keyboard.type("1", "2")
    menu("MENU", [MenuItem("Slow thing", raiser(KeyboardInterrupt))])
    out = capsys.readouterr().out
    assert "Cancelled." in out
    assert out.count("1. Slow thing") == 2  # the menu came back


def test_ctrl_c_at_the_menu_prompt_leaves_the_menu(keyboard: Keyboard) -> None:
    keyboard.type(KeyboardInterrupt)
    menu("MENU", [MenuItem("Thing", lambda: None)])  # returns rather than raising


@pytest.mark.parametrize(
    "exc",
    [
        MoneyError("amount is too large"),
        AccountError("you have only $5.00 in your account"),
        PaymentError("this card has expired"),
    ],
)
def test_a_refusal_is_one_line(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    keyboard.type("1", "2")
    menu("MENU", [MenuItem("Try it", raiser(exc))])
    out = capsys.readouterr().out
    assert f"  ✗ {exc}\n" in out
    assert "Traceback" not in out
    assert out.count("1. Try it") == 2


def test_a_refusal_message_is_cleaned(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    keyboard.type("1", "2")
    menu("MENU", [MenuItem("Try it", raiser(AccountError("no \x1b[2J such \u202e card")))])
    assert printable(capsys.readouterr().out)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError(r"C:\Users\dev\slotmachine\secret.py, line 42"),
        sqlite3.OperationalError("no such table: users"),
        ValueError("invalid literal for int() with base 10: 'x'"),
        ZeroDivisionError("division by zero"),
        KeyError("pw_hash"),
    ],
)
def test_a_bug_says_nothing_about_itself(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str], exc: Exception
) -> None:
    keyboard.type("1", "2")
    menu("MENU", [MenuItem("Try it", raiser(exc))])
    out = capsys.readouterr().out
    assert UNEXPECTED in out
    assert str(exc) not in out
    assert type(exc).__name__ not in out
    assert "Traceback" not in out
    assert out.count("1. Try it") == 2  # and the menu carries on


def test_eof_is_not_mistaken_for_a_bug(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    """EOFError is an Exception, so the catch-all must let it past."""
    keyboard.type("1")  # then input ends while the action is asking
    status = run(lambda: menu("MENU", [MenuItem("Ask", lambda: prompt_int("Amount"))]))
    out = capsys.readouterr().out
    assert status == 0
    assert "Goodbye." in out
    assert UNEXPECTED not in out


def test_eof_deep_in_nested_menus_exits_cleanly(
    keyboard: Keyboard, capsys: pytest.CaptureFixture[str]
) -> None:
    inner = MenuItem("Inner", lambda: menu("INNER", [MenuItem("Thing", lambda: None)]))
    keyboard.type("1", "1")
    status = run(lambda: menu("OUTER", [inner], back_label="Quit"))
    out = capsys.readouterr().out
    assert status == 0
    assert "Goodbye." in out
    assert "Traceback" not in out


@pytest.mark.parametrize(
    ("app", "status", "shown"),
    [
        (lambda: None, 0, "Goodbye."),
        (raiser(KeyboardInterrupt), 130, "Goodbye."),
        (raiser(AccountError("you already have a game open")), 1, "you already have a game open"),
        (raiser(RuntimeError("internal detail")), 1, UNEXPECTED),
    ],
)
def test_run_exit_status(
    capsys: pytest.CaptureFixture[str], app: Callable[[], object], status: int, shown: str
) -> None:
    assert run(app) == status
    out = capsys.readouterr().out
    assert shown in out
    assert "internal detail" not in out
    assert "Traceback" not in out


# ---------------------------------------------------------------------------
# Ctrl-C mid-spin: the menu must let it reach the transaction, which rolls back
# ---------------------------------------------------------------------------


def test_ctrl_c_mid_spin_rolls_back(
    conn: sqlite3.Connection,
    alice: StubSession,
    keyboard: Keyboard,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for spec in load_shipped_specs():
        storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
    gs = accounts.start_session(alice, conn, "classic_sevens", Money(1_000))

    # Ctrl-C lands after the bet has come off the credits and before the spin
    # is recorded: the worst moment, with money half-moved.
    real_record_spin = storage.record_spin
    monkeypatch.setattr(storage, "record_spin", raiser(KeyboardInterrupt))

    keyboard.type("1", "2")
    menu("PLAY", [MenuItem("Spin", lambda: accounts.spin(alice, conn, gs, Money(100)))])
    assert "Cancelled." in capsys.readouterr().out

    assert accounts.get_credits(alice, conn, gs) == Money(1_000)
    assert conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0
    spins_audited = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ?", (Action.SPIN,)
    ).fetchone()[0]
    assert spins_audited == 0

    # Nothing was left half-open: the next spin goes through normally.
    monkeypatch.setattr(storage, "record_spin", real_record_spin)
    outcome = accounts.spin(alice, conn, gs, Money(100))
    assert outcome.credits == Money(1_000) - Money(100) + outcome.payout


# ---------------------------------------------------------------------------
# Output the terminal cannot encode
# ---------------------------------------------------------------------------


def test_characters_the_code_page_lacks_do_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirected output on Windows is in the locale's code page, and cp1252 has no '✓' or '╔'."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")  # strict, as Python opens it
    monkeypatch.setattr(sys, "stdout", out)

    assert run(lambda: success("Chain intact ╔═╗")) == 0

    out.flush()
    assert "? Chain intact ???" in out.buffer.getvalue().decode("cp1252")


def test_bytes_that_are_not_text_do_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Piped input is decoded strictly too; an invalid byte must not stop the program."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"\xff\xfe\x80\n7\n"), "utf-8"))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8"))

    answers: list[int] = []
    assert run(lambda: answers.append(prompt_int("Pick"))) == 0
    assert answers == [7]


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

GAMES_HEADERS = ["ID", "NAME", "STATUS", "SPINS", "WAGERED", "RTP"]
GAMES_ROWS = [
    ["classic_sevens", "Classic Sevens", "enabled", "12,043", Money(1_420_300), "94.1%"],
    ["five_line_deluxe", "Five Line Deluxe", "disabled", "8,771", Money(3_811_000), "92.8%"],
]


def test_table_lines_up_columns() -> None:
    lines = table(GAMES_HEADERS, GAMES_ROWS).splitlines()
    header, first, second = lines

    assert all(line.startswith("  ") for line in lines)
    # Text columns are left-aligned: they start together.
    assert header.index("NAME") == first.index("Classic Sevens") == second.index("Five Line")
    assert header.index("STATUS") == first.index("enabled") == second.index("disabled")
    # Number columns are right-aligned: they end together.
    assert header.index("SPINS") + 5 == first.index("12,043") + 6 == second.index("8,771") + 5
    assert first.index("$14,203.00") + 10 == second.index("$38,110.00") + 10
    assert header.endswith("RTP") and first.endswith("94.1%") and second.endswith("92.8%")


def test_table_footer() -> None:
    lines = table(GAMES_HEADERS, GAMES_ROWS, footer=[["TOTAL", "", "", "20,814", "", "93.6%"]])
    *_, rule, total = lines.splitlines()
    assert set(rule.strip()) == {"-"}
    assert total.startswith("  TOTAL") and total.endswith("93.6%")


def test_table_align_override() -> None:
    lines = table(["N", "WHO"], [["1", "alice"], ["100", "bob"]], align="<<").splitlines()
    assert lines[1].startswith("  1 ")


def test_table_cleans_every_cell() -> None:
    text = table(["ACTOR\x1b[2J", "DETAIL"], [["evil\u202e", "a\rb\x07"]], footer=[["\x9b", ""]])
    assert printable(text)


@pytest.mark.parametrize(
    ("rows", "kwargs"),
    [([["only one"]], {}), ([["a", "b", "c"]], {}), ([["a", "b"]], {"align": "<"})],
)
def test_table_refuses_a_bad_shape(rows: list[list[str]], kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        table(["A", "B"], rows, **kwargs)
