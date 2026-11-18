"""What a gambling commissioner can do: analyse the games and test them.

Every public function here is a commissioner action and carries
``@require_role(Role.COMMISSIONER)``, and nothing else (test-enforced).

A test session plays a game through the same path a player's spin takes: a
fresh seed from ``rng.secure_seed``, stops from ``rng.derive_stops``, the
result from the pure engine. Nothing is simulated differently, so what the
commissioner measures is the real game. The differences are all about money:

- The bankroll is virtual and lives only in this call. No account, wallet or
  game session is touched, and commissioners have none.
- Each round is recorded with ``is_test = 1`` (the schema refuses anything
  else for a commissioner) and is left out of every player statistic and of
  casino accounting.
- The seed of every round is shown. This is the only place seeds are shown.

After the rounds are written, every one is replayed from its stored seed and
must give the same stops and the same payout.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Final

from slotmachine import audit, auth, rng, stats, storage
from slotmachine.audit import Action
from slotmachine.auth import Role
from slotmachine.game.engine import evaluate
from slotmachine.game.spec import GameSpec
from slotmachine.money import Money
from slotmachine.tools import rtpcalc

MAX_ROUNDS: Final[int] = 100_000
MAX_BANKROLL: Final[Money] = Money(100_000_000)  # $1,000,000.00
#: Rounds listed one by one in a report. Larger sessions report a summary
#: for the rest, but every round is still recorded and replayed.
ROUNDS_SHOWN: Final[int] = 1_000


class CommissionerError(ValueError):
    """A commissioner request was refused. The message is safe to show as-is."""


@dataclass(frozen=True)
class GameListing:
    spec: GameSpec
    enabled: bool


@dataclass(frozen=True)
class RoundRecord:
    round: int
    spin_id: int
    seed_hex: str
    stops: tuple[int, ...]
    grid: tuple[tuple[str, ...], ...]
    bet: Money
    payout: Money
    bankroll: Money  # after this round


@dataclass(frozen=True)
class SessionReport:
    game_id: str
    name: str
    rounds_requested: int
    rounds_played: int
    bet: Money
    start_bankroll: Money
    end_bankroll: Money
    total_bet: Money
    total_paid: Money
    hits: int
    largest_win: Money
    theoretical_rtp: float
    optimal_hold_rtp: float | None
    first_spin_id: int | None
    last_spin_id: int | None
    replay_checked: int
    replay_ok: bool
    rounds: tuple[RoundRecord, ...]  # the first ROUNDS_SHOWN

    @property
    def observed_rtp(self) -> float | None:
        return self.total_paid.cents / self.total_bet.cents if self.total_bet.is_positive() else None

    @property
    def hit_rate(self) -> float | None:
        return self.hits / self.rounds_played if self.rounds_played else None

    @property
    def stopped_early(self) -> bool:
        """The bankroll ran out before every round was played."""
        return self.rounds_played < self.rounds_requested


@auth.require_role(Role.COMMISSIONER)
def list_games(session: auth.Session, conn: sqlite3.Connection) -> list[GameListing]:
    """Every installed game. A commissioner may test one before it goes live."""
    return [
        GameListing(spec=stats.spec_of(row), enabled=bool(row["enabled"]))
        for row in storage.list_games(conn, enabled_only=False)
    ]


@auth.require_role(Role.COMMISSIONER)
def analyze_game(
    session: auth.Session, conn: sqlite3.Connection, game_id: str, *, test: bool = False
) -> stats.GameAnalysis:
    """Theory against practice for one game: RTP, hit rate, payout spread.

    ``test`` picks the commissioner's own test rounds instead of live play.
    """
    if not isinstance(game_id, str) or not isinstance(test, bool):
        raise CommissionerError("choose a game")
    analysis = stats.game_analysis(conn, game_id, test=test)
    if analysis is None:
        raise CommissionerError("there is no game with that id")
    return analysis


@auth.require_role(Role.COMMISSIONER)
def par_sheet(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> rtpcalc.ParSheet:
    row = storage.get_game(conn, game_id) if isinstance(game_id, str) else None
    if row is None:
        raise CommissionerError("there is no game with that id")
    return stats.par_sheet(str(row["config_json"]))


@auth.require_role(Role.COMMISSIONER)
def run_test_session(
    session: auth.Session,
    conn: sqlite3.Connection,
    game_id: str,
    rounds: int,
    bet: Money,
    bankroll: Money,
) -> SessionReport:
    """Play ``game_id`` up to ``rounds`` times at ``bet``, from a virtual ``bankroll``.

    Stops early if the bankroll cannot cover the next bet. Every round, its
    start and its end are recorded in one transaction: an interrupted test
    leaves nothing behind. Holds are not used; each round is a fresh spin.
    """
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MAX_ROUNDS:
        raise CommissionerError(f"run between 1 and {MAX_ROUNDS:,} rounds")
    if not isinstance(bet, Money) or not isinstance(bankroll, Money):
        raise CommissionerError("enter a bet and a bankroll")
    row = storage.get_game(conn, game_id) if isinstance(game_id, str) else None
    if row is None:
        raise CommissionerError("there is no game with that id")
    spec = stats.spec_of(row)
    if bet.cents not in spec.bet_tiers:
        tiers = ", ".join(str(Money(tier)) for tier in spec.bet_tiers)
        raise CommissionerError(f"the bets on this game are {tiers}")
    if bankroll < bet:
        raise CommissionerError(f"the bankroll must cover at least one bet of {bet}")
    if bankroll > MAX_BANKROLL:
        raise CommissionerError(f"the bankroll can be at most {MAX_BANKROLL}")

    left = bankroll
    total_bet = Money.zero()
    total_paid = Money.zero()
    largest = Money.zero()
    hits = 0
    played = 0
    first_id: int | None = None
    last_id: int | None = None
    shown: list[RoundRecord] = []
    strips = spec.strip_lengths()

    with storage.transaction(conn):
        audit.write(
            conn,
            session.username,
            Action.TEST_SESSION,
            f"start game={spec.id} rounds={rounds} bet={bet} bankroll={bankroll}",
        )
        while played < rounds and left >= bet:
            seed = rng.secure_seed()
            stops = rng.derive_stops(seed, strips)
            result = evaluate(spec, stops, bet.cents)
            payout = Money(result.total_payout_cents)
            spin_id = storage.record_spin(
                conn,
                session_id=None,
                user_id=session.user_id,
                game_id=spec.id,
                bet=bet,
                seed=seed,
                stops_json=json.dumps(stops),
                payout=payout,
                is_test=True,
            )
            played = played + 1
            left = left - bet + payout
            total_bet = total_bet + bet
            total_paid = total_paid + payout
            if payout.is_positive():
                hits = hits + 1
            if payout > largest:
                largest = payout
            if first_id is None:
                first_id = spin_id
            last_id = spin_id
            if len(shown) < ROUNDS_SHOWN:
                shown.append(
                    RoundRecord(
                        round=played,
                        spin_id=spin_id,
                        seed_hex=seed.hex(),
                        stops=tuple(stops),
                        grid=result.grid,
                        bet=bet,
                        payout=payout,
                        bankroll=left,
                    )
                )
        audit.write(
            conn,
            session.username,
            Action.TEST_SESSION,
            f"end game={spec.id} played={played} spins={first_id}-{last_id}"
            f" bet_total={total_bet} paid={total_paid} bankroll={left}",
        )

    checked, ok = _replay(conn, spec, session.user_id, first_id, last_id)
    base, optimal = stats.theoretical_rtp(str(row["config_json"]))
    return SessionReport(
        game_id=spec.id,
        name=spec.name,
        rounds_requested=rounds,
        rounds_played=played,
        bet=bet,
        start_bankroll=bankroll,
        end_bankroll=left,
        total_bet=total_bet,
        total_paid=total_paid,
        hits=hits,
        largest_win=largest,
        theoretical_rtp=base if base is not None else 0.0,
        optimal_hold_rtp=optimal,
        first_spin_id=first_id,
        last_spin_id=last_id,
        replay_checked=checked,
        replay_ok=ok,
        rounds=tuple(shown),
    )


def _replay(
    conn: sqlite3.Connection, spec: GameSpec, user_id: int, first: int | None, last: int | None
) -> tuple[int, bool]:
    """Recompute every recorded round from its seed. (rounds checked, all matched)."""
    if first is None or last is None:
        return 0, True
    checked = 0
    strips = spec.strip_lengths()
    rows = conn.execute(
        "SELECT seed, stops_json, bet_cents, payout_cents FROM spins"
        " WHERE id BETWEEN ? AND ? AND user_id = ? AND is_test = 1 ORDER BY id",
        (first, last, user_id),
    )
    for row in rows:
        checked = checked + 1
        stops = rng.derive_stops(bytes(row["seed"]), strips)
        if stops != json.loads(row["stops_json"]):
            return checked, False
        if evaluate(spec, stops, int(row["bet_cents"])).total_payout_cents != int(row["payout_cents"]):
            return checked, False
    return checked, checked == last - first + 1
