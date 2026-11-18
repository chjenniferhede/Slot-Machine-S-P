"""Read-only statistics over the spins table, for all three roles.

Nothing here changes the database. Every query binds its values with ``?``.

Live and test spins are kept apart through the ``live_spins`` and
``test_spins`` views in schema.sql, so a commissioner's test session can never
leak into a player's statistics or the casino's books.

Amounts stay in whole cents (``Money``). Floats appear only in ratios such as
a win percentage or an RTP, which are shown to people and never fed back into
a balance.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache

from slotmachine import auth
from slotmachine.game import GameSpec, load_spec
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import base_rtp, hit_frequency, optimal_hold_rtp

#: How many past visits a player's statistics list.
RECENT_SESSIONS = 10


# ---------------------------------------------------------------------------
# A player's own statistics
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
    """One visit to a machine, from buy-in to cash-out, for a player's history."""

    session_id: int
    name: str
    started_at: str
    ended_at: str | None
    spins: int
    staked: Money
    paid: Money

    @property
    def net(self) -> Money:
        return self.paid - self.staked


@dataclass(frozen=True)
class PlayerStats:
    sessions: int
    spins: int
    wins: int
    wagered: Money
    won: Money
    biggest_payout: Money
    favourite_game: str | None
    by_game: tuple[GameLine, ...] = ()
    recent_sessions: tuple[SessionLine, ...] = ()

    @property
    def net(self) -> Money:
        return self.won - self.wagered

    @property
    def total_lost(self) -> Money:
        """Net loss, or $0.00 if the player is ahead."""
        return -self.net if self.net.is_negative() else Money.zero()

    @property
    def win_percent(self) -> float:
        return 100.0 * self.wins / self.spins if self.spins else 0.0


@auth.require_role(auth.Role.PLAYER)
def view_own_statistics(session: auth.Session, conn: sqlite3.Connection) -> PlayerStats:
    """The caller's lifetime statistics, from live spins only."""
    row = conn.execute(
        "SELECT COUNT(*) AS spins,"
        " COALESCE(SUM(CASE WHEN payout_cents > 0 THEN 1 ELSE 0 END), 0) AS wins,"
        " COALESCE(SUM(bet_cents), 0) AS wagered,"
        " COALESCE(SUM(payout_cents), 0) AS won,"
        " COALESCE(MAX(payout_cents), 0) AS biggest"
        " FROM live_spins WHERE user_id = ?",
        (session.user_id,),
    ).fetchone()
    sessions = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (session.user_id,)
    ).fetchone()
    favourite = conn.execute(
        "SELECT g.name AS name FROM live_spins s JOIN games g ON g.id = s.game_id"
        " WHERE s.user_id = ? GROUP BY s.game_id ORDER BY COUNT(*) DESC, g.name LIMIT 1",
        (session.user_id,),
    ).fetchone()
    by_game = conn.execute(
        "SELECT s.game_id, g.name, COUNT(*) AS spins, SUM(s.bet_cents) AS wagered,"
        " SUM(s.payout_cents) AS won"
        " FROM live_spins s JOIN games g ON g.id = s.game_id"
        " WHERE s.user_id = ? GROUP BY s.game_id ORDER BY g.name",
        (session.user_id,),
    ).fetchall()
    history = conn.execute(
        "SELECT se.id, g.name, se.started_at, se.ended_at, COUNT(sp.id) AS spins,"
        " COALESCE(SUM(sp.bet_cents), 0) AS staked, COALESCE(SUM(sp.payout_cents), 0) AS paid"
        " FROM sessions se JOIN games g ON g.id = se.game_id"
        " LEFT JOIN live_spins sp ON sp.session_id = se.id"
        " WHERE se.user_id = ? GROUP BY se.id ORDER BY se.id DESC LIMIT ?",
        (session.user_id, RECENT_SESSIONS),
    ).fetchall()
    return PlayerStats(
        sessions=int(sessions["n"]),
        spins=int(row["spins"]),
        wins=int(row["wins"]),
        wagered=Money(int(row["wagered"])),
        won=Money(int(row["won"])),
        biggest_payout=Money(int(row["biggest"])),
        favourite_game=None if favourite is None else str(favourite["name"]),
        by_game=tuple(
            GameLine(
                game_id=str(g["game_id"]),
                name=str(g["name"]),
                spins=int(g["spins"]),
                wagered=Money(int(g["wagered"])),
                won=Money(int(g["won"])),
            )
            for g in by_game
        ),
        recent_sessions=tuple(
            SessionLine(
                session_id=int(h["id"]),
                name=str(h["name"]),
                started_at=str(h["started_at"]),
                ended_at=None if h["ended_at"] is None else str(h["ended_at"]),
                spins=int(h["spins"]),
                staked=Money(int(h["staked"])),
                paid=Money(int(h["paid"])),
            )
            for h in history
        ),
    )


# ---------------------------------------------------------------------------
# Per-game figures, for technicians and commissioners
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GameFigures:
    """What one game has done, over live or test spins."""

    game_id: str
    name: str
    enabled: bool
    spins: int
    wins: int
    wagered: Money
    paid: Money
    biggest_payout: Money
    players: int
    sessions: int
    first_spin: str | None
    last_spin: str | None
    days_active: int

    @property
    def losses(self) -> int:
        return self.spins - self.wins

    @property
    def hit_rate(self) -> float:
        """Share of spins that paid anything, as a percentage."""
        return 100.0 * self.wins / self.spins if self.spins else 0.0

    @property
    def observed_rtp(self) -> float:
        """Paid out as a percentage of wagered."""
        return 100.0 * self.paid.cents / self.wagered.cents if self.wagered.cents else 0.0

    @property
    def house_net(self) -> Money:
        return self.wagered - self.paid

    @property
    def spins_per_day(self) -> float:
        return self.spins / self.days_active if self.days_active else 0.0


def game_figures(conn: sqlite3.Connection, *, test: bool = False) -> list[GameFigures]:
    """Figures for every installed game, from live spins or (``test``) test spins.

    Private to the guarded actions in cli/technician.py and cli/commissioner.py,
    which decide who may see them.
    """
    view_query = _TEST_FIGURES if test else _LIVE_FIGURES
    figures: list[GameFigures] = []
    for game in conn.execute("SELECT id, name, enabled FROM games ORDER BY name"):
        row = conn.execute(view_query, (game["id"],)).fetchone()
        figures.append(
            GameFigures(
                game_id=str(game["id"]),
                name=str(game["name"]),
                enabled=bool(game["enabled"]),
                spins=int(row["spins"]),
                wins=int(row["wins"]),
                wagered=Money(int(row["wagered"])),
                paid=Money(int(row["paid"])),
                biggest_payout=Money(int(row["biggest"])),
                players=int(row["players"]),
                sessions=int(row["sessions"]),
                first_spin=row["first_spin"],
                last_spin=row["last_spin"],
                days_active=int(row["days"]),
            )
        )
    return figures


_FIGURE_COLUMNS = (
    "SELECT COUNT(*) AS spins,"
    " COALESCE(SUM(CASE WHEN payout_cents > 0 THEN 1 ELSE 0 END), 0) AS wins,"
    " COALESCE(SUM(bet_cents), 0) AS wagered,"
    " COALESCE(SUM(payout_cents), 0) AS paid,"
    " COALESCE(MAX(payout_cents), 0) AS biggest,"
    " COUNT(DISTINCT user_id) AS players,"
    " COUNT(DISTINCT session_id) AS sessions,"
    " MIN(created_at) AS first_spin,"
    " MAX(created_at) AS last_spin,"
    " COUNT(DISTINCT substr(created_at, 1, 10)) AS days"
)
_LIVE_FIGURES = _FIGURE_COLUMNS + " FROM live_spins WHERE game_id = ?"
_TEST_FIGURES = _FIGURE_COLUMNS + " FROM test_spins WHERE game_id = ?"


@dataclass(frozen=True)
class Distribution:
    """How spins paid, in buckets of payout as a multiple of the stake."""

    buckets: tuple[tuple[str, int], ...]
    mean_return: float  # payout / stake, averaged over spins
    std_dev: float  # of payout / stake


#: (label, lower bound inclusive, upper bound exclusive), in hundredths of the stake.
_BUCKETS = (
    ("0x (loss)", 0, 1),
    ("under 1x", 1, 100),
    ("1x-2x", 100, 200),
    ("2x-5x", 200, 500),
    ("5x-20x", 500, 2000),
    ("20x-100x", 2000, 10000),
    ("100x+", 10000, None),
)


_LIVE_PAYOUTS = "SELECT bet_cents, payout_cents FROM live_spins WHERE game_id = ?"
_TEST_PAYOUTS = "SELECT bet_cents, payout_cents FROM test_spins WHERE game_id = ?"


def payout_distribution(conn: sqlite3.Connection, game_id: str, *, test: bool = False) -> Distribution:
    """Payout multiples for one game's live (or test) spins."""
    query = _TEST_PAYOUTS if test else _LIVE_PAYOUTS
    counts = [0] * len(_BUCKETS)
    n = 0
    total = 0.0
    total_sq = 0.0
    for row in conn.execute(query, (game_id,)):
        bet = int(row["bet_cents"])
        payout = int(row["payout_cents"])
        hundredths = payout * 100 // bet
        for index, (_, low, high) in enumerate(_BUCKETS):
            if hundredths >= low and (high is None or hundredths < high):
                counts[index] = counts[index] + 1
                break
        ratio = payout / bet
        n = n + 1
        total = total + ratio
        total_sq = total_sq + ratio * ratio
    mean = total / n if n else 0.0
    variance = max(total_sq / n - mean * mean, 0.0) if n else 0.0
    return Distribution(
        buckets=tuple((label, count) for (label, _, _), count in zip(_BUCKETS, counts)),
        mean_return=mean,
        std_dev=variance**0.5,
    )


# ---------------------------------------------------------------------------
# Theoretical figures from the game's own maths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Theory:
    base_rtp: float  # percentage
    hold_rtp: float | None  # percentage under perfect hold play, or None
    hit_rate: float  # percentage
    hit_rate_estimated: bool


def theoretical(spec: GameSpec) -> Theory:
    """Exact RTP and hit rate for a game, from tools/rtpcalc. Cached per config."""
    return _theoretical_cached(dump_spec(spec))


@lru_cache(maxsize=32)
def _theoretical_cached(config_json: str) -> Theory:
    spec = load_spec(config_json)
    rtp, _ = base_rtp(spec)
    hold = optimal_hold_rtp(spec)
    hits, estimated = hit_frequency(spec, samples=50_000)
    return Theory(
        base_rtp=100.0 * rtp,
        hold_rtp=None if hold is None else 100.0 * hold,
        hit_rate=100.0 * hits,
        hit_rate_estimated=estimated,
    )
