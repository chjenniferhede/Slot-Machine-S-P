"""Works out exactly what each game pays back, and prints a PAR sheet.

A PAR sheet (Probability Accounting Report) is the document a slot
manufacturer keeps for each machine: the reel strips, the paytable, the
return to player and the hit frequency. It is what a gambling commissioner
asks to see.

RTP is computed exactly, not simulated. Trying every combination of reel
positions would be 24**5 for the five-reel game, which is far too slow, so
instead the expected payout of one payline is summed over every combination
of symbols. Each reel's symbol chances are fixed, and every payline reads one
symbol per reel, so all paylines have the same expected payout and the total
is simply that times the number of lines.

This shortcut holds only while no payout depends on symbols off its own
payline. Adding a scatter symbol, which pays from anywhere on the grid, would
break it.
"""

from __future__ import annotations

import argparse
import itertools
import sys
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from slotmachine import rng
from slotmachine.game.engine import evaluate
from slotmachine.game.spec import GameSpec, load_spec

SHIPPED_SPECS = ("classic_sevens.json", "five_line_deluxe.json")


@dataclass(frozen=True)
class SymbolShare:
    """How much of one reel a symbol takes up."""

    symbol: str
    positions: int
    chance: float


@dataclass(frozen=True)
class PayLine:
    """One line of the PAR sheet's paytable section."""

    symbol: str
    count: int
    multiplier: int
    chance: float
    rtp_share: float


@dataclass(frozen=True)
class ParSheet:
    """The full report for one game."""

    spec: GameSpec
    reel_shares: list[list[SymbolShare]]
    pay_lines: list[PayLine]
    base_rtp: float
    hit_frequency: float
    hit_frequency_is_estimate: bool
    optimal_hold_rtp: float | None


def symbol_chances(spec: GameSpec) -> list[dict[str, float]]:
    """For each reel, how likely each symbol is to land on a given row."""
    chances = []
    for reel in spec.reels:
        counts = Counter(reel)
        chances.append({s: counts[s] / len(reel) for s in spec.symbols if counts[s]})
    return chances


def _sequence_payout(spec: GameSpec, sequence: tuple[str, ...]) -> int:
    """The multiplier one payline pays for this run of symbols, left to right."""
    first = sequence[0]
    run_length = 1
    for symbol in sequence[1:]:
        if symbol != first:
            break
        run_length = run_length + 1

    while run_length >= 1:
        multiplier = spec.payout_multiplier(first, run_length)
        if multiplier > 0:
            return multiplier
        run_length = run_length - 1
    return 0


def _all_sequences(chances: list[dict[str, float]]):
    """Every combination of symbols across the reels, with its chance."""
    reels = [list(reel.items()) for reel in chances]
    for combination in itertools.product(*reels):
        sequence = tuple(symbol for symbol, _ in combination)
        chance = 1.0
        for _, reel_chance in combination:
            chance = chance * reel_chance
        yield sequence, chance


def base_rtp(spec: GameSpec) -> tuple[float, list[PayLine]]:
    """Expected payback per unit bet, and the breakdown by paytable row."""
    chances = symbol_chances(spec)
    line_count = len(spec.paylines)

    totals: dict[tuple[str, int], tuple[float, float]] = {}
    for sequence, chance in _all_sequences(chances):
        multiplier = _sequence_payout(spec, sequence)
        if multiplier == 0:
            continue

        first = sequence[0]
        run_length = 1
        for symbol in sequence[1:]:
            if symbol != first:
                break
            run_length = run_length + 1
        while spec.payout_multiplier(first, run_length) == 0:
            run_length = run_length - 1

        key = (first, run_length)
        seen_chance, seen_rtp = totals.get(key, (0.0, 0.0))
        totals[key] = (seen_chance + chance, seen_rtp + chance * multiplier)

    pay_lines = []
    overall = 0.0
    for (symbol, count), (chance, rtp) in totals.items():
        multiplier = spec.payout_multiplier(symbol, count)
        pay_lines.append(
            PayLine(
                symbol=symbol,
                count=count,
                multiplier=multiplier,
                chance=chance,
                rtp_share=rtp * line_count,
            )
        )
        overall = overall + rtp * line_count

    pay_lines.sort(key=lambda line: line.rtp_share, reverse=True)
    return overall, pay_lines


def hit_frequency(spec: GameSpec, samples: int = 200_000) -> tuple[float, bool]:
    """How often a spin pays anything, and whether the figure is an estimate.

    With one payline this is exact. With several, the lines share reels, so
    the chance that at least one wins cannot be worked out line by line and
    is measured by playing sample spins instead.
    """
    if len(spec.paylines) == 1:
        chances = symbol_chances(spec)
        hits = 0.0
        for sequence, chance in _all_sequences(chances):
            if _sequence_payout(spec, sequence) > 0:
                hits = hits + chance
        return hits, False

    strip_lengths = spec.strip_lengths()
    hits = 0
    for sample in range(samples):
        seed = sample.to_bytes(rng.SEED_BYTES, "big")
        stops = rng.derive_stops(seed, strip_lengths)
        if evaluate(spec, stops, 100).total_payout_cents > 0:
            hits = hits + 1
    return hits / samples, True


def optimal_hold_rtp(spec: GameSpec) -> float | None:
    """Payback for a player who always holds the best reels.

    A hold is a free choice, so a player who uses it well gets back more than
    the base game pays. Each re-spin costs another bet, which is what keeps
    the game in the casino's favour, and this works out whether that is still
    true against perfect play.

    Returns None for a game without holds.
    """
    if not spec.holds.enabled or spec.holds.max_consecutive == 0:
        return None

    chances = symbol_chances(spec)
    reel_count = spec.reel_count
    respin_cost = spec.holds.cost_percent / 100
    max_holds = spec.holds.max_consecutive

    def payout_of(state: tuple[str, ...]) -> float:
        return float(_sequence_payout(spec, state) * len(spec.paylines))

    @lru_cache(maxsize=None)
    def best_continuation(state: tuple[str, ...], holds_used: int) -> tuple[float, float]:
        """Expected further payout and further stake, playing on from here."""
        if holds_used >= max_holds:
            return (0.0, 0.0)

        best = (0.0, 0.0)  # stopping: no more payout, no more stake
        best_net = 0.0

        for held in _hold_choices(reel_count, spec.holds.max_reels_held):
            payout_sum = 0.0
            stake_sum = respin_cost

            respun = [r for r in range(reel_count) if r not in held]
            options = [list(chances[r].items()) for r in respun]

            for combination in itertools.product(*options):
                chance = 1.0
                next_state = list(state)
                for reel_number, (symbol, reel_chance) in zip(respun, combination):
                    next_state[reel_number] = symbol
                    chance = chance * reel_chance

                frozen = tuple(next_state)
                later_payout, later_stake = best_continuation(frozen, holds_used + 1)
                payout_sum = payout_sum + chance * (payout_of(frozen) + later_payout)
                stake_sum = stake_sum + chance * later_stake

            net = payout_sum - stake_sum
            if net > best_net:
                best_net = net
                best = (payout_sum, stake_sum)

        return best

    total_payout = 0.0
    total_stake = 1.0
    for sequence, chance in _all_sequences(chances):
        later_payout, later_stake = best_continuation(sequence, 0)
        total_payout = total_payout + chance * (payout_of(sequence) + later_payout)
        total_stake = total_stake + chance * later_stake

    return total_payout / total_stake


def _hold_choices(reel_count: int, max_reels_held: int) -> list[tuple[int, ...]]:
    """Every set of reels a player may hold, leaving at least one to re-spin."""
    largest = min(max_reels_held, reel_count - 1)
    choices = []
    for size in range(largest + 1):
        for held in itertools.combinations(range(reel_count), size):
            choices.append(held)
    return choices


def par_sheet(spec: GameSpec, samples: int = 200_000) -> ParSheet:
    """Work out every figure in the report for one game."""
    rtp, pay_lines = base_rtp(spec)
    hits, estimated = hit_frequency(spec, samples)
    return ParSheet(
        spec=spec,
        reel_shares=_reel_shares(spec),
        pay_lines=pay_lines,
        base_rtp=rtp,
        hit_frequency=hits,
        hit_frequency_is_estimate=estimated,
        optimal_hold_rtp=optimal_hold_rtp(spec),
    )


def _reel_shares(spec: GameSpec) -> list[list[SymbolShare]]:
    shares = []
    for reel in spec.reels:
        counts = Counter(reel)
        shares.append(
            [
                SymbolShare(symbol=s, positions=counts[s], chance=counts[s] / len(reel))
                for s in spec.symbols
                if counts[s]
            ]
        )
    return shares


def render(sheet: ParSheet) -> str:
    """The PAR sheet as text."""
    spec = sheet.spec
    out = []
    out.append(f"PROBABILITY ACCOUNTING REPORT -- {spec.name} ({spec.id})")

    holds = "no"
    if spec.holds.enabled:
        holds = f"yes (up to {spec.holds.max_consecutive}, {spec.holds.cost_percent}% of bet)"
    out.append(
        f"  Reels: {spec.reel_count}   Rows: {spec.rows}   "
        f"Paylines: {len(spec.paylines)}   Holds: {holds}"
    )
    bets = ", ".join(f"${t / 100:.2f}" for t in spec.bet_tiers)
    out.append(f"  Bets: {bets}")
    out.append("")

    out.append("  SYMBOL WEIGHTING")
    header = "    Symbol      " + "".join(f"Reel{i + 1}  " for i in range(spec.reel_count))
    out.append(header + "  Chance")
    for symbol in spec.symbols:
        cells = ""
        for reel_number in range(spec.reel_count):
            positions = next(
                (s.positions for s in sheet.reel_shares[reel_number] if s.symbol == symbol),
                0,
            )
            cells = cells + f"{positions:>5}  "
        chance = next(
            (s.chance for s in sheet.reel_shares[0] if s.symbol == symbol), 0.0
        )
        out.append(f"    {symbol:<12}{cells}  {chance:>7.4f}")
    out.append("")

    out.append("  PAYTABLE")
    out.append("    Combination          Pays   Chance      Share of RTP")
    for line in sheet.pay_lines:
        combination = f"{line.count} x {line.symbol}"
        out.append(
            f"    {combination:<20}{line.multiplier:>5}x  "
            f"{line.chance:>9.6f}   {line.rtp_share:>9.2%}"
        )
    out.append("    " + "-" * 54)
    out.append(f"    {'Return to player':<20}{'':>5}   {'':>9}   {sheet.base_rtp:>9.2%}")

    if sheet.optimal_hold_rtp is not None:
        out.append(
            f"    {'With perfect holds':<20}{'':>5}   {'':>9}   "
            f"{sheet.optimal_hold_rtp:>9.2%}"
        )

    estimate = " (estimated)" if sheet.hit_frequency_is_estimate else ""
    out.append(
        f"    {'Hit frequency':<20}{'':>5}   {'':>9}   "
        f"{sheet.hit_frequency:>9.2%}{estimate}"
    )

    edge = 1 - (sheet.optimal_hold_rtp or sheet.base_rtp)
    out.append(f"    {'House edge':<20}{'':>5}   {'':>9}   {edge:>9.2%}")
    return "\n".join(out)


def load_shipped_specs() -> list[GameSpec]:
    """Load the games that ship with the system."""
    specs = []
    for filename in SHIPPED_SPECS:
        text = resources.files("slotmachine.game.specs").joinpath(filename).read_text()
        specs.append(load_spec(text))
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slotmachine-rtpcalc",
        description="Print the PAR sheet for each game",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=200_000,
        help="sample spins used for hit frequency on multi-line games",
    )
    args = parser.parse_args(argv)

    for spec in load_shipped_specs():
        print(render(par_sheet(spec, args.samples)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
