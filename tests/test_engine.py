"""Working out what a spin shows and pays."""

from __future__ import annotations

import json

import pytest

from slotmachine.game.engine import evaluate, grid_from_stops, render_grid
from slotmachine.game.spec import load_spec

# Three reels, one row, one payline. Reel positions are easy to reason about:
# position 0 is A, 1 is B, 2 is C, 3 is D.
TOY = load_spec(
    json.dumps(
        {
            "id": "toy",
            "name": "Toy",
            "symbols": ["A", "B", "C", "D"],
            "reels": [["A", "B", "C", "D"]] * 3,
            "rows": 1,
            "paylines": [[0, 0, 0]],
            "paytable": [
                {"symbol": "A", "count": 3, "multiplier": 10},
                {"symbol": "A", "count": 2, "multiplier": 3},
                {"symbol": "B", "count": 3, "multiplier": 5},
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

# Three rows and five reels, so the V and inverted-V paylines can be checked.
GRID = load_spec(
    json.dumps(
        {
            "id": "grid",
            "name": "Grid",
            "symbols": ["A", "B", "C"],
            "reels": [["A", "B", "C"]] * 5,
            "rows": 3,
            "paylines": [
                [0, 0, 0, 0, 0],
                [1, 1, 1, 1, 1],
                [2, 2, 2, 2, 2],
                [0, 1, 2, 1, 0],
                [2, 1, 0, 1, 2],
            ],
            "paytable": [
                {"symbol": "A", "count": 3, "multiplier": 2},
                {"symbol": "A", "count": 5, "multiplier": 50},
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


def test_grid_reads_the_stop_position() -> None:
    assert grid_from_stops(TOY, [0, 1, 2]) == (("A", "B", "C"),)


def test_grid_wraps_around() -> None:
    """A reel is a loop, so row 1 of a stop at the last position is position 0."""
    assert grid_from_stops(GRID, [2, 2, 2, 2, 2])[1] == ("A", "A", "A", "A", "A")


def test_grid_rejects_wrong_number_of_stops() -> None:
    with pytest.raises(ValueError):
        grid_from_stops(TOY, [0, 1])


def test_three_of_a_kind() -> None:
    result = evaluate(TOY, [0, 0, 0], 100)
    assert result.total_payout_cents == 1000
    assert len(result.wins) == 1
    assert result.wins[0].symbol == "A"
    assert result.wins[0].count == 3


def test_two_of_a_kind() -> None:
    result = evaluate(TOY, [0, 0, 1], 100)
    assert result.total_payout_cents == 300
    assert result.wins[0].count == 2


def test_no_win() -> None:
    result = evaluate(TOY, [2, 3, 0], 100)
    assert result.total_payout_cents == 0
    assert result.wins == ()


def test_run_must_start_at_the_leftmost_reel() -> None:
    """B,A,A pays nothing even though two As are showing."""
    assert evaluate(TOY, [1, 0, 0], 100).total_payout_cents == 0


def test_longest_run_is_paid_once() -> None:
    """Three As pay the 3-rule only, not the 3-rule plus the 2-rule."""
    result = evaluate(TOY, [0, 0, 0], 100)
    assert len(result.wins) == 1
    assert result.total_payout_cents == 1000


def test_shorter_run_pays_when_the_longer_one_does_not() -> None:
    """Two Bs have no rule, so the line pays nothing rather than erroring."""
    assert evaluate(TOY, [1, 1, 0], 100).total_payout_cents == 0


def test_payout_scales_with_bet() -> None:
    assert evaluate(TOY, [0, 0, 0], 25).total_payout_cents == 250
    assert evaluate(TOY, [0, 0, 0], 500).total_payout_cents == 5000


def test_rejects_non_positive_bet() -> None:
    with pytest.raises(ValueError):
        evaluate(TOY, [0, 0, 0], 0)


def test_every_payline_can_win() -> None:
    """One case per line, each winning on that line alone."""
    # All stops at 0 gives rows A / B / C, so only the top row is all A.
    top_row = evaluate(GRID, [0, 0, 0, 0, 0], 100)
    assert [w.line_number for w in top_row.wins] == [0]

    # Stops at 2 put A on the middle row everywhere.
    middle = evaluate(GRID, [2, 2, 2, 2, 2], 100)
    assert [w.line_number for w in middle.wins] == [1]

    # Stops at 1 put A on the bottom row everywhere.
    bottom = evaluate(GRID, [1, 1, 1, 1, 1], 100)
    assert [w.line_number for w in bottom.wins] == [2]

    # V shape: rows 0,1,2,1,0 must all be A.
    v_shape = evaluate(GRID, [0, 2, 1, 2, 0], 100)
    assert 3 in [w.line_number for w in v_shape.wins]

    # Inverted V: rows 2,1,0,1,2 must all be A.
    inverted = evaluate(GRID, [1, 2, 0, 2, 1], 100)
    assert 4 in [w.line_number for w in inverted.wins]


def test_multiple_lines_add_up() -> None:
    result = evaluate(GRID, [0, 2, 1, 2, 0], 100)
    assert result.total_payout_cents == sum(w.payout_cents for w in result.wins)


def test_evaluate_is_deterministic() -> None:
    first = evaluate(GRID, [0, 1, 2, 1, 0], 100)
    for _ in range(100):
        assert evaluate(GRID, [0, 1, 2, 1, 0], 100) == first


def test_evaluate_uses_no_randomness_or_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine must be pure, so the payout maths can be checked exactly."""
    import datetime
    import secrets

    def explode(*args, **kwargs):
        raise AssertionError("the engine must not use randomness or the clock")

    monkeypatch.setattr(secrets, "token_bytes", explode)
    monkeypatch.setattr(datetime, "datetime", explode)
    assert evaluate(GRID, [0, 0, 0, 0, 0], 100).total_payout_cents > 0


def test_render_grid_draws_a_box() -> None:
    drawn = render_grid(TOY, [0, 1, 2])
    lines = drawn.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("╔")
    assert "A" in lines[1] and "B" in lines[1] and "C" in lines[1]
