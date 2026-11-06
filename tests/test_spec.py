"""Loading and validating game configs."""

from __future__ import annotations

import json
import pickle

import pytest

from slotmachine.game.spec import SpecError, dump_spec, load_spec
from slotmachine.tools.rtpcalc import load_shipped_specs

GOOD = {
    "id": "toy",
    "name": "Toy",
    "symbols": ["A", "B"],
    "reels": [["A", "A", "B", "B"], ["A", "A", "B", "B"]],
    "rows": 1,
    "paylines": [[0, 0]],
    "paytable": [{"symbol": "A", "count": 2, "multiplier": 2}],
    "bet_tiers": [100, 500],
    "holds": {
        "enabled": False,
        "max_consecutive": 0,
        "max_reels_held": 0,
        "cost_percent": 100,
    },
}


def spec_with(**changes) -> str:
    raw = json.loads(json.dumps(GOOD))
    raw.update(changes)
    return json.dumps(raw)


def test_loads_valid() -> None:
    spec = load_spec(json.dumps(GOOD))
    assert spec.id == "toy"
    assert spec.reel_count == 2
    assert spec.strip_lengths() == [4, 4]
    assert spec.payout_multiplier("A", 2) == 2
    assert spec.payout_multiplier("B", 2) == 0


def test_roundtrip() -> None:
    spec = load_spec(json.dumps(GOOD))
    assert load_spec(dump_spec(spec)) == spec


@pytest.mark.parametrize(
    ("changes", "because"),
    [
        ({"id": "Has Capitals"}, "id must be lowercase"),
        ({"id": ""}, "id cannot be empty"),
        ({"symbols": []}, "needs at least one symbol"),
        ({"symbols": ["A", "A"]}, "symbols must not repeat"),
        ({"reels": []}, "needs at least one reel"),
        ({"reels": [[], []]}, "a reel cannot be empty"),
        ({"reels": [["A", "Z"], ["A", "B"]]}, "unknown symbol on a reel"),
        ({"rows": 0}, "rows must be at least 1"),
        ({"rows": 99}, "rows cannot exceed the shortest reel"),
        ({"paylines": []}, "needs at least one payline"),
        ({"paylines": [[0]]}, "payline must cover every reel"),
        ({"paylines": [[0, 5]]}, "payline row is out of range"),
        ({"paytable": []}, "needs at least one pay rule"),
        ({"paytable": [{"symbol": "Z", "count": 2, "multiplier": 2}]}, "unknown symbol"),
        ({"paytable": [{"symbol": "A", "count": 9, "multiplier": 2}]}, "count too high"),
        ({"paytable": [{"symbol": "A", "count": 0, "multiplier": 2}]}, "count too low"),
        ({"paytable": [{"symbol": "A", "count": 2, "multiplier": 0}]}, "multiplier too low"),
        (
            {
                "paytable": [
                    {"symbol": "A", "count": 2, "multiplier": 2},
                    {"symbol": "A", "count": 2, "multiplier": 5},
                ]
            },
            "two rules for the same combination",
        ),
        ({"bet_tiers": []}, "needs at least one bet"),
        ({"bet_tiers": [500, 100]}, "bets must increase"),
        ({"bet_tiers": [0]}, "bets must be positive"),
        ({"holds": {"enabled": True}}, "hold policy is incomplete"),
        (
            {
                "holds": {
                    "enabled": True,
                    "max_consecutive": 1,
                    "max_reels_held": 1,
                    "cost_percent": 0,
                }
            },
            "re-spin cannot be free",
        ),
    ],
)
def test_rejects(changes: dict, because: str) -> None:
    with pytest.raises(SpecError):
        load_spec(spec_with(**changes))


def test_rejects_non_json() -> None:
    with pytest.raises(SpecError):
        load_spec("not json at all")


def test_rejects_pickle_payload() -> None:
    """A pickled object must be refused, not unpickled.

    pickle.loads on data from outside the program runs whatever code the data
    contains. Game configs are JSON for exactly this reason.
    """
    payload = pickle.dumps({"id": "toy"})
    with pytest.raises(SpecError):
        load_spec(payload.decode("latin-1"))


def test_shipped_specs_load() -> None:
    specs = load_shipped_specs()
    assert [s.id for s in specs] == ["classic_sevens", "five_line_deluxe"]
    for spec in specs:
        assert spec.paylines
        assert spec.bet_tiers
