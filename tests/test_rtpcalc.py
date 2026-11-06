"""The payout maths: does the casino actually make money."""

from __future__ import annotations

import json

import pytest

from slotmachine import rng
from slotmachine.game.engine import evaluate
from slotmachine.game.spec import load_spec
from slotmachine.tools.rtpcalc import (
    _hold_choices,
    base_rtp,
    hit_frequency,
    load_shipped_specs,
    optimal_hold_rtp,
    par_sheet,
    render,
    symbol_chances,
)

# Small enough to work out by hand. Each reel is half A and half B, so every
# symbol has a one in two chance.
#
#   2 x A pays 2x, chance 0.5 * 0.5 = 0.25, so it returns 0.50 per bet
#   2 x B pays 1x, chance 0.5 * 0.5 = 0.25, so it returns 0.25 per bet
#                                              total RTP = 0.75
#   something pays on half the spins, so hit frequency = 0.50
TOY = load_spec(
    json.dumps(
        {
            "id": "toy",
            "name": "Toy",
            "symbols": ["A", "B"],
            "reels": [["A", "A", "B", "B"], ["A", "A", "B", "B"]],
            "rows": 1,
            "paylines": [[0, 0]],
            "paytable": [
                {"symbol": "A", "count": 2, "multiplier": 2},
                {"symbol": "B", "count": 2, "multiplier": 1},
            ],
            "bet_tiers": [100],
            "holds": {
                "enabled": False,
                "max_consecutive": 0,
                "max_reels_held": 0,
                "cost_percent": 100,
            },
        }
    )
)


def test_toy_rtp_matches_hand_calculation() -> None:
    """Proves the calculator itself, against a game simple enough to check."""
    rtp, _ = base_rtp(TOY)
    assert rtp == pytest.approx(0.75)


def test_toy_hit_frequency_matches_hand_calculation() -> None:
    hits, estimated = hit_frequency(TOY)
    assert hits == pytest.approx(0.50)
    assert estimated is False


def test_symbol_chances_sum_to_one() -> None:
    for spec in load_shipped_specs():
        for reel in symbol_chances(spec):
            assert sum(reel.values()) == pytest.approx(1.0)


def test_single_payline_hit_frequency_is_exact() -> None:
    for spec in load_shipped_specs():
        _, estimated = hit_frequency(spec, samples=1000)
        assert estimated is (len(spec.paylines) > 1)


@pytest.mark.parametrize("spec", load_shipped_specs(), ids=lambda s: s.id)
def test_shipped_game_is_profitable(spec) -> None:
    """Whichever way it is played, the house must keep something."""
    base, _ = base_rtp(spec)
    assert 0 < base < 1.0

    best_play = optimal_hold_rtp(spec)
    if best_play is not None:
        assert best_play < 1.0, "a perfect player would beat this game"


@pytest.mark.parametrize("spec", load_shipped_specs(), ids=lambda s: s.id)
def test_shipped_game_returns_a_sensible_share(spec) -> None:
    """The figure a player actually experiences should be 90-96%."""
    base, _ = base_rtp(spec)
    headline = optimal_hold_rtp(spec)
    if headline is None:
        headline = base
    assert 0.90 <= headline <= 0.96


def test_holds_are_worth_having() -> None:
    """If perfect holds did not beat never holding, the maths would be wrong."""
    classic = load_shipped_specs()[0]
    base, _ = base_rtp(classic)
    assert optimal_hold_rtp(classic) > base


def test_no_hold_value_for_a_game_without_holds() -> None:
    deluxe = load_shipped_specs()[1]
    assert optimal_hold_rtp(deluxe) is None


def test_hold_choices_respect_the_limit() -> None:
    assert _hold_choices(3, 1) == [(), (0,), (1,), (2,)]
    assert (0, 1) in _hold_choices(3, 2)
    # Every reel held would mean nothing to re-spin.
    assert (0, 1, 2) not in _hold_choices(3, 3)


@pytest.mark.parametrize("spec", load_shipped_specs(), ids=lambda s: s.id)
def test_computed_rtp_matches_play(spec) -> None:
    """Play the real engine many times and check it pays what the maths says.

    This is the end to end check: the reel strips, the random number
    generator and the calculator all have to agree.
    """
    bet = spec.bet_tiers[0]
    spins = 200_000
    staked = 0
    paid = 0
    strip_lengths = spec.strip_lengths()

    for sample in range(spins):
        seed = sample.to_bytes(rng.SEED_BYTES, "big")
        stops = rng.derive_stops(seed, strip_lengths)
        staked = staked + bet
        paid = paid + evaluate(spec, stops, bet).total_payout_cents

    measured = paid / staked
    expected, _ = base_rtp(spec)
    assert measured == pytest.approx(expected, abs=0.03)


def test_par_sheet_renders() -> None:
    for spec in load_shipped_specs():
        text = render(par_sheet(spec, samples=2000))
        assert "PROBABILITY ACCOUNTING REPORT" in text
        assert spec.name in text
        assert "Return to player" in text
        assert "House edge" in text
