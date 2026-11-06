"""What a game is made of, and how to load one safely.

A game is data, not code: reels, paylines and a paytable stored as JSON, so a
new game needs no new Python. The JSON is checked field by field on load,
because a bad game config would otherwise cause confusing failures much later,
during a real spin.

Always JSON. Never pickle, which would run whatever code the file contained.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Final

GAME_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9_]{1,32}$")


class SpecError(ValueError):
    """A game config is malformed. The message names the offending field."""


@dataclass(frozen=True)
class PayRule:
    """``count`` of ``symbol`` in a row pays the bet times ``multiplier``."""

    symbol: str
    count: int
    multiplier: int


@dataclass(frozen=True)
class HoldPolicy:
    """The rules for keeping reels and re-spinning the rest.

    ``max_reels_held`` is the main lever on how much a hold is worth. Keeping
    two matching reels to chase the third is worth far more than keeping one,
    so allowing it forces the whole paytable down.

    ``cost_percent`` is a percentage of the original bet, so 200 means a
    re-spin costs twice the bet. Charging more than one bet is what lets the
    base paytable stay generous: a re-spin is only worth taking when the held
    reel is good enough to beat the price.
    """

    enabled: bool
    max_consecutive: int
    max_reels_held: int
    cost_percent: int


@dataclass(frozen=True)
class GameSpec:
    """One complete game.

    ``reels`` holds the symbols around each reel, in order. ``paylines`` gives,
    for each winning line, which row to read on each reel.
    """

    id: str
    name: str
    symbols: tuple[str, ...]
    reels: tuple[tuple[str, ...], ...]
    rows: int
    paylines: tuple[tuple[int, ...], ...]
    paytable: tuple[PayRule, ...]
    bet_tiers: tuple[int, ...]
    holds: HoldPolicy

    @property
    def reel_count(self) -> int:
        return len(self.reels)

    def strip_lengths(self) -> list[int]:
        """How many positions each reel has, for the random number generator."""
        return [len(reel) for reel in self.reels]

    def payout_multiplier(self, symbol: str, count: int) -> int:
        """The multiplier for this many matching symbols, or 0 if it pays nothing."""
        for rule in self.paytable:
            if rule.symbol == symbol and rule.count == count:
                return rule.multiplier
        return 0


def load_spec(json_text: str) -> GameSpec:
    """Build a GameSpec from JSON, rejecting anything malformed."""
    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as error:
        raise SpecError(f"not valid JSON: {error}") from None

    if not isinstance(raw, dict):
        raise SpecError("a game config must be a JSON object")

    game_id = _require_string(raw, "id")
    if GAME_ID_PATTERN.match(game_id) is None:
        raise SpecError("id must be 1-32 characters of a-z, 0-9 or underscore")

    name = _require_string(raw, "name")
    symbols = _load_symbols(raw)
    reels = _load_reels(raw, symbols)
    rows = _load_rows(raw, reels)
    paylines = _load_paylines(raw, len(reels), rows)
    paytable = _load_paytable(raw, symbols, len(reels))
    bet_tiers = _load_bet_tiers(raw)
    holds = _load_holds(raw)

    return GameSpec(
        id=game_id,
        name=name,
        symbols=symbols,
        reels=reels,
        rows=rows,
        paylines=paylines,
        paytable=paytable,
        bet_tiers=bet_tiers,
        holds=holds,
    )


def dump_spec(spec: GameSpec) -> str:
    """Turn a GameSpec back into JSON."""
    return json.dumps(
        {
            "id": spec.id,
            "name": spec.name,
            "symbols": list(spec.symbols),
            "reels": [list(reel) for reel in spec.reels],
            "rows": spec.rows,
            "paylines": [list(line) for line in spec.paylines],
            "paytable": [
                {"symbol": r.symbol, "count": r.count, "multiplier": r.multiplier}
                for r in spec.paytable
            ],
            "bet_tiers": list(spec.bet_tiers),
            "holds": {
                "enabled": spec.holds.enabled,
                "max_consecutive": spec.holds.max_consecutive,
                "max_reels_held": spec.holds.max_reels_held,
                "cost_percent": spec.holds.cost_percent,
            },
        },
        indent=2,
    )


def _require_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise SpecError(f"{field} must be a non-empty string")
    return value


def _load_symbols(raw: dict[str, Any]) -> tuple[str, ...]:
    symbols = raw.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise SpecError("symbols must be a non-empty list")
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol:
            raise SpecError("every symbol must be a non-empty string")
    if len(set(symbols)) != len(symbols):
        raise SpecError("symbols must not repeat")
    return tuple(symbols)


def _load_reels(raw: dict[str, Any], symbols: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    reels = raw.get("reels")
    if not isinstance(reels, list) or not reels:
        raise SpecError("reels must be a non-empty list")

    loaded = []
    for reel_number, reel in enumerate(reels):
        if not isinstance(reel, list) or not reel:
            raise SpecError(f"reel {reel_number} must be a non-empty list of symbols")
        for symbol in reel:
            if symbol not in symbols:
                raise SpecError(f"reel {reel_number} uses unknown symbol {symbol!r}")
        loaded.append(tuple(reel))
    return tuple(loaded)


def _load_rows(raw: dict[str, Any], reels: tuple[tuple[str, ...], ...]) -> int:
    rows = raw.get("rows")
    if not isinstance(rows, int) or isinstance(rows, bool) or rows < 1:
        raise SpecError("rows must be a whole number of at least 1")

    shortest_reel = min(len(reel) for reel in reels)
    if rows > shortest_reel:
        raise SpecError(f"rows is {rows} but the shortest reel has only {shortest_reel}")
    return rows


def _load_paylines(
    raw: dict[str, Any], reel_count: int, rows: int
) -> tuple[tuple[int, ...], ...]:
    paylines = raw.get("paylines")
    if not isinstance(paylines, list) or not paylines:
        raise SpecError("paylines must be a non-empty list")

    loaded = []
    for line_number, line in enumerate(paylines):
        if not isinstance(line, list) or len(line) != reel_count:
            raise SpecError(f"payline {line_number} must have one row per reel")
        for row in line:
            if not isinstance(row, int) or isinstance(row, bool) or not 0 <= row < rows:
                raise SpecError(f"payline {line_number} has a row outside 0..{rows - 1}")
        loaded.append(tuple(line))
    return tuple(loaded)


def _load_paytable(
    raw: dict[str, Any], symbols: tuple[str, ...], reel_count: int
) -> tuple[PayRule, ...]:
    paytable = raw.get("paytable")
    if not isinstance(paytable, list) or not paytable:
        raise SpecError("paytable must be a non-empty list")

    loaded = []
    seen = set()
    for rule in paytable:
        if not isinstance(rule, dict):
            raise SpecError("every paytable rule must be an object")

        symbol = rule.get("symbol")
        count = rule.get("count")
        multiplier = rule.get("multiplier")

        if symbol not in symbols:
            raise SpecError(f"paytable uses unknown symbol {symbol!r}")
        if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= reel_count:
            raise SpecError(f"paytable count for {symbol} must be between 1 and {reel_count}")
        if not isinstance(multiplier, int) or isinstance(multiplier, bool) or multiplier < 1:
            raise SpecError(f"paytable multiplier for {symbol} must be at least 1")
        if (symbol, count) in seen:
            raise SpecError(f"paytable has two rules for {count} x {symbol}")

        seen.add((symbol, count))
        loaded.append(PayRule(symbol=symbol, count=count, multiplier=multiplier))
    return tuple(loaded)


def _load_bet_tiers(raw: dict[str, Any]) -> tuple[int, ...]:
    bet_tiers = raw.get("bet_tiers")
    if not isinstance(bet_tiers, list) or not bet_tiers:
        raise SpecError("bet_tiers must be a non-empty list of amounts in cents")

    previous = 0
    for tier in bet_tiers:
        if not isinstance(tier, int) or isinstance(tier, bool) or tier <= 0:
            raise SpecError("every bet tier must be a positive whole number of cents")
        if tier <= previous:
            raise SpecError("bet_tiers must increase")
        previous = tier
    return tuple(bet_tiers)


def _load_holds(raw: dict[str, Any]) -> HoldPolicy:
    holds = raw.get("holds")
    if not isinstance(holds, dict):
        raise SpecError("holds must be an object")

    enabled = holds.get("enabled")
    max_consecutive = holds.get("max_consecutive")
    max_reels_held = holds.get("max_reels_held")
    cost_percent = holds.get("cost_percent")

    if not isinstance(enabled, bool):
        raise SpecError("holds.enabled must be true or false")
    if (
        not isinstance(max_consecutive, int)
        or isinstance(max_consecutive, bool)
        or max_consecutive < 0
    ):
        raise SpecError("holds.max_consecutive must be 0 or more")
    if (
        not isinstance(max_reels_held, int)
        or isinstance(max_reels_held, bool)
        or max_reels_held < 0
    ):
        raise SpecError("holds.max_reels_held must be 0 or more")
    if (
        not isinstance(cost_percent, int)
        or isinstance(cost_percent, bool)
        or not 1 <= cost_percent <= 500
    ):
        raise SpecError("holds.cost_percent must be between 1 and 500")

    return HoldPolicy(
        enabled=enabled,
        max_consecutive=max_consecutive,
        max_reels_held=max_reels_held,
        cost_percent=cost_percent,
    )
