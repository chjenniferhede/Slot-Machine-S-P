"""The system as a whole: money conservation, interruptions, crashes and tampering.

The unit tests check each money movement on its own. These check that the
movements together lose and create nothing: several players, both games,
holds, cards in and out, with the totals checked after every step. Money
enters only through card deposits here, so the totals must match exactly.

Longer runs of the same checks are in test_soak.py.
"""

from __future__ import annotations

import functools
import itertools
import random
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from conftest import dump_all_text
from ledger import (
    CLASSIC,
    DELUXE,
    Ledger,
    Player,
    Table,
    assert_conserved,
    install_games,
    new_player,
)

from slotmachine import accounts, audit, storage
from slotmachine.accounts import AccountError, GameSession
from slotmachine.audit import Action
from slotmachine.game import GameSpec
from slotmachine.money import Money


@pytest.fixture()
def games(conn: sqlite3.Connection) -> dict[str, GameSpec]:
    return install_games(conn)


def spin_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0])


# ---------------------------------------------------------------------------
# Conservation
# ---------------------------------------------------------------------------


def test_conservation_with_holds(conn: sqlite3.Connection, games: object) -> None:
    """Three players, both games, holds, cards in and out, checked after every step.

    Holds are where an accounting slip is likeliest: the re-spin is charged
    at two bets but pays on one, so both must reach the books exactly.
    """
    ledger = Ledger()
    table = Table(conn, [new_player(conn, n) for n in ("ann", "ben", "cat")], ledger, random.Random(1))

    while table.spins < 1_000:
        assert table.turn()
        assert_conserved(conn, ledger)
    table.cash_everyone_out()

    h = assert_conserved(conn, ledger)
    assert h.credits == Money(0)
    assert spin_count(conn) == table.spins
    assert table.counts["hold"] > 50
    for what in ("deposit", "withdraw", "buyin", "cashout"):
        assert table.counts.get(what, 0) > 0, f"the script never got to {what}"
    assert audit.verify_chain(conn).ok


def test_conservation_with_concurrent_sessions(
    conn: sqlite3.Connection, games: object, tmp_path: Path
) -> None:
    """Four players at once, each on their own connection, as four terminals would be.

    While they play, this thread keeps checking the totals from a fifth
    connection. Each check reads one snapshot, so a movement half-seen
    from another connection would show as money missing.
    """
    players = [new_player(conn, f"p{i}") for i in range(4)]

    def play(index: int) -> Ledger:
        own = storage.connect(tmp_path / "test.db")
        try:
            table = Table(own, [players[index]], Ledger(), random.Random(index))
            table.play(spins=150)
            table.cash_everyone_out()
            return table.ledger
        finally:
            own.close()

    checks_while_playing = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(play, i) for i in range(4)]
        while not all(f.done() for f in futures):
            assert_conserved(conn)
            checks_while_playing = checks_while_playing + 1
        ledgers = [f.result() for f in futures]  # re-raises anything a player hit

    h = assert_conserved(conn, Ledger.combined(ledgers))
    assert h.credits == Money(0)
    assert spin_count(conn) >= 4 * 150
    assert checks_while_playing > 10
    assert audit.verify_chain(conn).ok


_ALL_OF_IT = Money(10_000)


def _withdraw_all(player: Player, conn: sqlite3.Connection, ledger: Ledger) -> None:
    ledger.withdraw(conn, player, _ALL_OF_IT)


def _buy_in_with_all(player: Player, conn: sqlite3.Connection, ledger: Ledger) -> None:
    accounts.start_session(player.session, conn, CLASSIC, _ALL_OF_IT)


@pytest.mark.parametrize(
    "moves",
    [
        pytest.param((_withdraw_all, _withdraw_all), id="withdraw-twice"),
        pytest.param((_buy_in_with_all, _buy_in_with_all), id="buy-in-twice"),
        pytest.param((_withdraw_all, _buy_in_with_all), id="withdraw-and-buy-in"),
    ],
)
def test_same_player_on_two_terminals_cannot_spend_twice(
    conn: sqlite3.Connection,
    games: object,
    tmp_path: Path,
    moves: tuple[Callable[[Player, sqlite3.Connection, Ledger], None], ...],
) -> None:
    """Two connections race to spend the same $100. One wins; the other is refused.

    Both ask for the whole $100 at the same moment, as near as two threads
    can manage. The balance is checked in the transaction that spends it, so
    the second one sees the first one's spend (TOCTOU, CWE-367).
    """
    alice = new_player(conn, "alice")
    ledger = Ledger()
    for _ in range(20):
        ledger.deposit(conn, alice, _ALL_OF_IT)
        start = threading.Barrier(2)

        def race(move: Callable[[Player, sqlite3.Connection, Ledger], None]) -> str:
            own = storage.connect(tmp_path / "test.db")
            try:
                start.wait()
                move(alice, own, ledger)
                return "spent"
            except AccountError:
                return "refused"
            finally:
                own.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = sorted(pool.map(race, moves))
        assert results == ["refused", "spent"]
        assert_conserved(conn, ledger)

        # Put the $100 back on the card, wherever the winner left it.
        open_game = storage.get_open_session(conn, alice.session.user_id)
        if open_game is not None:
            accounts.cash_out(alice.session, conn, GameSession(open_game["id"], CLASSIC))
        if accounts.get_balance(alice.session, conn).is_positive():
            ledger.withdraw(conn, alice, _ALL_OF_IT)

    assert accounts.get_balance(alice.session, conn) == Money(0)
    assert_conserved(conn, ledger)


# ---------------------------------------------------------------------------
# Ctrl-C at every step
# ---------------------------------------------------------------------------


class _Interruptible(sqlite3.Connection):
    """A connection that raises KeyboardInterrupt in place of one chosen statement.

    Python delivers Ctrl-C between statements, never inside one, so stopping
    before each statement in turn tries every point a real Ctrl-C can land.
    """

    interrupt_at: int | None = None
    statements = 0

    def arm(self, statement: int) -> None:
        self.statements = 0
        self.interrupt_at = statement

    def disarm(self) -> None:
        self.interrupt_at = None

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        self.statements = self.statements + 1
        if self.statements == self.interrupt_at:
            raise KeyboardInterrupt
        return super().execute(sql, parameters)


def _open_interruptible(path: Path, monkeypatch: pytest.MonkeyPatch) -> _Interruptible:
    """storage.connect, with its settings, but handing back an _Interruptible."""
    with monkeypatch.context() as m:
        m.setattr(sqlite3, "connect", functools.partial(sqlite3.connect, factory=_Interruptible))
        conn = storage.connect(path)
    assert isinstance(conn, _Interruptible)
    return conn


@pytest.mark.parametrize("operation", ["deposit", "withdraw", "buy_in", "spin", "hold", "cash_out"])
def test_keyboardinterrupt_at_every_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Ctrl-C before any statement of a money movement leaves nothing changed.

    Every table is compared, not only the money: no half-written spin, no
    audit entry for something that did not happen, and the connection is
    not left inside a transaction. Then the movement is run to the end, once.
    """
    conn = _open_interruptible(tmp_path / "casino.db", monkeypatch)
    storage.init_db(conn)
    install_games(conn)
    ledger = Ledger()
    alice = new_player(conn, "alice")
    ledger.deposit(conn, alice, Money(10_000))
    if operation in ("spin", "hold", "cash_out"):
        alice.game = accounts.start_session(alice.session, conn, CLASSIC, Money(5_000))

    def game() -> GameSession:
        assert alice.game is not None
        return alice.game

    if operation == "hold":
        accounts.spin(alice.session, conn, game(), Money(100))

    moves: dict[str, Callable[[], object]] = {
        "deposit": lambda: ledger.deposit(conn, alice, Money(500)),
        "withdraw": lambda: ledger.withdraw(conn, alice, Money(500)),
        "buy_in": lambda: accounts.start_session(alice.session, conn, DELUXE, Money(5_000)),
        "spin": lambda: accounts.spin(alice.session, conn, game(), Money(100)),
        "hold": lambda: accounts.hold_and_respin(alice.session, conn, game(), [0]),
        "cash_out": lambda: accounts.cash_out(alice.session, conn, game()),
    }
    move = moves[operation]

    before = dump_all_text(conn)
    for statement in itertools.count(1):
        conn.arm(statement)
        try:
            move()
        except KeyboardInterrupt:
            conn.disarm()
            assert not conn.in_transaction, f"left in a transaction at statement {statement}"
            assert dump_all_text(conn) == before, f"Ctrl-C at statement {statement} changed something"
            continue
        conn.disarm()
        break

    assert statement > 5, "the movement ran too few statements for this to test anything"
    assert dump_all_text(conn) != before  # and uninterrupted, it did happen
    assert_conserved(conn, ledger)
    assert audit.verify_chain(conn).ok
    conn.close()


# ---------------------------------------------------------------------------
# The process killed mid-spin
# ---------------------------------------------------------------------------

# Run in a separate process, which dies without warning in the middle of a
# spin: after the bet is taken and the spin row written, before the payout.
# os._exit skips every except and finally block, so no rollback code runs;
# only SQLite's own journal can keep the database whole.
_DIE_MID_SPIN = """
import os
import sys
from types import SimpleNamespace

from slotmachine import accounts, storage
from slotmachine.accounts import GameSession
from slotmachine.money import Money

db, user_id, username, session_id, game_id, spins_first = sys.argv[1:]
player = SimpleNamespace(user_id=int(user_id), username=username, role="player")
game = GameSession(id=int(session_id), game_id=game_id)
conn = storage.connect(db)
for _ in range(int(spins_first)):
    accounts.spin(player, conn, game, Money(100))

record_spin = storage.record_spin

def record_then_die(*args, **kwargs):
    record_spin(*args, **kwargs)
    os._exit(9)

storage.record_spin = record_then_die
accounts.spin(player, conn, game, Money(100))
sys.exit("the spin finished, but the process should have died inside it")
"""


def _play_then_die(db: Path, player: Player, spins_first: int) -> None:
    assert player.game is not None
    args = [db, player.session.user_id, player.session.username, player.game.id, player.game.game_id]
    died = subprocess.run(
        [sys.executable, "-c", _DIE_MID_SPIN, *map(str, args), str(spins_first)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert died.returncode == 9, died.stderr  # 9 means it died at the planted point


def test_kill_mid_spin(conn: sqlite3.Connection, games: object, tmp_path: Path) -> None:
    """A process killed between the bet and the payout leaves no trace of the spin."""
    ledger = Ledger()
    alice = new_player(conn, "alice")
    ledger.deposit(conn, alice, Money(10_000))
    alice.game = accounts.start_session(alice.session, conn, CLASSIC, Money(5_000))
    entries_before = len(audit.read(conn, limit=1_000))

    _play_then_die(tmp_path / "test.db", alice, spins_first=0)

    assert accounts.get_credits(alice.session, conn, alice.game) == Money(5_000)
    assert spin_count(conn) == 0
    assert len(audit.read(conn, limit=1_000)) == entries_before
    assert audit.verify_chain(conn).ok
    assert_conserved(conn, ledger)

    # The dead process held the write lock. It must be free again.
    accounts.spin(alice.session, conn, alice.game, Money(100))
    assert spin_count(conn) == 1


def test_orphan_session_recovery(conn: sqlite3.Connection, games: object, tmp_path: Path) -> None:
    """After a crash, the next start cashes out the session it left open, and logs it."""
    ledger = Ledger()
    alice = new_player(conn, "alice")
    ledger.deposit(conn, alice, Money(10_000))
    alice.game = accounts.start_session(alice.session, conn, CLASSIC, Money(5_000))

    _play_then_die(tmp_path / "test.db", alice, spins_first=20)
    left_in_machine = accounts.get_credits(alice.session, conn, alice.game)
    assert spin_count(conn) == 20  # the 21st died half done and left nothing

    restarted = storage.connect(tmp_path / "test.db")
    try:
        assert accounts.recover_open_sessions(restarted) == 1
    finally:
        restarted.close()

    assert storage.get_open_session(conn, alice.session.user_id) is None
    assert accounts.get_balance(alice.session, conn) == Money(5_000) + left_in_machine
    [entry] = audit.read(conn, action=Action.SESSION_RECOVERED)
    assert entry["actor"] == "system"
    assert f"amount={left_in_machine} " in entry["detail"]
    assert_conserved(conn, ledger)
    assert audit.verify_chain(conn).ok


# ---------------------------------------------------------------------------
# Tampering with the audit log
# ---------------------------------------------------------------------------


@pytest.fixture()
def history(conn: sqlite3.Connection, games: object) -> int:
    """A database with real history behind it. Returns the number of audit entries."""
    ledger = Ledger()
    Table(conn, [new_player(conn, n) for n in ("ann", "ben")], ledger, random.Random(7)).play(60)
    entries = len(audit.read(conn, limit=10_000))
    assert entries > 60
    assert audit.verify_chain(conn).ok
    return entries


def _attacker(db: Path) -> sqlite3.Connection:
    """A plain sqlite3 connection, as the sqlite3 shell would open. None of our code."""
    return sqlite3.connect(db)


def test_tamper_detected_end_to_end(
    conn: sqlite3.Connection, history: int, tmp_path: Path
) -> None:
    """The demo: edit entry 50 by hand, and the check names entry 50.

    The schema refuses the edit first, so the attacker has to drop the
    trigger, as the demo script must too.
    """
    attacker = _attacker(tmp_path / "test.db")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        attacker.execute("UPDATE audit_log SET detail = 'nothing happened' WHERE id = 50")
    attacker.execute("DROP TRIGGER audit_log_is_immutable")
    attacker.execute("UPDATE audit_log SET detail = 'nothing happened' WHERE id = 50")
    attacker.commit()
    attacker.close()

    result = audit.verify_chain(conn)
    assert (result.ok, result.first_bad_id, result.reason) == (False, 50, "hash mismatch")


@pytest.mark.parametrize(
    ("tamper", "first_bad_id", "reason"),
    [
        pytest.param(
            # Also work out the entry's new hash, as someone who has read audit.py would.
            "UPDATE audit_log SET detail = 'nothing happened',"
            " hash = :rehash WHERE id = 50",
            51,
            "broken link",
            id="edit-and-rehash",
        ),
        pytest.param("DELETE FROM audit_log WHERE id = 50", 51, "id gap", id="delete-one"),
    ],
)
def test_tamper_variants_detected(
    conn: sqlite3.Connection, history: int, tmp_path: Path, tamper: str, first_bad_id: int, reason: str
) -> None:
    row = audit.read(conn, limit=10_000)[-50]  # newest first, so this is entry 50
    assert row["id"] == 50
    rehash = audit._entry_hash(row["prev_hash"], row["ts"], row["actor"], row["action"], "nothing happened")

    attacker = _attacker(tmp_path / "test.db")
    attacker.execute("DROP TRIGGER audit_log_is_immutable")
    attacker.execute("DROP TRIGGER audit_log_cannot_be_deleted")
    attacker.execute(tamper, {"rehash": rehash} if ":rehash" in tamper else {})
    attacker.commit()
    attacker.close()

    result = audit.verify_chain(conn)
    assert (result.ok, result.first_bad_id, result.reason) == (False, first_bad_id, reason)


def test_tamper_the_chain_cannot_see(
    conn: sqlite3.Connection, history: int, tmp_path: Path
) -> None:
    """What the chain cannot catch, written down so nobody claims otherwise in the exam.

    The hashes have no secret key and the newest hash is kept nowhere else,
    so someone who can write the file can delete the newest entries, or
    rewrite every entry from an edit onwards, and the chain still checks
    out. Only a copy of the newest hash kept out of their reach would show it.
    """
    attacker = _attacker(tmp_path / "test.db")
    attacker.row_factory = sqlite3.Row
    attacker.execute("DROP TRIGGER audit_log_is_immutable")
    attacker.execute("DROP TRIGGER audit_log_cannot_be_deleted")

    attacker.execute("DELETE FROM audit_log WHERE id > ?", (history - 5,))
    previous = attacker.execute("SELECT hash FROM audit_log WHERE id = 49").fetchone()["hash"]
    for row in list(attacker.execute("SELECT * FROM audit_log WHERE id >= 50 ORDER BY id")):
        detail = "nothing happened" if row["id"] == 50 else row["detail"]
        new_hash = audit._entry_hash(previous, row["ts"], row["actor"], row["action"], detail)
        attacker.execute(
            "UPDATE audit_log SET detail = ?, prev_hash = ?, hash = ? WHERE id = ?",
            (detail, previous, new_hash, row["id"]),
        )
        previous = new_hash
    attacker.commit()
    attacker.close()

    result = audit.verify_chain(conn)
    assert result.ok
    assert result.entries_checked == history - 5
