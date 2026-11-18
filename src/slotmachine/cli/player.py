"""The player interface: account, cards, deposits, playing, and statistics.

Every action here calls a function in accounts.py, payment.py or stats.py
that carries ``@auth.require_role(Role.PLAYER)``, re-checks the caller's
identity in the database, and looks up cards and game sessions by the
caller's own user id. This module only prompts and prints; it holds no money
and makes no decisions about it.
"""

from __future__ import annotations

import sqlite3
from typing import Final

from slotmachine import accounts, auth, payment, stats
from slotmachine.accounts import AccountError, GameSession, SpinOutcome
from slotmachine.cli.common import change_password, heading, percent
from slotmachine.cli.menus import MenuItem, menu, table
from slotmachine.cli.prompts import (
    error,
    info,
    prompt_choice,
    prompt_int,
    prompt_money,
    prompt_secret,
    prompt_yes_no,
    success,
)
from slotmachine.game import GameSpec
from slotmachine.game.engine import render_grid
from slotmachine.money import Money

#: Names for the paylines of a three-row game, in the order the specs list them.
THREE_ROW_LINE_NAMES: Final[tuple[str, ...]] = ("top row", "middle row", "bottom row", "V", "inverted V")


def player_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    """The player's main menu. Leaving it cashes out any game still open."""
    try:
        menu(
            "PLAYER",
            [
                MenuItem("Play a slot game", lambda: _play(session, conn)),
                MenuItem("My account: balance, cards, deposit, withdraw", lambda: _account_menu(session, conn)),
                MenuItem("My playing statistics", lambda: _show_stats(session, conn)),
                MenuItem("Change my password", lambda: change_password(conn, session)),
            ],
            session,
            back_label="Log out",
        )
    finally:
        _cash_out_if_open(session, conn)


# ---------------------------------------------------------------------------
# Account and cards
# ---------------------------------------------------------------------------


def _account_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    menu(
        "MY ACCOUNT",
        [
            MenuItem("Show balance", lambda: _show_balance(session, conn)),
            MenuItem("Show linked cards", lambda: _show_cards(session, conn)),
            MenuItem("Link a debit/credit card", lambda: _add_card(session, conn)),
            MenuItem("Remove a card", lambda: _remove_card(session, conn)),
            MenuItem("Deposit funds from a card", lambda: _deposit(session, conn)),
            MenuItem("Withdraw funds to a card", lambda: _withdraw(session, conn)),
        ],
        session,
    )


def _show_balance(session: auth.Session, conn: sqlite3.Connection) -> None:
    info(f"Account balance: {accounts.get_balance(session, conn)}")


def _show_cards(session: auth.Session, conn: sqlite3.Connection) -> list[payment.StoredMethod]:
    methods = payment.list_payment_methods(session, conn)
    if not methods:
        info("No cards linked yet. Choose 'Link a debit/credit card' first.")
        return methods
    print(table(
        ["#", "Card", "Expires"],
        [[m.id, str(m), f"{m.exp_month:02d}/{m.exp_year}"] for m in methods],
    ))
    return methods


def _add_card(session: auth.Session, conn: sqlite3.Connection) -> None:
    info("The card number and security code are hidden as you type and are never stored.")
    number = prompt_secret("Card number")
    month = prompt_int("Expiry month (1-12)", minimum=1, maximum=12)
    year = prompt_int("Expiry year (e.g. 2029)", minimum=2000, maximum=2100)
    cvv = prompt_secret("Security code (CVV)")
    method = payment.add_payment_method(
        session, conn, payment.CardInput(number=number, exp_month=month, exp_year=year, cvv=cvv)
    )
    success(f"Linked {method} as card #{method.id}.")


def _pick_card(session: auth.Session, conn: sqlite3.Connection) -> int | None:
    methods = _show_cards(session, conn)
    if not methods:
        return None
    return prompt_int("Card #", minimum=1)


def _remove_card(session: auth.Session, conn: sqlite3.Connection) -> None:
    method_id = _pick_card(session, conn)
    if method_id is None:
        return
    payment.remove_payment_method(session, conn, method_id)
    success(f"Card #{method_id} removed.")


def _deposit(session: auth.Session, conn: sqlite3.Connection) -> None:
    method_id = _pick_card(session, conn)
    if method_id is None:
        return
    info(f"Daily deposit limit: {accounts.DAILY_DEPOSIT_LIMIT}.")
    amount = prompt_money("Amount to deposit", maximum=accounts.DAILY_DEPOSIT_LIMIT)
    balance = accounts.deposit(session, conn, method_id, amount)
    success(f"Deposited {amount}. Balance: {balance}")


def _withdraw(session: auth.Session, conn: sqlite3.Connection) -> None:
    method_id = _pick_card(session, conn)
    if method_id is None:
        return
    info(f"Balance: {accounts.get_balance(session, conn)}. Daily withdrawal limit: {accounts.DAILY_WITHDRAW_LIMIT}.")
    amount = prompt_money("Amount to withdraw", maximum=accounts.DAILY_WITHDRAW_LIMIT)
    balance = accounts.withdraw(session, conn, method_id, amount)
    success(f"Withdrew {amount}. Balance: {balance}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _show_stats(session: auth.Session, conn: sqlite3.Connection) -> None:
    s = stats.view_own_statistics(session, conn)
    heading("My playing statistics")
    print(table(
        ["Measure", "Value"],
        [
            ["Game sessions", f"{s.sessions:,}"],
            ["Spins played", f"{s.spins:,}"],
            ["Winning spins", f"{s.wins:,}"],
            ["Win percentage", percent(s.win_percent)],
            ["Total wagered", str(s.wagered)],
            ["Total won", str(s.won)],
            ["Net result", str(s.net)],
            ["Total lost", str(s.total_lost)],
            ["Highest payout", str(s.biggest_payout)],
            ["Favourite game", s.favourite_game or "-"],
        ],
        align="<>",
    ))


# ---------------------------------------------------------------------------
# Playing
# ---------------------------------------------------------------------------


def _play(session: auth.Session, conn: sqlite3.Connection) -> None:
    gs = _resume_or_buy_in(session, conn)
    if gs is None:
        return
    spec = accounts.current_game_rules(session, conn)
    if spec is None:
        return
    bet = Money(spec.bet_tiers[0])
    last: SpinOutcome | None = None
    _show_paytable(spec)

    while True:
        credits = accounts.get_credits(session, conn, gs)
        print()
        info(f"{spec.name}   Credits: {credits}   Bet: {bet}")
        options = [("s", f"Spin ({bet})")]
        if last is not None and last.respin_cost is not None:
            options.append(("h", f"Hold & re-spin ({last.respin_cost})"))
        options += [("b", "Change bet"), ("p", "Paytable"), ("x", "Cash out")]
        choice = prompt_choice("", options)

        try:
            if choice == "s":
                last = accounts.spin(session, conn, gs, bet)
                _show_outcome(spec, last)
            elif choice == "h" and last is not None:
                held = _choose_holds(spec)
                last = accounts.hold_and_respin(session, conn, gs, held)
                _show_outcome(spec, last)
            elif choice == "b":
                bet = _choose_bet(spec)
            elif choice == "p":
                _show_paytable(spec)
            elif choice == "x":
                done = accounts.cash_out(session, conn, gs)
                verdict = "up" if done.net.is_positive() else "down" if done.net.is_negative() else "even"
                success(f"Cashed out {done.amount} to your account after {done.spins} spin(s).")
                info(f"This session you are {verdict} {Money(abs(done.net.cents))}.")
                info(f"Account balance: {accounts.get_balance(session, conn)}")
                return
        except AccountError as exc:
            # Not enough credits, or a hold that is no longer allowed: stay at the machine.
            error(exc)


def _resume_or_buy_in(session: auth.Session, conn: sqlite3.Connection) -> GameSession | None:
    open_game = accounts.current_game(session, conn)
    if open_game is not None:
        info("Resuming your open game.")
        return open_game

    games = accounts.list_playable_games(session, conn)
    if not games:
        info("No games are available right now.")
        return None
    heading("Choose a game")
    print(table(
        ["#", "Game", "Reels", "Paylines", "Bets", "Holds"],
        [
            [i, g.name, f"{g.reel_count}x{g.rows}", len(g.paylines),
             ", ".join(str(Money(t)) for t in g.bet_tiers), "yes" if g.holds.enabled else "no"]
            for i, g in enumerate(games, start=1)
        ],
    ))
    spec = games[prompt_int("Game #", minimum=1, maximum=len(games)) - 1]

    balance = accounts.get_balance(session, conn)
    smallest = Money(spec.bet_tiers[0])
    if balance < smallest:
        info(f"Your balance is {balance}. Deposit at least {smallest} to play {spec.name}.")
        return None
    info(f"Account balance: {balance}. Move some of it into the machine as credits.")
    buyin = prompt_money("Buy-in amount", minimum=smallest, maximum=balance)
    gs = accounts.start_session(session, conn, spec.id, buyin)
    success(f"Bought in for {buyin}. Good luck!")
    return gs


def _choose_bet(spec: GameSpec) -> Money:
    tiers = [Money(t) for t in spec.bet_tiers]
    print(table(["#", "Bet"], [[i, str(t)] for i, t in enumerate(tiers, start=1)]))
    return tiers[prompt_int("Bet #", minimum=1, maximum=len(tiers)) - 1]


def _choose_holds(spec: GameSpec) -> list[int]:
    most = spec.holds.max_reels_held
    if most == 1:
        reel = prompt_int(f"Reel to hold (1-{spec.reel_count})", minimum=1, maximum=spec.reel_count)
        return [reel - 1]
    held: list[int] = []
    while len(held) < most:
        reel = prompt_int(f"Reel to hold (1-{spec.reel_count})", minimum=1, maximum=spec.reel_count)
        held.append(reel - 1)
        if len(held) < most and not prompt_yes_no("Hold another reel?", default=False):
            break
    return held


def _line_name(spec: GameSpec, line_number: int) -> str:
    if len(spec.paylines) == 1:
        return "payline"
    if spec.rows == 3 and line_number < len(THREE_ROW_LINE_NAMES):
        return THREE_ROW_LINE_NAMES[line_number]
    return f"line {line_number + 1}"


def _show_outcome(spec: GameSpec, outcome: SpinOutcome) -> None:
    print()
    print("\n".join("  " + line for line in render_grid(spec, list(outcome.result.stops)).splitlines()))
    if outcome.held:
        info("Held: " + ", ".join(f"reel {r + 1}" for r in outcome.held))
    if outcome.result.wins:
        for win in outcome.result.wins:
            info(f"  {win.count} x {win.symbol} on the {_line_name(spec, win.line_number)}: {Money(win.payout_cents)}")
        success(f"WIN {outcome.payout}!")
    else:
        info("No win.")
    info(f"Charged {outcome.charged}. Credits: {outcome.credits}")
    if outcome.respin_cost is not None:
        info(f"You may hold reel(s) and re-spin the rest for {outcome.respin_cost}.")


def _show_paytable(spec: GameSpec) -> None:
    heading(f"{spec.name} paytable (multiples of your bet)")
    rules = sorted(spec.paytable, key=lambda r: (-r.multiplier, r.symbol))
    print(table(["Symbols", "Pays"], [[f"{r.count} x {r.symbol}", f"{r.multiplier}x"] for r in rules]))
    if len(spec.paylines) == 1:
        info("One payline. Matches count from the leftmost reel.")
    else:
        lines = ", ".join(_line_name(spec, i) for i in range(len(spec.paylines)))
        info(f"{len(spec.paylines)} paylines: {lines}. Matches count from the leftmost reel.")
    if spec.holds.enabled:
        info(
            f"Holds: after a spin you may keep up to {spec.holds.max_reels_held} reel(s) and re-spin the rest,"
            f" {spec.holds.max_consecutive} time(s) in a row, for {spec.holds.cost_percent}% of your bet."
            " A re-spin pays on your original bet."
        )


def _cash_out_if_open(session: auth.Session, conn: sqlite3.Connection) -> None:
    """On logging out, return any credits left in a machine to the account."""
    try:
        gs = accounts.current_game(session, conn)
        if gs is None:
            return
        done = accounts.cash_out(session, conn, gs)
        info(f"Your open game was cashed out: {done.amount} returned to your account.")
    except (AccountError, auth.AccessDenied, sqlite3.Error):
        # Startup recovery cashes out anything left open, so nothing is lost.
        pass
