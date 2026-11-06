"""The slot games themselves: what a game is, and what a spin pays."""

from slotmachine.game.engine import LineWin, SpinResult, evaluate, grid_from_stops
from slotmachine.game.spec import GameSpec, HoldPolicy, PayRule, SpecError, load_spec

__all__ = [
    "GameSpec",
    "HoldPolicy",
    "LineWin",
    "PayRule",
    "SpecError",
    "SpinResult",
    "evaluate",
    "grid_from_stops",
    "load_spec",
]
