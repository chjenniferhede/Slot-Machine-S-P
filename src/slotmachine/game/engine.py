"""Working out what a spin shows and what it pays.

Everything here is pure: the same stops and bet always give the same result.
No randomness, no database, no clock. That is what lets the payout maths be
checked exactly rather than estimated by simulation.

Matching runs left to right from the leftmost reel, which is the normal slot
convention. Three sevens in the middle of a line pay nothing; the run has to
start at reel 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from slotmachine.game.spec import GameSpec


@dataclass(frozen=True)
class LineWin:
    """One payline that paid, and what it paid."""

    line_number: int
    symbol: str
    count: int
    payout_cents: int


@dataclass(frozen=True)
class SpinResult:
    """Everything about one spin: where it stopped, what showed, what it paid."""

    stops: tuple[int, ...]
    grid: tuple[tuple[str, ...], ...]
    wins: tuple[LineWin, ...]
    total_payout_cents: int


def grid_from_stops(spec: GameSpec, stops: list[int]) -> tuple[tuple[str, ...], ...]:
    """The symbols on show, as rows from top to bottom.

    A reel is a loop, so a stop near the end of a strip wraps back to the
    start.
    """
    if len(stops) != spec.reel_count:
        raise ValueError(f"expected {spec.reel_count} stops, got {len(stops)}")

    grid = []
    for row in range(spec.rows):
        row_symbols = []
        for reel_number, stop in enumerate(stops):
            reel = spec.reels[reel_number]
            position = (stop + row) % len(reel)
            row_symbols.append(reel[position])
        grid.append(tuple(row_symbols))
    return tuple(grid)


def evaluate(spec: GameSpec, stops: list[int], bet_cents: int) -> SpinResult:
    """Work out the result of one spin."""
    if bet_cents <= 0:
        raise ValueError("bet must be positive")

    grid = grid_from_stops(spec, stops)

    wins = []
    total = 0
    for line_number, payline in enumerate(spec.paylines):
        win = _evaluate_line(spec, grid, line_number, payline, bet_cents)
        if win is not None:
            wins.append(win)
            total = total + win.payout_cents

    return SpinResult(
        stops=tuple(stops),
        grid=grid,
        wins=tuple(wins),
        total_payout_cents=total,
    )


def _evaluate_line(
    spec: GameSpec,
    grid: tuple[tuple[str, ...], ...],
    line_number: int,
    payline: tuple[int, ...],
    bet_cents: int,
) -> LineWin | None:
    """What one payline pays, or None if it pays nothing.

    Counts how many reels from the left show the same symbol as reel 0, then
    looks that up in the paytable. A line pays once, for its longest run.
    """
    symbols_on_line = []
    for reel_number, row in enumerate(payline):
        symbols_on_line.append(grid[row][reel_number])

    first_symbol = symbols_on_line[0]

    run_length = 1
    for symbol in symbols_on_line[1:]:
        if symbol != first_symbol:
            break
        run_length = run_length + 1

    # A shorter run can still pay even when the longest one does not, so walk
    # down from the longest and take the first rule that exists.
    while run_length >= 1:
        multiplier = spec.payout_multiplier(first_symbol, run_length)
        if multiplier > 0:
            return LineWin(
                line_number=line_number,
                symbol=first_symbol,
                count=run_length,
                payout_cents=bet_cents * multiplier,
            )
        run_length = run_length - 1

    return None


def render_grid(spec: GameSpec, stops: list[int]) -> str:
    """The reels drawn as a box, for the terminal."""
    grid = grid_from_stops(spec, stops)
    width = max(len(symbol) for symbol in spec.symbols) + 2

    top = "╔" + "╦".join("═" * width for _ in range(spec.reel_count)) + "╗"
    bottom = "╚" + "╩".join("═" * width for _ in range(spec.reel_count)) + "╝"

    lines = [top]
    for row in grid:
        cells = [symbol.center(width) for symbol in row]
        lines.append("║" + "║".join(cells) + "║")
    lines.append(bottom)
    return "\n".join(lines)
