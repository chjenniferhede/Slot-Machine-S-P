"""Long runs: 100,000 spins of money conservation, and 1,000,000 spins of RTP per game.

Marked slow, so a plain ``pytest`` leaves them out. Run them with
``pytest -m slow``; together they take about five minutes.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections import Counter
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest
from ledger import CLASSIC, DELUXE, Calendar, Ledger, Table, assert_conserved, install_games, new_player

from slotmachine import audit, rng, storage
from slotmachine.game import evaluate
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import base_rtp, load_shipped_specs

pytestmark = pytest.mark.slow

SOAK_SPINS = 100_000
CHECK_EVERY = 1_000
RTP_SPINS = 1_000_000

#: How far an observed figure may be from its expected value, in standard
#: errors. Five is a false alarm about once in two million checks.
Z = 5.0


# ---------------------------------------------------------------------------
# 100,000 spins
# ---------------------------------------------------------------------------


@dataclass
class Soak:
    conn: sqlite3.Connection
    table: Table
    checks: int


@pytest.fixture(scope="module")
def soak(tmp_path_factory: pytest.TempPathFactory) -> Generator[Soak, None, None]:
    """Six players, both games, holds, cards in and out, over many days.

    Totals are checked every 1,000 spins and at the end, after everyone has
    cashed out. A player turned away at the daily deposit limit comes back
    the next day, so the daily totals roll over many times.
    """
    with pytest.MonkeyPatch.context() as mp:
        calendar = Calendar()
        mp.setattr(storage, "today_bucket", calendar.today)

        db: Path = tmp_path_factory.mktemp("soak") / "casino.db"
        conn = storage.connect(db)
        storage.init_db(conn)
        install_games(conn)
        players = [new_player(conn, f"player{i}") for i in range(6)]
        ledger = Ledger()
        table = Table(conn, players, ledger, random.Random(20261117), calendar)

        checks = 0
        while table.spins < SOAK_SPINS:
            table.play(spins=min(CHECK_EVERY, SOAK_SPINS - table.spins))
            assert_conserved(conn, ledger)
            checks = checks + 1
        table.cash_everyone_out()

        yield Soak(conn, table, checks)
        conn.close()


def test_conservation_100k_spins(soak: Soak) -> None:
    """After 100,000 spins, every cent is where the books say, exactly."""
    h = assert_conserved(soak.conn, soak.table.ledger)
    assert h.credits == Money(0)  # everyone cashed out

    counts = soak.table.counts
    assert soak.table.spins == SOAK_SPINS
    assert soak.conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == SOAK_SPINS
    assert soak.checks == SOAK_SPINS // CHECK_EVERY

    # The run did what it set out to: both games, holds, every kind of movement.
    played = dict(soak.conn.execute("SELECT game_id, COUNT(*) FROM spins GROUP BY game_id"))
    assert played.keys() == {CLASSIC, DELUXE}
    assert counts["hold"] > 1_000
    assert counts["buyin"] > 1_000 and counts["cashout"] == counts["buyin"]
    assert counts["deposit"] > 50 and counts["withdraw"] > 50
    assert counts.get("day", 0) > 1, "the daily limits never rolled over"


def test_audit_chain_intact_after_soak(soak: Soak) -> None:
    entries = soak.conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    result = audit.verify_chain(soak.conn)
    assert result.ok, result
    assert result.entries_checked == entries > SOAK_SPINS


# ---------------------------------------------------------------------------
# 1,000,000 spins: the engine, the RNG and the PAR sheet agree
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("game_id", [CLASSIC, DELUXE])
def test_observed_rtp_converges(game_id: str) -> None:
    """Spun through the real RNG and engine, each game returns what rtpcalc says.

    Two checks, each within five standard errors:

    - How often each paytable combination came up, against its chance on
      the PAR sheet. This is the check that finds bugs. With an off-by-one
      planted in the reel grid, the worst combination was 45 to 62 standard
      errors out, while the RTP moved only 3 to 5.
    - The return per unit bet against the PAR sheet's RTP. A single spin
      pays anything from nothing to over 100 bets, so after a million spins
      the standard error is still about 0.6 percentage points and the bound
      about 3. Holding it to 0.5 points would take around 25 million spins.

    Neither can see a bias that leaves the outcomes alone, such as the
    modulo bias test_rng.py checks for: the strips spread each symbol evenly,
    so favouring some positions barely changes what shows.
    """
    spec = {s.id: s for s in load_shipped_specs()}[game_id]
    theoretical, pay_lines = base_rtp(spec)
    lengths = spec.strip_lengths()

    paid = 0
    paid_squared = 0
    hits: Counter[tuple[str, int]] = Counter()
    hits_squared: Counter[tuple[str, int]] = Counter()
    for _ in range(RTP_SPINS):
        stops = rng.derive_stops(rng.secure_seed(), lengths)
        result = evaluate(spec, stops, 1)  # a 1-cent bet pays its multiplier in cents
        paid = paid + result.total_payout_cents
        paid_squared = paid_squared + result.total_payout_cents**2
        if result.wins:
            this_spin = Counter((win.symbol, win.count) for win in result.wins)
            hits.update(this_spin)
            hits_squared.update({key: n * n for key, n in this_spin.items()})

    n = RTP_SPINS
    lines = len(spec.paylines)
    expected_combos = {(row.symbol, row.count) for row in pay_lines}
    assert set(hits) <= expected_combos, f"wins not on the PAR sheet: {set(hits) - expected_combos}"
    for row in pay_lines:
        key = (row.symbol, row.count)
        expected = n * lines * row.chance
        # Wins on different lines of one spin are not independent, so the
        # spread is measured per spin; never taken as less than Poisson's.
        per_spin_mean = hits[key] / n
        variance = n * (hits_squared[key] / n - per_spin_mean**2)
        spread = math.sqrt(max(variance, expected))
        assert abs(hits[key] - expected) <= Z * spread, (
            f"{game_id}: {row.count} x {row.symbol} came up {hits[key]} times,"
            f" expected {expected:.0f} +/- {spread:.0f}"
        )

    observed = paid / n
    standard_error = math.sqrt((paid_squared / n - observed**2) / n)
    assert abs(observed - theoretical) <= Z * standard_error, (
        f"{game_id}: observed RTP {observed:.4%}, PAR sheet {theoretical:.4%},"
        f" standard error {standard_error:.4%}"
    )
