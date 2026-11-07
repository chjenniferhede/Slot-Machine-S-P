"""Shared test fixtures."""

from __future__ import annotations

import sqlite3
import sys
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass, field

import pytest

from slotmachine import storage
from slotmachine.money import Money


@dataclass(frozen=True)
class StubSession:
    """Stands in for Layer 2's auth.Session until it lands. Same fields."""

    user_id: int
    username: str
    role: str = "player"


def make_player(conn: sqlite3.Connection, username: str) -> StubSession:
    """A player with an empty account, and a session for them."""
    with storage.transaction(conn):
        uid = storage.create_user(conn, username, "not-a-real-hash", "player")
        storage.create_account(conn, uid)
    return StubSession(uid, username)


@pytest.fixture()
def conn(tmp_path) -> Generator[sqlite3.Connection, None, None]:
    """A fresh, initialised database per test.

    A real file rather than ':memory:' so that WAL mode and the file-backed
    behaviour under test match production.
    """
    db = tmp_path / "test.db"
    c = storage.connect(db)
    storage.init_db(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def player(conn: sqlite3.Connection) -> int:
    """A player with an account and a $200.00 balance."""
    with storage.transaction(conn):
        uid = storage.create_user(conn, "alice", "not-a-real-hash", "player")
        storage.create_account(conn, uid)
        storage.adjust_balance(conn, uid, Money(20_000))
    return uid


@pytest.fixture()
def alice(player: int) -> StubSession:
    """A session for the ``player`` fixture's user."""
    return StubSession(player, "alice")


Keystrokes = str | BaseException | type[BaseException]


@dataclass
class Keyboard:
    """Someone at the terminal, answering input() and getpass() from a script.

    Each answer is a line of text, or an exception to raise in its place:
    KeyboardInterrupt is Ctrl-C. Once the script runs out, the next prompt
    raises EOFError, as Ctrl-D or the end of piped input would.

    Each prompt is written to stdout followed by a newline, so captured output
    reads line by line. The answers are not written: a real terminal echoes
    typed text itself, not through stdout, and getpass does not echo at all.
    """

    script: deque[Keystrokes] = field(default_factory=deque)
    prompts: list[str] = field(default_factory=list)
    secret_prompts: list[str] = field(default_factory=list)

    def type(self, *answers: Keystrokes) -> None:
        self.script.extend(answers)

    def input(self, prompt: object = "") -> str:
        return self._answer(str(prompt))

    def getpass(self, prompt: str = "Password: ", stream: object = None) -> str:
        self.secret_prompts.append(prompt)
        return self._answer(prompt)

    def _answer(self, prompt: str) -> str:
        sys.stdout.write(prompt + "\n")
        self.prompts.append(prompt)
        if not self.script:
            raise EOFError
        answer = self.script.popleft()
        if isinstance(answer, str):
            return answer
        raise answer


@pytest.fixture()
def keyboard(monkeypatch: pytest.MonkeyPatch) -> Keyboard:
    """Scripted answers for every input() and getpass() call in the test."""
    kb = Keyboard()
    monkeypatch.setattr("builtins.input", kb.input)
    monkeypatch.setattr("getpass.getpass", kb.getpass)
    return kb


def dump_all_text(conn: sqlite3.Connection) -> str:
    """Every value in every table, concatenated.

    Used to assert that a secret -- a card number, a CVV, a password -- appears
    nowhere in storage. It lives in the test suite rather than in the package
    because it interpolates a table name into SQL, which production code is
    never permitted to do; here the names come from sqlite_master, not from
    user input, and SQLite cannot parameterise an identifier.
    """
    out: list[str] = []
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table in tables:
        for row in conn.execute(f"SELECT * FROM {table['name']}"):  # noqa: S608
            out.extend(str(v) for v in tuple(row))
    return "\n".join(out)
