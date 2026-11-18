"""The Big Gamble Commission interface: analysis and testing only.

Every public function here carries ``@auth.require_role(Role.COMMISSIONER)``,
and nothing else; tests/test_source_invariants.py enforces it.

A commissioner never touches real money. Test rounds are recorded with
``is_test = 1`` and no game session, and a trigger in schema.sql refuses a
test spin from anyone but a commissioner, so test play cannot reach a player's
balance or the casino's books. Seeds are shown here and nowhere else.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slotmachine import audit, auth, rng, stats, storage
from slotmachine.audit import Action
from slotmachine.cli.common import RequestError, change_password, heading, percent
from slotmachine.cli.menus import MenuItem, menu, table
from slotmachine.cli.prompts import info, prompt_int, prompt_money, prompt_yes_no, success
from slotmachine.game import GameSpec, evaluate, load_spec
from slotmachine.game.engine import render_grid
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import par_sheet, render

Role = auth.Role

MAX_TEST_ROUNDS: Final[int] = 100_000
MAX_BANKROLL: Final[Money] = Money(100_000_000)  # $1,000,000.00
ROUNDS_SHOWN: Final[int] = 20
REPORT_DIR: Final[str] = "reports"


class TestSessionError(RequestError):
    """A test or replay request was refused. The message is safe to show."""

    __test__ = False  # not a pytest test class, despite the name


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameAnalysis:
    figures: stats.GameFigures
    theory: stats.Theory
    distribution: stats.Distribution


@auth.require_role(Role.COMMISSIONER)
def list_games(session: auth.Session, conn: sqlite3.Connection) -> list[tuple[GameSpec, bool]]:
    """Every installed game and whether it is playable. Disabled games can be tested too."""
    return [(load_spec(row["config_json"]), bool(row["enabled"])) for row in storage.list_games(conn, enabled_only=False)]


@auth.require_role(Role.COMMISSIONER)
def view_game_statistics(
    session: auth.Session, conn: sqlite3.Connection, include_tests: bool = False
) -> list[GameAnalysis]:
    """Each game's wins, losses, payout frequency and return, against its maths.

    Live play by default; with ``include_tests``, the commission's own test
    rounds instead.
    """
    analyses = []
    for figures in stats.game_figures(conn, test=include_tests):
        spec = _spec(conn, figures.game_id)
        analyses.append(
            GameAnalysis(
                figures=figures,
                theory=stats.theoretical(spec),
                distribution=stats.payout_distribution(conn, figures.game_id, test=include_tests),
            )
        )
    return analyses


@auth.require_role(Role.COMMISSIONER)
def view_par_sheet(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> str:
    """The game's PAR sheet: reel strips, paytable, exact RTP and hit frequency."""
    return render(par_sheet(_spec(conn, game_id), samples=50_000))


# ---------------------------------------------------------------------------
# Test mode
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestRound:
    __test__ = False

    number: int
    spin_id: int
    seed_hex: str
    stops: tuple[int, ...]
    payout: Money


@dataclass(frozen=True)
class TestReport:
    __test__ = False

    spec: GameSpec
    bet: Money
    rounds: tuple[TestRound, ...]
    rounds_requested: int = 0
    start_bankroll: Money | None = None  # None: no bankroll, every round is played
    replay_checked: int = 0
    replay_ok: bool = False

    @property
    def end_bankroll(self) -> Money | None:
        if self.start_bankroll is None:
            return None
        return self.start_bankroll - self.wagered + self.paid

    @property
    def stopped_early(self) -> bool:
        """The bankroll ran out before every requested round was played."""
        return len(self.rounds) < self.rounds_requested

    @property
    def wagered(self) -> Money:
        return self.bet.times(len(self.rounds))

    @property
    def paid(self) -> Money:
        total = Money.zero()
        for r in self.rounds:
            total = total + r.payout
        return total

    @property
    def wins(self) -> int:
        return sum(1 for r in self.rounds if r.payout.is_positive())

    @property
    def rtp(self) -> float:
        return 100.0 * self.paid.cents / self.wagered.cents if self.rounds else 0.0

    @property
    def hit_rate(self) -> float:
        return 100.0 * self.wins / len(self.rounds) if self.rounds else 0.0

    @property
    def biggest(self) -> Money:
        return max((r.payout for r in self.rounds), default=Money.zero())


@auth.require_role(Role.COMMISSIONER)
def run_test_session(
    session: auth.Session,
    conn: sqlite3.Connection,
    game_id: str,
    bet: Money,
    rounds: int,
    *,
    bankroll: Money | None = None,
) -> TestReport:
    """Play ``game_id`` ``rounds`` times at ``bet`` with virtual money, recording every seed.

    Each round goes through the same RNG and engine as live play: a fresh
    32-byte seed from rng.secure_seed(), reel stops derived from it, and the
    same evaluate(). The rounds are stored as test spins, all in one
    transaction with one audit entry, so an interrupted test leaves nothing.

    With ``bankroll``, the session also tracks a virtual balance and stops
    when it cannot cover the next bet. It exists only in this call: no
    account, wallet or game session is touched.

    Afterwards every round is replayed from its stored seed; the report says
    whether all of them reproduce exactly.
    """
    if isinstance(rounds, bool) or not isinstance(rounds, int) or not 1 <= rounds <= MAX_TEST_ROUNDS:
        raise TestSessionError(f"choose between 1 and {MAX_TEST_ROUNDS:,} rounds")
    spec = _spec(conn, game_id)
    if not isinstance(bet, Money) or bet.cents not in spec.bet_tiers:
        tiers = ", ".join(str(Money(t)) for t in spec.bet_tiers)
        raise TestSessionError(f"the bets on this game are {tiers}")
    if bankroll is not None:
        if not isinstance(bankroll, Money) or bankroll < bet:
            raise TestSessionError(f"the bankroll must cover at least one bet of {bet}")
        if bankroll > MAX_BANKROLL:
            raise TestSessionError(f"the bankroll can be at most {MAX_BANKROLL}")

    played: list[TestRound] = []
    strip_lengths = spec.strip_lengths()
    left = bankroll
    with storage.transaction(conn):
        for number in range(1, rounds + 1):
            if left is not None and left < bet:
                break
            seed = rng.secure_seed()
            stops = rng.derive_stops(seed, strip_lengths)
            payout = Money(evaluate(spec, stops, bet.cents).total_payout_cents)
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
            played.append(TestRound(number, spin_id, seed.hex(), tuple(stops), payout))
            if left is not None:
                left = left - bet + payout
        report = TestReport(spec=spec, bet=bet, rounds=tuple(played), rounds_requested=rounds, start_bankroll=bankroll)
        bankroll_note = "" if bankroll is None else f" bankroll={bankroll} left={report.end_bankroll}"
        audit.write(
            conn,
            session.username,
            Action.TEST_SESSION,
            f"game={spec.id} bet={bet} rounds={len(played)}/{rounds} spins={played[0].spin_id}-{played[-1].spin_id}"
            f" wagered={report.wagered} paid={report.paid}{bankroll_note}",
        )

    checked, ok = _replay_rounds(conn, spec, session.user_id, played[0].spin_id, played[-1].spin_id)
    return TestReport(
        spec=spec,
        bet=bet,
        rounds=tuple(played),
        rounds_requested=rounds,
        start_bankroll=bankroll,
        replay_checked=checked,
        replay_ok=ok,
    )


def _replay_rounds(
    conn: sqlite3.Connection, spec: GameSpec, user_id: int, first: int, last: int
) -> tuple[int, bool]:
    """Recompute every round of a test session from its stored seed: (rounds checked, all matched)."""
    checked = 0
    strips = spec.strip_lengths()
    rows = conn.execute(
        "SELECT seed, stops_json, bet_cents, payout_cents FROM test_spins"
        " WHERE id BETWEEN ? AND ? AND user_id = ? ORDER BY id",
        (first, last, user_id),
    )
    for row in rows:
        checked = checked + 1
        stops = rng.derive_stops(bytes(row["seed"]), strips)
        if stops != [int(s) for s in json.loads(row["stops_json"])]:
            return checked, False
        if evaluate(spec, stops, int(row["bet_cents"])).total_payout_cents != int(row["payout_cents"]):
            return checked, False
    return checked, checked == last - first + 1


@auth.require_role(Role.COMMISSIONER)
def export_test_report(session: auth.Session, conn: sqlite3.Connection, report: TestReport) -> Path:
    """Write every round of a test session, seeds included, to a CSV file under reports/."""
    folder = Path(REPORT_DIR)
    folder.mkdir(exist_ok=True)
    path = folder / f"test_{report.spec.id}_spins_{report.rounds[0].spin_id}-{report.rounds[-1].spin_id}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["round", "spin_id", "seed_hex", "stops", "bet", "payout"])
        for r in report.rounds:
            writer.writerow([r.number, r.spin_id, r.seed_hex, " ".join(map(str, r.stops)), str(report.bet), str(r.payout)])
    return path


# ---------------------------------------------------------------------------
# Replaying a spin from its seed
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Replay:
    spin_id: int
    game: GameSpec
    is_test: bool
    created_at: str
    seed_hex: str
    recorded_stops: tuple[int, ...]
    derived_stops: tuple[int, ...]
    held_reels: tuple[int, ...]  # reels carried over from the spin before
    recorded_payout: Money
    recomputed_payout: Money
    stops_match: bool

    @property
    def verified(self) -> bool:
        return self.stops_match and self.recorded_payout == self.recomputed_payout


@auth.require_role(Role.COMMISSIONER)
def view_spin_seeds(session: auth.Session, conn: sqlite3.Connection, spin_id: int) -> Replay:
    """Re-derive any recorded spin, live or test, from its stored seed.

    The seed alone fixes every reel position, and the engine is pure, so
    re-running both must reproduce exactly what was recorded and paid. A
    mismatch means the spin was not produced by the certified RNG and engine.
    """
    row = conn.execute("SELECT * FROM spins WHERE id = ?", (spin_id,)).fetchone()
    if row is None:
        raise TestSessionError("there is no spin with that number")
    spec = _spec(conn, str(row["game_id"]))
    seed = bytes(row["seed"])
    recorded = [int(s) for s in json.loads(row["stops_json"])]
    derived = rng.derive_stops(seed, spec.strip_lengths())

    held: list[int] = []
    stops_match = True
    previous: list[int] | None = None
    if row["is_hold_respin"]:
        before = conn.execute(
            "SELECT stops_json FROM spins WHERE session_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
            (row["session_id"], spin_id),
        ).fetchone()
        previous = None if before is None else [int(s) for s in json.loads(before["stops_json"])]
    for reel, (got, expected) in enumerate(zip(recorded, derived)):
        if got == expected:
            continue
        if previous is not None and previous[reel] == got:
            held.append(reel)
        else:
            stops_match = False

    charged = int(row["bet_cents"])
    base_bet = charged * 100 // spec.holds.cost_percent if row["is_hold_respin"] else charged
    payout = Money(evaluate(spec, recorded, base_bet).total_payout_cents)

    with storage.transaction(conn):
        audit.write(conn, session.username, Action.SPIN_REPLAYED, f"spin={spin_id}")

    return Replay(
        spin_id=spin_id,
        game=spec,
        is_test=bool(row["is_test"]),
        created_at=str(row["created_at"]),
        seed_hex=seed.hex(),
        recorded_stops=tuple(recorded),
        derived_stops=tuple(derived),
        held_reels=tuple(held),
        recorded_payout=Money(int(row["payout_cents"])),
        recomputed_payout=payout,
        stops_match=stops_match,
    )


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


@auth.require_role(Role.COMMISSIONER)
def commissioner_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    """The commissioner's main menu."""
    info("Testing-only mode: nothing here touches real money.")
    menu(
        "BIG GAMBLE COMMISSION — TESTING MODE",
        [
            MenuItem("Analyse game statistics (live play)", lambda: _show_statistics(session, conn, False)),
            MenuItem("Test a game for X rounds", lambda: _test_game(session, conn)),
            MenuItem("Analyse statistics of all test rounds", lambda: _show_statistics(session, conn, True)),
            MenuItem("Replay and verify a spin from its seed", lambda: _replay(session, conn)),
            MenuItem("View a game's PAR sheet", lambda: _par(session, conn)),
            MenuItem("Change my password", lambda: change_password(conn, session)),
        ],
        session,
        back_label="Log out",
    )


def _spec(conn: sqlite3.Connection, game_id: str) -> GameSpec:
    row = storage.get_game(conn, game_id)
    if row is None:
        raise TestSessionError("there is no game with that id")
    return load_spec(row["config_json"])


def _choose_game(conn: sqlite3.Connection) -> GameSpec:
    games = storage.list_games(conn, enabled_only=False)
    print(table(
        ["#", "Game", "Id", "Status"],
        [[i, g["name"], g["id"], "playable" if g["enabled"] else "disabled"] for i, g in enumerate(games, start=1)],
    ))
    choice = prompt_int("Game #", minimum=1, maximum=len(games))
    return load_spec(games[choice - 1]["config_json"])


def _show_statistics(session: auth.Session, conn: sqlite3.Connection, tests: bool) -> None:
    analyses = view_game_statistics(session, conn, tests)
    source = "commission test rounds" if tests else "live play"
    heading(f"Game statistics — {source}")
    print(table(
        ["Game", "Spins", "Wins", "Losses", "Payout freq", "Theory freq", "RTP", "RTP theory", "RTP best holds"],
        [
            [a.figures.name, f"{a.figures.spins:,}", f"{a.figures.wins:,}", f"{a.figures.losses:,}",
             percent(a.figures.hit_rate), percent(a.theory.hit_rate), percent(a.figures.observed_rtp),
             percent(a.theory.base_rtp), "-" if a.theory.hold_rtp is None else percent(a.theory.hold_rtp)]
            for a in analyses
        ],
    ))
    print()
    print(table(
        ["Game", "Wagered", "Paid out", "House net", "Biggest win", "Mean return", "Std dev", "Players"],
        [
            [a.figures.name, str(a.figures.wagered), str(a.figures.paid), str(a.figures.house_net),
             str(a.figures.biggest_payout), f"{a.distribution.mean_return:.3f}x", f"{a.distribution.std_dev:.3f}x",
             f"{a.figures.players:,}"]
            for a in analyses
        ],
    ))
    for a in analyses:
        if not a.figures.spins:
            continue
        heading(f"Payout distribution — {a.figures.name}")
        print(table(
            ["Payout (x stake)", "Spins", "Share"],
            [[label, f"{n:,}", percent(100.0 * n / a.figures.spins)] for label, n in a.distribution.buckets],
        ))


def _test_game(session: auth.Session, conn: sqlite3.Connection) -> None:
    spec = _choose_game(conn)
    tiers = [Money(t) for t in spec.bet_tiers]
    print(table(["#", "Bet"], [[i, str(t)] for i, t in enumerate(tiers, start=1)]))
    bet = tiers[prompt_int("Bet #", minimum=1, maximum=len(tiers)) - 1]
    rounds = prompt_int("Number of rounds (X)", minimum=1, maximum=MAX_TEST_ROUNDS)
    bankroll: Money | None = None
    if prompt_yes_no("Stop early if a virtual bankroll runs out?", default=False):
        bankroll = prompt_money("Virtual bankroll", minimum=bet, maximum=MAX_BANKROLL)
    info(f"Playing {spec.name} {rounds:,} times at {bet} with virtual money...")
    report = run_test_session(session, conn, spec.id, bet, rounds, bankroll=bankroll)
    theory = stats.theoretical(spec)

    shown = report.rounds[:ROUNDS_SHOWN]
    heading(f"Rounds (first {len(shown)} of {len(report.rounds):,})")
    print(table(
        ["Round", "Spin id", "Seed (hex)", "Stops", "Payout"],
        [[r.number, r.spin_id, r.seed_hex, " ".join(map(str, r.stops)), str(r.payout)] for r in shown],
        align=">><<>",
    ))

    heading("Test session summary")
    print(table(
        ["Measure", "Value"],
        [
            ["Game", f"{spec.name} ({spec.id})"],
            ["Rounds", f"{len(report.rounds):,} of {report.rounds_requested:,}"],
            ["Bet per round", str(report.bet)],
            ["Spin ids", f"{report.rounds[0].spin_id} to {report.rounds[-1].spin_id}"],
            ["Total wagered (virtual)", str(report.wagered)],
            ["Total paid (virtual)", str(report.paid)],
            ["Wins / losses", f"{report.wins:,} / {len(report.rounds) - report.wins:,}"],
            ["Payout frequency", f"{percent(report.hit_rate)} (theory {percent(theory.hit_rate)})"],
            ["Return to player", f"{percent(report.rtp)} (theory {percent(theory.base_rtp)})"],
            ["Biggest win", str(report.biggest)],
            *(
                []
                if report.start_bankroll is None
                else [["Virtual bankroll", f"{report.start_bankroll} -> {report.end_bankroll}"]]
            ),
        ],
        align="<<",
    ))
    if report.stopped_early:
        info(f"The bankroll ran out after {len(report.rounds):,} of {report.rounds_requested:,} rounds.")
    if report.replay_ok:
        success(f"Replay verified: all {report.replay_checked:,} rounds reproduce from their seeds.")
    else:
        info("✗ REPLAY MISMATCH: a recorded round does not reproduce from its seed.")
    if prompt_yes_no("Save every round, with its seed, to a CSV file?", default=True):
        path = export_test_report(session, conn, report)
        success(f"Saved {path}")


def _replay(session: auth.Session, conn: sqlite3.Connection) -> None:
    spin_id = prompt_int("Spin id", minimum=1)
    replay = view_spin_seeds(session, conn, spin_id)
    heading(f"Spin {replay.spin_id} — {replay.game.name} ({'test' if replay.is_test else 'live'}, {replay.created_at})")
    print("\n".join("  " + line for line in render_grid(replay.game, list(replay.recorded_stops)).splitlines()))
    rows = [
        ["Seed", replay.seed_hex],
        ["Stops recorded", " ".join(map(str, replay.recorded_stops))],
        ["Stops derived from seed", " ".join(map(str, replay.derived_stops))],
        ["Payout recorded", str(replay.recorded_payout)],
        ["Payout recomputed", str(replay.recomputed_payout)],
    ]
    if replay.held_reels:
        rows.append(["Held from previous spin", ", ".join(f"reel {r + 1}" for r in replay.held_reels)])
    print(table(["", ""], rows, align="<<"))
    if replay.verified:
        success("Verified: the seed reproduces this spin and its payout exactly.")
    else:
        info("✗ MISMATCH: this spin was not produced by the certified RNG and engine.")


def _par(session: auth.Session, conn: sqlite3.Connection) -> None:
    spec = _choose_game(conn)
    print(view_par_sheet(session, conn, spec.id))


__all__ = [
    "TestSessionError",
    "commissioner_menu",
    "export_test_report",
    "list_games",
    "run_test_session",
    "view_game_statistics",
    "view_par_sheet",
    "view_spin_seeds",
]
