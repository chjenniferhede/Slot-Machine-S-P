"""A player's money: the balance, deposits and withdrawals, and game credits.

Each movement checks the money and changes it inside one transaction, and
writes its audit entry in that same transaction, so the movement and its
record commit or roll back together. Checking first and changing the money
in a later step would let two requests both pass the check (TOCTOU, CWE-367).

Every lookup of a card or a game session includes the caller's user_id. The
players-only role check is added by Layer 2's ``@require_role`` once it lands.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, TypeGuard

from slotmachine import audit, rng, storage
from slotmachine.audit import Action
from slotmachine.game import GameSpec, SpinResult, evaluate, load_spec
from slotmachine.money import Money
from slotmachine.payment import Caller, has_expired

# Per player, per UTC calendar day. Module constants so the technician
# interface can show them.
DAILY_DEPOSIT_LIMIT: Final[Money] = Money(200_000)  # $2,000.00
DAILY_WITHDRAW_LIMIT: Final[Money] = Money(200_000)  # $2,000.00

_NO_SUCH_METHOD: Final[str] = "you have no payment method with that number"
_NO_OPEN_GAME: Final[str] = "you have no open game with that number"


class AccountError(ValueError):
    """A request to move money was refused. The message is safe to show the user."""


class _NotYours(Exception):
    """The card or session is not the caller's. Logged, then turned into AccountError."""


def get_balance(session: Caller, conn: sqlite3.Connection) -> Money:
    account = storage.get_account(conn, session.user_id)
    if account is None:
        raise AccountError("this login has no account")
    return Money(int(account["balance_cents"]))


# ---------------------------------------------------------------------------
# Deposits and withdrawals
# ---------------------------------------------------------------------------


def deposit(session: Caller, conn: sqlite3.Connection, method_id: int, amount: Money) -> Money:
    """Move money from one of the caller's cards into their account.

    Returns the new balance.
    """
    _check_request(method_id, amount)

    try:
        with storage.transaction(conn):
            _check_method(session, conn, method_id)
            account = _account_for_today(session, conn)

            deposited_today = Money(int(account["daily_deposit_cents"]))
            room = DAILY_DEPOSIT_LIMIT - deposited_today
            if amount > room:
                raise AccountError(
                    f"the daily deposit limit is {DAILY_DEPOSIT_LIMIT};"
                    f" you can deposit {room} more today"
                )

            balance = storage.record_deposit(conn, session.user_id, amount)
            audit.write(
                conn,
                session.username,
                Action.DEPOSIT,
                f"method={method_id} amount={amount} balance={balance}",
            )
    except _NotYours:
        _record_refusal(session, conn, f"deposit method={method_id}")
        raise AccountError(_NO_SUCH_METHOD) from None

    return balance


def withdraw(session: Caller, conn: sqlite3.Connection, method_id: int, amount: Money) -> Money:
    """Move money from the caller's account back to one of their cards.

    Returns the new balance.
    """
    _check_request(method_id, amount)

    try:
        with storage.transaction(conn):
            _check_method(session, conn, method_id)
            account = _account_for_today(session, conn)

            # Read inside the transaction that changes it, so nothing can
            # spend the same money between this check and the debit.
            balance = Money(int(account["balance_cents"]))
            if amount > balance:
                raise AccountError(f"you have only {balance} in your account")

            withdrawn_today = Money(int(account["daily_withdraw_cents"]))
            room = DAILY_WITHDRAW_LIMIT - withdrawn_today
            if amount > room:
                raise AccountError(
                    f"the daily withdrawal limit is {DAILY_WITHDRAW_LIMIT};"
                    f" you can withdraw {room} more today"
                )

            balance = storage.record_withdrawal(conn, session.user_id, amount)
            audit.write(
                conn,
                session.username,
                Action.WITHDRAW,
                f"method={method_id} amount={amount} balance={balance}",
            )
    except _NotYours:
        _record_refusal(session, conn, f"withdraw method={method_id}")
        raise AccountError(_NO_SUCH_METHOD) from None

    return balance


def _check_request(method_id: object, amount: object) -> None:
    if not isinstance(amount, Money):
        raise AccountError("enter an amount, for example 50.00")
    if not amount.is_positive():
        # Zero does nothing. A negative deposit would be a withdrawal that
        # skips every withdrawal check, and a negative withdrawal a deposit
        # that skips the deposit limit.
        raise AccountError("the amount must be more than $0.00")
    if not _is_whole(method_id):
        raise AccountError("choose a payment method by its number")


def _check_method(session: Caller, conn: sqlite3.Connection, method_id: int) -> None:
    """Refuse a card that is not the caller's, or that has expired since it was added."""
    method = storage.get_payment_method(conn, session.user_id, method_id)
    if method is None:
        raise _NotYours
    if has_expired(int(method["exp_month"]), int(method["exp_year"])):
        raise AccountError("this card has expired; add a new one")


def _account_for_today(session: Caller, conn: sqlite3.Connection) -> sqlite3.Row:
    """The caller's account, with the daily totals reset if the UTC date has changed.

    UTC everywhere: a reset at local midnight would let someone who changes
    their timezone start a new day twice.
    """
    storage.roll_over_daily_totals(conn, session.user_id, storage.today_bucket())
    account = storage.get_account(conn, session.user_id)
    if account is None:
        raise AccountError("this login has no account")
    return account


# ---------------------------------------------------------------------------
# Game credits: buying in, spinning, cashing out
# ---------------------------------------------------------------------------
# A machine does not bet from the account. Money moves in as credits, bets
# come out of the credits, and cashing out moves what is left back. That
# bounds what one session can lose, and gives the spec's "cash out" a real
# meaning.
#
# If a technician disables a game, sessions already open on it may carry on
# and cash out, and new sessions are refused. Stopping a session part way
# would leave the player's credits stuck in a machine they cannot use.


@dataclass(frozen=True)
class GameSession:
    """A handle on an open session.

    Credits and game rules are always read from the database, never from
    this object, so a stale or made-up handle cannot change them.
    """

    id: int
    game_id: str


@dataclass(frozen=True)
class SpinOutcome:
    """What one spin did, and what the player may do next."""

    spin_id: int
    result: SpinResult
    charged: Money  # the bet, or the price of a re-spin
    payout: Money
    credits: Money  # after this spin
    held: tuple[int, ...]  # reels kept from the spin before; empty for a fresh spin
    holds_left: int  # holds still allowed in a row from here
    respin_cost: Money | None  # what holding now would cost, or None if not allowed


@dataclass(frozen=True)
class CashOut:
    amount: Money  # credits moved back to the account
    net: Money  # paid out minus staked over the session; negative is a loss
    spins: int


def start_session(
    session: Caller, conn: sqlite3.Connection, game_id: str, buyin: Money
) -> GameSession:
    """Buy into a game: move ``buyin`` from the account into credits."""
    if not isinstance(buyin, Money) or not buyin.is_positive():
        raise AccountError("the buy-in must be more than $0.00")

    with storage.transaction(conn):
        spec = _playable_spec(conn, game_id)

        smallest_bet = Money(spec.bet_tiers[0])
        if buyin < smallest_bet:
            raise AccountError(f"buy in at least {smallest_bet}, the smallest bet on this game")

        if storage.get_open_session(conn, session.user_id) is not None:
            raise AccountError("you already have a game open; cash out of it first")

        balance = get_balance(session, conn)
        if buyin > balance:
            raise AccountError(f"you have only {balance} in your account")

        balance = storage.adjust_balance(conn, session.user_id, -buyin)
        session_id = storage.create_session(conn, session.user_id, spec.id, buyin)
        audit.write(
            conn,
            session.username,
            Action.SESSION_BUYIN,
            f"session={session_id} game={spec.id} amount={buyin} balance={balance}",
        )

    return GameSession(id=session_id, game_id=spec.id)


def spin(session: Caller, conn: sqlite3.Connection, gs: GameSession, bet: Money) -> SpinOutcome:
    """Spin every reel for ``bet``, which must be one of the game's bet tiers."""
    return _play(session, conn, gs, respin=False, bet=bet, held=())


def hold_and_respin(
    session: Caller, conn: sqlite3.Connection, gs: GameSession, held_reels: Sequence[int]
) -> SpinOutcome:
    """Keep ``held_reels`` (counting from 0) where the last spin left them, and spin the rest.

    These are the rules rtpcalc proves the game's return against: at most
    ``max_reels_held`` reels, at most ``max_consecutive`` holds in a row, a
    price of ``cost_percent`` of the original bet, and a payout worked out on
    the original bet. The held reels' positions come from the recorded spin,
    never from the caller.
    """
    return _play(session, conn, gs, respin=True, bet=None, held=held_reels)


def get_credits(session: Caller, conn: sqlite3.Connection, gs: GameSession) -> Money:
    _check_handle(gs)
    row = storage.get_session(conn, session.user_id, gs.id)
    if row is None or row["ended_at"] is not None:
        raise AccountError(_NO_OPEN_GAME)
    return Money(int(row["credits_cents"]))


def cash_out(session: Caller, conn: sqlite3.Connection, gs: GameSession) -> CashOut:
    """Move the session's credits back to the account and close the session."""
    _check_handle(gs)

    try:
        with storage.transaction(conn):
            row = _open_session(session, conn, gs)
            amount = Money(int(row["credits_cents"]))

            balance = storage.adjust_balance(conn, session.user_id, amount)
            storage.close_session(conn, gs.id)

            spins, staked, paid = storage.session_totals(conn, gs.id)
            net = paid - staked
            audit.write(
                conn,
                session.username,
                Action.SESSION_CASHOUT,
                f"session={gs.id} amount={amount} net={net} spins={spins} balance={balance}",
            )
    except _NotYours:
        _record_refusal(session, conn, f"cash_out session={gs.id}")
        raise AccountError(_NO_OPEN_GAME) from None

    return CashOut(amount=amount, net=net, spins=spins)


def recover_open_sessions(conn: sqlite3.Connection) -> int:
    """Cash out every session still open. Call once at startup.

    A session open at startup was left by a run that crashed or was killed,
    and its credits would otherwise sit in limbo. This assumes one running
    copy of the program per database, which is how the system is deployed.
    Returns how many sessions were recovered.
    """
    recovered = 0
    with storage.transaction(conn):
        for row in storage.list_open_sessions(conn):
            amount = Money(int(row["credits_cents"]))
            balance = storage.adjust_balance(conn, int(row["user_id"]), amount)
            storage.close_session(conn, int(row["id"]))
            audit.write(
                conn,
                "system",
                Action.SESSION_RECOVERED,
                f"session={row['id']} user={row['user_id']} amount={amount} balance={balance}",
            )
            recovered = recovered + 1
    return recovered


def respin_cost(spec: GameSpec, bet: Money) -> Money:
    """The price of a re-spin after a hold: ``cost_percent`` of the bet, rounded up.

    Rounded up so the price is never below the one rtpcalc assumed, which
    keeps its perfect-play return an upper bound. At 200% it is exact anyway.
    """
    return Money((bet.cents * spec.holds.cost_percent + 99) // 100)


def _play(
    session: Caller,
    conn: sqlite3.Connection,
    gs: GameSession,
    *,
    respin: bool,
    bet: object,
    held: object,
) -> SpinOutcome:
    """One spin: a fresh one for ``bet``, or with ``respin`` a re-spin keeping ``held``.

    Which it is comes only from the public function called, never from the
    values passed, so a missing bet cannot turn a spin into a re-spin.
    """
    _check_handle(gs)

    try:
        with storage.transaction(conn):
            row = _open_session(session, conn, gs)
            spec = _session_spec(conn, row)
            credits = Money(int(row["credits_cents"]))

            held_reels: tuple[int, ...] = ()
            previous_stops: list[int] = []
            if not respin:
                base_bet = _check_bet(spec, bet)
                charge = base_bet
                holds_used = 0
            else:
                held_reels = _check_hold(spec, held)
                previous_stops, base_bet, holds_used = _hold_chain(conn, spec, gs.id)
                charge = respin_cost(spec, base_bet)
                holds_used = holds_used + 1

            if charge > credits:
                raise AccountError(f"this costs {charge} and you have {credits} in credits")

            # The draw comes after every check, and every reel is drawn even
            # when some are held, so a seed always means the same positions.
            seed = rng.secure_seed()
            stops = rng.derive_stops(seed, spec.strip_lengths())
            for reel in held_reels:
                stops[reel] = previous_stops[reel]

            # Bet, result, record, payout. All in this one transaction, so a
            # crash or Ctrl-C anywhere in here leaves the player exactly where
            # they were before the spin; nobody can abort a spin they lost.
            storage.adjust_credits(conn, gs.id, -charge)
            result = evaluate(spec, stops, base_bet.cents)
            payout = Money(result.total_payout_cents)
            spin_id = storage.record_spin(
                conn,
                session_id=gs.id,
                user_id=session.user_id,
                game_id=spec.id,
                bet=charge,
                seed=seed,
                stops_json=json.dumps(stops),
                payout=payout,
                is_hold_respin=bool(held_reels),
            )
            credits = storage.adjust_credits(conn, gs.id, payout)

            detail = f"session={gs.id} spin={spin_id} bet={charge} payout={payout} credits={credits}"
            if held_reels:
                detail = detail + " held=" + ",".join(str(reel) for reel in held_reels)
            audit.write(conn, session.username, Action.SPIN, detail)
    except _NotYours:
        _record_refusal(session, conn, f"spin session={gs.id}")
        raise AccountError(_NO_OPEN_GAME) from None

    holds_left = _holds_left(spec, holds_used)
    next_cost = respin_cost(spec, base_bet) if holds_left > 0 else None
    return SpinOutcome(
        spin_id=spin_id,
        result=result,
        charged=charge,
        payout=payout,
        credits=credits,
        held=held_reels,
        holds_left=holds_left,
        respin_cost=next_cost,
    )


def _check_handle(gs: object) -> None:
    if not isinstance(gs, GameSession) or not _is_whole(gs.id):
        raise AccountError(_NO_OPEN_GAME)


def _open_session(session: Caller, conn: sqlite3.Connection, gs: GameSession) -> sqlite3.Row:
    row = storage.get_session(conn, session.user_id, gs.id)
    if row is None:
        raise _NotYours
    if row["ended_at"] is not None:
        raise AccountError("this game has already been cashed out")
    return row


def _playable_spec(conn: sqlite3.Connection, game_id: object) -> GameSpec:
    """The rules of a game that can be bought into now. Missing and disabled look the same."""
    row = storage.get_game(conn, game_id) if isinstance(game_id, str) else None
    if row is None or not row["enabled"]:
        raise AccountError("that game is not available")
    return load_spec(row["config_json"])


def _session_spec(conn: sqlite3.Connection, session_row: sqlite3.Row) -> GameSpec:
    """The rules for an open session. Read every spin, from the database.

    Whether the game is still enabled is deliberately not checked here: see
    the note at the top of this section.
    """
    game = storage.get_game(conn, session_row["game_id"])
    if game is None:
        raise storage.StorageError(f"session {session_row['id']} has no game")
    return load_spec(game["config_json"])


def _check_bet(spec: GameSpec, bet: object) -> Money:
    """A bet must be one of the game's tiers. An arbitrary amount could be
    negative, or large enough to mint money on a single win."""
    if not isinstance(bet, Money):
        raise AccountError("choose a bet")
    if not bet.is_positive():
        raise AccountError("a bet must be more than $0.00")
    if bet.cents not in spec.bet_tiers:
        tiers = ", ".join(str(Money(tier)) for tier in spec.bet_tiers)
        raise AccountError(f"the bets on this game are {tiers}")
    return bet


def _check_hold(spec: GameSpec, held: object) -> tuple[int, ...]:
    """The reels to hold, checked against the game's hold rules. Returned sorted."""
    policy = spec.holds
    if not policy.enabled or policy.max_consecutive == 0 or policy.max_reels_held == 0:
        raise AccountError("this game has no holds")
    if not isinstance(held, (list, tuple)) or not held:
        raise AccountError("choose which reels to hold")

    reels: set[int] = set()
    for reel in held:
        if not _is_whole(reel) or not 0 <= reel < spec.reel_count:
            raise AccountError("choose reels that are on this game")
        if reel in reels:
            raise AccountError("each reel can be held only once")
        reels.add(reel)

    if len(reels) > policy.max_reels_held:
        raise AccountError(f"you can hold at most {policy.max_reels_held} reel(s) at a time")
    if len(reels) >= spec.reel_count:
        raise AccountError("at least one reel has to spin")
    return tuple(sorted(reels))


def _hold_chain(
    conn: sqlite3.Connection, spec: GameSpec, session_id: int
) -> tuple[list[int], Money, int]:
    """Where the last spin stopped, the bet the current run of holds started
    from, and how many holds that run has used already.

    All three come from the spins table, which cannot be edited, so none of
    them can be supplied or altered by the caller.
    """
    recent = storage.recent_spins(conn, session_id, spec.holds.max_consecutive + 1)
    if not recent:
        raise AccountError("spin first: there is nothing to hold yet")

    holds_used = 0
    first_spin = None
    for row in recent:
        if row["is_hold_respin"]:
            holds_used = holds_used + 1
        else:
            first_spin = row
            break

    if first_spin is None or holds_used >= spec.holds.max_consecutive:
        raise AccountError("no more holds in a row; spin again first")

    previous_stops = [int(stop) for stop in json.loads(recent[0]["stops_json"])]
    return previous_stops, Money(int(first_spin["bet_cents"])), holds_used


def _holds_left(spec: GameSpec, holds_used: int) -> int:
    policy = spec.holds
    if not policy.enabled or policy.max_reels_held == 0:
        return 0
    return max(policy.max_consecutive - holds_used, 0)


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


def _record_refusal(session: Caller, conn: sqlite3.Connection, detail: str) -> None:
    # A transaction of its own: the one the request ran in was rolled back.
    # Someone else's card or session and one that does not exist are logged,
    # and answered, the same way.
    with storage.transaction(conn):
        audit.write(conn, session.username, Action.ACCESS_DENIED, detail)


def _is_whole(value: object) -> TypeGuard[int]:
    # bool is a subclass of int, and True is not an id.
    return isinstance(value, int) and not isinstance(value, bool)
