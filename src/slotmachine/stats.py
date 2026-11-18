"""Read-only statistics over recorded spins, for every role's reports.

Nothing here checks who is asking: the role-gated entry points in
``accounts``, ``technician`` and ``commissioner`` do that, then call these.
Nothing here writes, either, so a report can never move money.

Live and test spins are never mixed. Every query takes ``test`` and binds it
against ``spins.is_test``: player statistics and casino accounting read live
spins only, and a commissioner's test rounds are reported on their own.

Amounts are Money. Ratios such as return to player are floats, which is fine
for a percentage on a report and is why this module is not a money path.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from typing import Final

from slotmachine.game.spec import GameSpec, SpecError, load_spec
from slotmachine.money import Money
from slotmachine.tools import rtpcalc

#: Samples for the multi-line hit frequency on a PAR sheet shown on screen.
#: The command-line tool uses more; this keeps a report quick to open.
PAR_SHEET_SAMPLES: Final[int] = 50_000

#: Payout buckets, as a multiple of what the spin cost. Upper bounds are
#: exclusive; the last bucket has none.
HISTOGRAM_BUCKETS: Final[tuple[str, ...]] = (
    "0x",
    "under 1x",
    "1x to 2x",
    "2x to 5x",
    "5x to 10x",
    "10x to 50x",
    "50x and up",
)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameLine:
    """One game's totals inside a player's statistics."""

    game_id: str
    name: str
    spins: int
    wagered: Money
    won: Money


@dataclass(frozen=True)
class SessionLine:
    """One visit to a machine, for a player's history."""

    session_id: int
    game_id: str
    name: str
    started_at: str
    ended_at: str | None
    spins: int
    staked: Money
    paid: Money


@dataclass(frozen=True)
class PlayerSummary:
    sessions: int
    spins: int
    wins: int
    wagered: Money
    won: Money
    biggest_payout: Money
    by_game: tuple[GameLine, ...]
    recent_sessions: tuple[SessionLine, ...]

    @property
    def net(self) -> Money:
        """Won minus wagered: negative means the player is down."""
        return self.won - self.wagered

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.spins if self.spins else None


@dataclass(frozen=True)
class GameStats:
    """One game's totals, for the technician diagnostic and commissioner analysis."""

    game_id: str
    name: str
    enabled: bool
    spins: int
    wins: int
    wagered: Money
    paid: Money
    largest_win: Money
    unique_players: int
    active_days: int
    theoretical_rtp: float | None
    optimal_hold_rtp: float | None

    @property
    def losses(self) -> int:
        return self.spins - self.wins

    @property
    def house_net(self) -> Money:
        """What the casino kept: wagered minus paid."""
        return self.wagered - self.paid

    @property
    def observed_rtp(self) -> float | None:
        return self.paid.cents / self.wagered.cents if self.wagered.is_positive() else None

    @property
    def hit_rate(self) -> float | None:
        return self.wins / self.spins if self.spins else None

    @property
    def plays_per_day(self) -> float | None:
        return self.spins / self.active_days if self.active_days else None


@dataclass(frozen=True)
class GameAnalysis:
    stats: GameStats
    histogram: tuple[tuple[str, int], ...]
    return_stdev: float | None  # spread of payout / cost per spin


# ---------------------------------------------------------------------------
# Theory, worked out once per game config
# ---------------------------------------------------------------------------


@lru_cache(maxsize=64)
def theoretical_rtp(config_json: str) -> tuple[float | None, float | None]:
    """Base and perfect-hold return for a stored config; (None, None) if unreadable."""
    try:
        spec = load_spec(config_json)
    except SpecError:
        return None, None
    return spec_rtp(spec)


@lru_cache(maxsize=64)
def spec_rtp(spec: GameSpec) -> tuple[float, float | None]:
    """Base and perfect-hold return for a loaded game. GameSpec is frozen, so it caches."""
    base, _ = rtpcalc.base_rtp(spec)
    return base, rtpcalc.optimal_hold_rtp(spec)


@lru_cache(maxsize=16)
def par_sheet(config_json: str) -> rtpcalc.ParSheet:
    return rtpcalc.par_sheet(load_spec(config_json), PAR_SHEET_SAMPLES)


def spec_of(row: sqlite3.Row) -> GameSpec:
    return load_spec(str(row["config_json"]))


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def player_summary(conn: sqlite3.Connection, user_id: int, recent: int = 10) -> PlayerSummary:
    """One player's live play. Test spins never belong to a player, but are excluded anyway."""
    totals = conn.execute(
        "SELECT COUNT(*) AS spins, COALESCE(SUM(payout_cents > 0), 0) AS wins,"
        " COALESCE(SUM(bet_cents), 0) AS wagered, COALESCE(SUM(payout_cents), 0) AS won,"
        " COALESCE(MAX(payout_cents), 0) AS biggest"
        " FROM spins WHERE user_id = ? AND is_test = 0",
        (user_id,),
    ).fetchone()
    sessions = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    by_game = conn.execute(
        "SELECT s.game_id, g.name, COUNT(*) AS spins, SUM(s.bet_cents) AS wagered,"
        " SUM(s.payout_cents) AS won"
        " FROM spins s JOIN games g ON g.id = s.game_id"
        " WHERE s.user_id = ? AND s.is_test = 0"
        " GROUP BY s.game_id ORDER BY g.name",
        (user_id,),
    ).fetchall()
    history = conn.execute(
        "SELECT se.id, se.game_id, g.name, se.started_at, se.ended_at,"
        " COUNT(sp.id) AS spins, COALESCE(SUM(sp.bet_cents), 0) AS staked,"
        " COALESCE(SUM(sp.payout_cents), 0) AS paid"
        " FROM sessions se JOIN games g ON g.id = se.game_id"
        " LEFT JOIN spins sp ON sp.session_id = se.id AND sp.is_test = 0"
        " WHERE se.user_id = ?"
        " GROUP BY se.id ORDER BY se.id DESC LIMIT ?",
        (user_id, recent),
    ).fetchall()
    return PlayerSummary(
        sessions=int(sessions["n"]),
        spins=int(totals["spins"]),
        wins=int(totals["wins"]),
        wagered=Money(int(totals["wagered"])),
        won=Money(int(totals["won"])),
        biggest_payout=Money(int(totals["biggest"])),
        by_game=tuple(
            GameLine(
                game_id=str(row["game_id"]),
                name=str(row["name"]),
                spins=int(row["spins"]),
                wagered=Money(int(row["wagered"])),
                won=Money(int(row["won"])),
            )
            for row in by_game
        ),
        recent_sessions=tuple(
            SessionLine(
                session_id=int(row["id"]),
                game_id=str(row["game_id"]),
                name=str(row["name"]),
                started_at=str(row["started_at"]),
                ended_at=None if row["ended_at"] is None else str(row["ended_at"]),
                spins=int(row["spins"]),
                staked=Money(int(row["staked"])),
                paid=Money(int(row["paid"])),
            )
            for row in history
        ),
    )


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------

_GAME_TOTALS: Final[str] = (
    "SELECT g.id, g.name, g.enabled, g.config_json,"
    " COUNT(s.id) AS spins, COALESCE(SUM(s.payout_cents > 0), 0) AS wins,"
    " COALESCE(SUM(s.bet_cents), 0) AS wagered, COALESCE(SUM(s.payout_cents), 0) AS paid,"
    " COALESCE(MAX(s.payout_cents), 0) AS largest,"
    " COUNT(DISTINCT s.user_id) AS players,"
    " COUNT(DISTINCT substr(s.created_at, 1, 10)) AS days"
    " FROM games g LEFT JOIN spins s ON s.game_id = g.id AND s.is_test = ?"
)


def game_stats(conn: sqlite3.Connection, *, test: bool = False) -> list[GameStats]:
    """Every installed game, enabled or not, with its live (or test) totals."""
    rows = conn.execute(
        _GAME_TOTALS + " GROUP BY g.id ORDER BY g.name", (int(test),)
    ).fetchall()
    return [_to_stats(row) for row in rows]


def game_analysis(conn: sqlite3.Connection, game_id: str, *, test: bool = False) -> GameAnalysis | None:
    """One game in depth: totals, payout histogram and spread. None if no such game."""
    row = conn.execute(
        _GAME_TOTALS + " WHERE g.id = ? GROUP BY g.id", (int(test), game_id)
    ).fetchone()
    if row is None:
        return None

    # Integer comparisons only: payout against multiples of what the spin cost.
    buckets = conn.execute(
        "SELECT"
        " COALESCE(SUM(payout_cents = 0), 0) AS b0,"
        " COALESCE(SUM(payout_cents > 0 AND payout_cents < bet_cents), 0) AS b1,"
        " COALESCE(SUM(payout_cents >= bet_cents AND payout_cents < 2 * bet_cents), 0) AS b2,"
        " COALESCE(SUM(payout_cents >= 2 * bet_cents AND payout_cents < 5 * bet_cents), 0) AS b3,"
        " COALESCE(SUM(payout_cents >= 5 * bet_cents AND payout_cents < 10 * bet_cents), 0) AS b4,"
        " COALESCE(SUM(payout_cents >= 10 * bet_cents AND payout_cents < 50 * bet_cents), 0) AS b5,"
        " COALESCE(SUM(payout_cents >= 50 * bet_cents), 0) AS b6,"
        " AVG(CAST(payout_cents AS REAL) / bet_cents) AS mean_ratio,"
        " AVG((CAST(payout_cents AS REAL) / bet_cents) * (CAST(payout_cents AS REAL) / bet_cents))"
        " AS mean_square"
        " FROM spins WHERE game_id = ? AND is_test = ?",
        (game_id, int(test)),
    ).fetchone()
    counts = [int(buckets[f"b{i}"]) for i in range(len(HISTOGRAM_BUCKETS))]

    stdev: float | None = None
    if buckets["mean_ratio"] is not None:
        mean = float(buckets["mean_ratio"])
        variance = max(float(buckets["mean_square"]) - mean * mean, 0.0)
        stdev = math.sqrt(variance)

    return GameAnalysis(
        stats=_to_stats(row),
        histogram=tuple(zip(HISTOGRAM_BUCKETS, counts)),
        return_stdev=stdev,
    )


def _to_stats(row: sqlite3.Row) -> GameStats:
    base, optimal = theoretical_rtp(str(row["config_json"]))
    return GameStats(
        game_id=str(row["id"]),
        name=str(row["name"]),
        enabled=bool(row["enabled"]),
        spins=int(row["spins"]),
        wins=int(row["wins"]),
        wagered=Money(int(row["wagered"])),
        paid=Money(int(row["paid"])),
        largest_win=Money(int(row["largest"])),
        unique_players=int(row["players"]),
        active_days=int(row["days"]),
        theoretical_rtp=base,
        optimal_hold_rtp=optimal,
    )
