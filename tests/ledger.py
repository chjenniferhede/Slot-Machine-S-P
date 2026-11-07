"""Following the money through the whole database.

Used by test_integration.py and test_soak.py. Players here start with an
empty account and pay in by card, the way a real player does, so every cent
in the database came in through ``accounts.deposit``. That is what lets
``assert_conserved`` demand that the money adds up exactly.
"""

from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from conftest import StubSession, make_player

from slotmachine import accounts, payment, storage
from slotmachine.accounts import AccountError, GameSession, SpinOutcome
from slotmachine.audit import Action
from slotmachine.game import GameSpec
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import load_shipped_specs

CLASSIC = "classic_sevens"  # bets $0.25 / $1 / $5, holds: one reel, once, for two bets
DELUXE = "five_line_deluxe"  # bets $1 / $5 / $25, no holds

#: The card networks' published test number. It passes the Luhn check and
#: belongs to no one.
TEST_VISA = "4111 1111 1111 1111"


def install_games(conn: sqlite3.Connection) -> dict[str, GameSpec]:
    """Both shipped games, installed and enabled."""
    specs = {}
    for spec in load_shipped_specs():
        storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
        specs[spec.id] = spec
    return specs


# ---------------------------------------------------------------------------
# Players and what crossed the casino's edge
# ---------------------------------------------------------------------------


@dataclass
class Player:
    session: StubSession
    method_id: int  # the card they pay in and cash out with
    game: GameSession | None = None  # the open game, if any
    last: SpinOutcome | None = None  # the last spin in that game, for holds
    done: bool = False  # stopped for the day: out of money and at the deposit limit


def new_player(conn: sqlite3.Connection, username: str) -> Player:
    """A player with an empty account and one card."""
    session = make_player(conn, username)
    card = payment.CardInput(
        number=TEST_VISA, exp_month=12, exp_year=date.today().year + 3, cvv="123"
    )
    method = payment.add_payment_method(session, conn, card)
    return Player(session, method.id)


@dataclass
class Ledger:
    """What the test saw cross the casino's edge: card deposits in, withdrawals out.

    Updated only when the call returns, so a deposit that was refused or
    interrupted is not counted.
    """

    deposited: Money = field(default_factory=Money.zero)
    withdrawn: Money = field(default_factory=Money.zero)

    def deposit(self, conn: sqlite3.Connection, player: Player, amount: Money) -> None:
        accounts.deposit(player.session, conn, player.method_id, amount)
        self.deposited = self.deposited + amount

    def withdraw(self, conn: sqlite3.Connection, player: Player, amount: Money) -> None:
        accounts.withdraw(player.session, conn, player.method_id, amount)
        self.withdrawn = self.withdrawn + amount

    @classmethod
    def combined(cls, ledgers: list[Ledger]) -> Ledger:
        total = cls()
        for ledger in ledgers:
            total.deposited = total.deposited + ledger.deposited
            total.withdrawn = total.withdrawn + ledger.withdrawn
        return total


# ---------------------------------------------------------------------------
# Where every cent is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Holdings:
    """Where the money is, all read from one snapshot of the database."""

    balances: Money  # in players' accounts
    credits: Money  # in open game sessions
    left_in_closed: Money  # in closed sessions, which should hold nothing
    staked: Money  # bet on every spin, re-spin prices included
    paid: Money  # paid out by every spin
    # Totals of the amount= field of each kind of audit entry.
    deposited: Money
    withdrawn: Money
    bought_in: Money
    cashed_out: Money  # by the player, or by recovery after a crash

    @property
    def casino_profit(self) -> Money:
        return self.staked - self.paid

    def __str__(self) -> str:
        return (
            f"balances {self.balances}, credits {self.credits}, casino profit"
            f" {self.casino_profit} (staked {self.staked}, paid {self.paid});"
            f" audit log: deposited {self.deposited}, withdrawn {self.withdrawn},"
            f" bought in {self.bought_in}, cashed out {self.cashed_out}"
        )


_AMOUNT = re.compile(r"\bamount=(\$[0-9,]+\.[0-9]{2})(?: |$)")


def read_holdings(conn: sqlite3.Connection) -> Holdings:
    """Read every total in one read transaction.

    Other connections may be writing while this runs. Inside one transaction
    every query sees the database as it was at the first one, so the totals
    belong to the same moment and can be compared exactly.
    """
    conn.execute("BEGIN")
    try:
        return Holdings(
            balances=_total(conn, "SELECT SUM(balance_cents) FROM accounts"),
            credits=_total(conn, "SELECT SUM(credits_cents) FROM sessions WHERE ended_at IS NULL"),
            left_in_closed=_total(
                conn, "SELECT SUM(credits_cents) FROM sessions WHERE ended_at IS NOT NULL"
            ),
            staked=_total(conn, "SELECT SUM(bet_cents) FROM spins"),
            paid=_total(conn, "SELECT SUM(payout_cents) FROM spins"),
            deposited=_audit_total(conn, Action.DEPOSIT),
            withdrawn=_audit_total(conn, Action.WITHDRAW),
            bought_in=_audit_total(conn, Action.SESSION_BUYIN),
            cashed_out=_audit_total(conn, Action.SESSION_CASHOUT)
            + _audit_total(conn, Action.SESSION_RECOVERED),
        )
    finally:
        conn.execute("COMMIT")


def _total(conn: sqlite3.Connection, query: str) -> Money:
    return Money(int(conn.execute(query).fetchone()[0] or 0))


def _audit_total(conn: sqlite3.Connection, action: str) -> Money:
    total = Money(0)
    for (detail,) in conn.execute("SELECT detail FROM audit_log WHERE action = ?", (action,)):
        match = _AMOUNT.search(detail)
        assert match is not None, f"{action} entry has no amount: {detail!r}"
        total = total + Money.parse(match.group(1))
    return total


def assert_conserved(conn: sqlite3.Connection, ledger: Ledger | None = None) -> Holdings:
    """Every cent is accounted for, exactly. Integer cents do not round.

    1. The money in the system is what came in by card less what went out:
       balances + open credits + casino profit == deposits - withdrawals.
    2. The money that went into the machines is still in them or was won by
       the casino: buy-ins - cash-outs == open credits + casino profit.
    3. A closed session holds nothing, so no money is counted twice.
    4. If the test kept a ledger, the audit log's deposits and withdrawals
       match it, so none was left out of the log.

    Without a ledger this can run while other connections are playing, since
    everything it compares comes from one snapshot.
    """
    h = read_holdings(conn)
    in_system = h.balances + h.credits + h.casino_profit
    net_in = h.deposited - h.withdrawn
    assert in_system == net_in, f"{in_system} in the system, but {net_in} came in net: {h}"
    in_machines = h.bought_in - h.cashed_out
    assert in_machines == h.credits + h.casino_profit, (
        f"{in_machines} went into the machines net, but they hold"
        f" {h.credits + h.casino_profit}: {h}"
    )
    assert h.left_in_closed == Money(0), f"closed sessions still hold {h.left_in_closed}"
    if ledger is not None:
        assert (h.deposited, h.withdrawn) == (ledger.deposited, ledger.withdrawn), (
            f"the test deposited {ledger.deposited} and withdrew {ledger.withdrawn}: {h}"
        )
    return h


# ---------------------------------------------------------------------------
# Players playing
# ---------------------------------------------------------------------------


class Calendar:
    """Days passing, so the daily limits start again. Stands in for storage.today_bucket."""

    def __init__(self) -> None:
        self.day = 0

    def today(self) -> str:
        return f"day-{self.day:05d}"

    def next_day(self) -> None:
        self.day = self.day + 1


@dataclass
class Table:
    """Players coming and going at the machines, by a seeded script.

    The script decides what each player does next. What the reels show comes
    from the real RNG, so every run plays differently and the checks must
    hold whatever happens. With a calendar, a player turned away at the
    daily deposit limit comes back the next day; without one, they stop.
    """

    conn: sqlite3.Connection
    players: list[Player]
    ledger: Ledger
    script: random.Random
    calendar: Calendar | None = None
    specs: dict[str, GameSpec] = field(default_factory=lambda: {s.id: s for s in load_shipped_specs()})
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def spins(self) -> int:
        return self.counts.get("spin", 0) + self.counts.get("hold", 0)

    def play(self, spins: int) -> None:
        """Play until ``spins`` more spins are done, or everyone has stopped."""
        target = self.spins + spins
        while self.spins < target and self.turn():
            pass

    def turn(self) -> bool:
        """One thing done by one player. False once nobody can play on."""
        playing = [p for p in self.players if not p.done]
        if not playing:
            return False
        player = self.script.choice(playing)
        if player.game is None:
            self._between_games(player)
        else:
            self._at_the_machine(player, player.game)
        return True

    def cash_everyone_out(self) -> None:
        for player in self.players:
            if player.game is not None:
                self._cash_out(player, player.game)

    def _at_the_machine(self, player: Player, game: GameSession) -> None:
        credits = accounts.get_credits(player.session, self.conn, game)
        spec = self.specs[game.game_id]
        if self.script.random() < 0.05:
            self._cash_out(player, game)
            return

        last = player.last
        if (
            last is not None
            and last.respin_cost is not None
            and last.respin_cost <= credits
            and self.script.random() < 0.3
        ):
            reel = self.script.randrange(spec.reel_count)
            player.last = accounts.hold_and_respin(player.session, self.conn, game, [reel])
            self._count("hold")
            return

        tiers = [Money(tier) for tier in spec.bet_tiers if Money(tier) <= credits]
        if not tiers:
            self._cash_out(player, game)
            return
        # Mostly the smallest bet, sometimes more, as real players do.
        bet = self.script.choices(tiers, weights=[70, 25, 5][: len(tiers)])[0]
        player.last = accounts.spin(player.session, self.conn, game, bet)
        self._count("spin")

    def _between_games(self, player: Player) -> None:
        balance = accounts.get_balance(player.session, self.conn)
        if balance.cents > 2_000 and self.script.random() < 0.3:
            amount = Money(self.script.randint(1, balance.cents // 2))
            try:
                self.ledger.withdraw(self.conn, player, amount)
                self._count("withdraw")
            except AccountError as refused:
                assert "daily withdrawal limit" in str(refused), refused
            return

        if balance.cents < 2_000:
            amount = Money(self.script.choice([2_000, 5_000, 10_000, 20_000]))
            try:
                self.ledger.deposit(self.conn, player, amount)
                self._count("deposit")
            except AccountError as refused:
                assert "daily deposit limit" in str(refused), refused
                if self.calendar is None:
                    player.done = True
                else:
                    self.calendar.next_day()
                    self._count("day")
            return

        spec = self.specs[self.script.choice(sorted(self.specs))]
        buyin = Money(self.script.randint(spec.bet_tiers[0], balance.cents))
        player.game = accounts.start_session(player.session, self.conn, spec.id, buyin)
        player.last = None
        self._count("buyin")

    def _cash_out(self, player: Player, game: GameSession) -> None:
        accounts.cash_out(player.session, self.conn, game)
        player.game = None
        player.last = None
        self._count("cashout")

    def _count(self, what: str) -> None:
        self.counts[what] = self.counts.get(what, 0) + 1
