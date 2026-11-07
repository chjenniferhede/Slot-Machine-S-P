"""Game credits: buying in, spinning, holds, cashing out, and crash recovery."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import StubSession, make_player

from slotmachine import accounts, audit, rng, storage
from slotmachine.accounts import AccountError, GameSession, SpinOutcome
from slotmachine.audit import Action
from slotmachine.game import GameSpec, SpinResult, evaluate
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import load_shipped_specs

CLASSIC = "classic_sevens"  # bets $0.25 / $1 / $5, holds: one reel, once, for two bets
DELUXE = "five_line_deluxe"  # bets $1 / $5 / $25, no holds


@pytest.fixture()
def games(conn: sqlite3.Connection) -> dict[str, GameSpec]:
    """Both shipped games, installed and enabled."""
    specs = {}
    for spec in load_shipped_specs():
        storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
        specs[spec.id] = spec
    return specs


def credits(conn: sqlite3.Connection, gs: GameSession) -> Money:
    row = conn.execute("SELECT credits_cents FROM sessions WHERE id = ?", (gs.id,)).fetchone()
    return Money(int(row["credits_cents"]))


def balance(conn: sqlite3.Connection, user: StubSession) -> Money:
    return storage.get_balance(conn, user.user_id)


def spin_rows(conn: sqlite3.Connection, gs: GameSession) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM spins WHERE session_id = ? ORDER BY id", (gs.id,)))


def fixed_seeds(*seeds: bytes) -> Callable[[], bytes]:
    """A stand-in for rng.secure_seed that hands out these seeds in order."""
    remaining = iter(seeds)
    return lambda: next(remaining)


def seed_where(spec: GameSpec, wanted: Callable[[SpinResult], bool]) -> bytes:
    """The first counting seed whose spin satisfies ``wanted``."""
    for number in range(100_000):
        seed = number.to_bytes(rng.SEED_BYTES, "big")
        if wanted(evaluate(spec, rng.derive_stops(seed, spec.strip_lengths()), 100)):
            return seed
    raise AssertionError("no such seed")


# ---------------------------------------------------------------------------
# Buying in
# ---------------------------------------------------------------------------


def test_buyin_moves_money(conn: sqlite3.Connection, alice: StubSession, games: object) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))

    assert balance(conn, alice) == Money(15_000)
    assert credits(conn, gs) == Money(5_000)
    assert accounts.get_credits(alice, conn, gs) == Money(5_000)
    assert gs.game_id == CLASSIC

    [entry] = audit.read(conn, action=Action.SESSION_BUYIN)
    assert entry["detail"] == f"session={gs.id} game={CLASSIC} amount=$50.00 balance=$150.00"


def test_buyin_over_balance_rejected(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    with pytest.raises(AccountError, match=r"only \$200\.00"):
        accounts.start_session(alice, conn, CLASSIC, Money(20_001))

    assert balance(conn, alice) == Money(20_000)
    assert storage.get_open_session(conn, alice.user_id) is None


@pytest.mark.parametrize("buyin", [Money(0), Money(-5_000), 5_000, None])
def test_buyin_must_be_positive_money(
    conn: sqlite3.Connection, alice: StubSession, games: object, buyin: object
) -> None:
    with pytest.raises(AccountError, match="more than"):
        accounts.start_session(alice, conn, CLASSIC, buyin)  # type: ignore[arg-type]
    assert balance(conn, alice) == Money(20_000)


def test_buyin_must_cover_the_smallest_bet(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    with pytest.raises(AccountError, match=r"at least \$1\.00"):
        accounts.start_session(alice, conn, DELUXE, Money(50))


@pytest.mark.parametrize("game_id", ["no_such_game", 123, None, CLASSIC])
def test_unavailable_game_refused(
    conn: sqlite3.Connection, alice: StubSession, games: object, game_id: object
) -> None:
    """Unknown and disabled games get the same answer."""
    storage.set_game_enabled(conn, CLASSIC, False)
    with pytest.raises(AccountError, match="not available"):
        accounts.start_session(alice, conn, game_id, Money(1_000))  # type: ignore[arg-type]
    assert balance(conn, alice) == Money(20_000)


def test_staff_cannot_buy_in(conn: sqlite3.Connection, games: object) -> None:
    with storage.transaction(conn):
        uid = storage.create_user(conn, "tech", "not-a-real-hash", "technician")
    tech = StubSession(uid, "tech", role="technician")

    with pytest.raises(AccountError, match="no account"):
        accounts.start_session(tech, conn, CLASSIC, Money(1_000))


def test_one_session_per_user(conn: sqlite3.Connection, alice: StubSession, games: object) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))

    with pytest.raises(AccountError, match="already have a game open"):
        accounts.start_session(alice, conn, DELUXE, Money(5_000))
    assert balance(conn, alice) == Money(15_000)

    accounts.cash_out(alice, conn, gs)
    accounts.start_session(alice, conn, DELUXE, Money(5_000))


def test_one_open_session_is_enforced_by_the_schema(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    """Even code that skips the check in start_session cannot open a second."""
    storage.create_session(conn, alice.user_id, CLASSIC, Money(100))
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        storage.create_session(conn, alice.user_id, DELUXE, Money(100))


# ---------------------------------------------------------------------------
# Bets
# ---------------------------------------------------------------------------


def test_bet_must_be_a_tier(conn: sqlite3.Connection, alice: StubSession, games: object) -> None:
    """$3.00 is refused when the tiers are $1 / $5 / $25."""
    gs = accounts.start_session(alice, conn, DELUXE, Money(5_000))

    with pytest.raises(AccountError, match=r"\$1\.00, \$5\.00, \$25\.00"):
        accounts.spin(alice, conn, gs, Money(300))

    assert credits(conn, gs) == Money(5_000)
    assert spin_rows(conn, gs) == []


@pytest.mark.parametrize(
    ("bet", "message"),
    [(Money(-100), "more than"), (Money(0), "more than"), (100, "choose a bet"), (None, "choose a bet")],
)
def test_negative_bet_rejected(
    conn: sqlite3.Connection, alice: StubSession, games: object, bet: object, message: str
) -> None:
    gs = accounts.start_session(alice, conn, DELUXE, Money(5_000))
    with pytest.raises(AccountError, match=message):
        accounts.spin(alice, conn, gs, bet)  # type: ignore[arg-type]
    assert credits(conn, gs) == Money(5_000)


def test_bet_over_credits_rejected(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, DELUXE, Money(100))

    with pytest.raises(AccountError, match=r"costs \$5\.00 and you have \$1\.00"):
        accounts.spin(alice, conn, gs, Money(500))

    assert credits(conn, gs) == Money(100)
    assert spin_rows(conn, gs) == []


# ---------------------------------------------------------------------------
# Spins
# ---------------------------------------------------------------------------


def test_spin_moves_credits_and_records_it(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    outcome = accounts.spin(alice, conn, gs, Money(100))

    assert outcome.charged == Money(100)
    assert outcome.credits == Money(5_000) - Money(100) + outcome.payout
    assert credits(conn, gs) == outcome.credits
    assert outcome.held == ()

    [row] = spin_rows(conn, gs)
    assert row["id"] == outcome.spin_id
    assert (row["bet_cents"], row["payout_cents"]) == (100, outcome.payout.cents)
    assert (row["is_hold_respin"], row["is_test"], row["user_id"]) == (0, 0, alice.user_id)
    assert isinstance(row["seed"], bytes) and len(row["seed"]) == rng.SEED_BYTES

    [entry] = audit.read(conn, action=Action.SPIN)
    assert f"spin={outcome.spin_id} bet=$1.00" in entry["detail"]
    assert row["seed"].hex() not in entry["detail"]  # seeds are for commissioners only


def test_spin_replays_from_its_seed(
    conn: sqlite3.Connection, alice: StubSession, games: dict[str, GameSpec]
) -> None:
    """A recorded seed reproduces the spin, which is how a commissioner checks one."""
    spec = games[DELUXE]
    gs = accounts.start_session(alice, conn, DELUXE, Money(5_000))
    for _ in range(20):
        accounts.spin(alice, conn, gs, Money(100))

    for row in spin_rows(conn, gs):
        stops = rng.derive_stops(row["seed"], spec.strip_lengths())
        assert stops == json.loads(row["stops_json"])
        assert evaluate(spec, stops, row["bet_cents"]).total_payout_cents == row["payout_cents"]


def test_outcome_says_whether_a_hold_is_possible(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    classic = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    outcome = accounts.spin(alice, conn, classic, Money(100))
    assert (outcome.holds_left, outcome.respin_cost) == (1, Money(200))
    accounts.cash_out(alice, conn, classic)

    deluxe = accounts.start_session(alice, conn, DELUXE, Money(5_000))
    outcome = accounts.spin(alice, conn, deluxe, Money(100))
    assert (outcome.holds_left, outcome.respin_cost) == (0, None)


# ---------------------------------------------------------------------------
# Holds
# ---------------------------------------------------------------------------


def test_hold_keeps_the_held_reel_and_charges_two_bets(
    conn: sqlite3.Connection,
    alice: StubSession,
    games: dict[str, GameSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = games[CLASSIC]
    first_seed = (1).to_bytes(rng.SEED_BYTES, "big")
    second_seed = (2).to_bytes(rng.SEED_BYTES, "big")
    monkeypatch.setattr(rng, "secure_seed", fixed_seeds(first_seed, second_seed))

    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    first = accounts.spin(alice, conn, gs, Money(100))
    second = accounts.hold_and_respin(alice, conn, gs, [0])

    # Reel 0 stays put; the others are what the new seed draws.
    drawn = rng.derive_stops(second_seed, spec.strip_lengths())
    assert second.result.stops == (first.result.stops[0], drawn[1], drawn[2])
    assert second.held == (0,)

    # Two bets to re-spin; paid on the original one-dollar bet.
    assert second.charged == Money(200)
    expected = evaluate(spec, list(second.result.stops), 100).total_payout_cents
    assert second.payout == Money(expected)
    assert second.credits == first.credits - Money(200) + second.payout

    respin = spin_rows(conn, gs)[-1]
    assert (respin["bet_cents"], respin["is_hold_respin"]) == (200, 1)
    assert audit.read(conn, action=Action.SPIN)[0]["detail"].endswith(" held=0")
    assert (second.holds_left, second.respin_cost) == (0, None)


def test_hold_limit_enforced(conn: sqlite3.Connection, alice: StubSession, games: object) -> None:
    """One hold in a row on Classic Sevens. A fresh spin earns the next one."""
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    accounts.spin(alice, conn, gs, Money(100))
    accounts.hold_and_respin(alice, conn, gs, [1])

    before = credits(conn, gs)
    with pytest.raises(AccountError, match="no more holds"):
        accounts.hold_and_respin(alice, conn, gs, [1])
    assert credits(conn, gs) == before

    accounts.spin(alice, conn, gs, Money(100))
    accounts.hold_and_respin(alice, conn, gs, [2])


def test_hold_is_priced_on_the_bet_it_started_from(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    accounts.spin(alice, conn, gs, Money(25))
    assert accounts.hold_and_respin(alice, conn, gs, [0]).charged == Money(50)


def test_hold_needs_a_spin_first(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    with pytest.raises(AccountError, match="spin first"):
        accounts.hold_and_respin(alice, conn, gs, [0])


@pytest.mark.parametrize(
    ("held", "message"),
    [
        ([0, 1], "at most 1 reel"),  # holding two would chase three of a kind
        ([3], "on this game"),
        ([-1], "on this game"),
        (["0"], "on this game"),
        ([True], "on this game"),
        ([0, 0], "only once"),
        ([], "which reels"),
        ("0", "which reels"),
        (None, "which reels"),
    ],
)
def test_hold_rules(
    conn: sqlite3.Connection, alice: StubSession, games: object, held: object, message: str
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    accounts.spin(alice, conn, gs, Money(100))
    before = credits(conn, gs)

    with pytest.raises(AccountError, match=message):
        accounts.hold_and_respin(alice, conn, gs, held)  # type: ignore[arg-type]

    assert credits(conn, gs) == before
    assert len(spin_rows(conn, gs)) == 1


def test_no_holds_on_five_line_deluxe(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, DELUXE, Money(5_000))
    accounts.spin(alice, conn, gs, Money(100))
    with pytest.raises(AccountError, match="no holds"):
        accounts.hold_and_respin(alice, conn, gs, [0])


def test_hold_needs_credits_for_the_respin(
    conn: sqlite3.Connection,
    alice: StubSession,
    games: dict[str, GameSpec],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    losing = seed_where(games[CLASSIC], lambda result: result.total_payout_cents == 0)
    monkeypatch.setattr(rng, "secure_seed", fixed_seeds(losing))

    gs = accounts.start_session(alice, conn, CLASSIC, Money(100))
    accounts.spin(alice, conn, gs, Money(100))  # loses: no credits left

    with pytest.raises(AccountError, match=r"costs \$2\.00 and you have \$0\.00"):
        accounts.hold_and_respin(alice, conn, gs, [0])


def test_respin_cost_rounds_up(games: dict[str, GameSpec]) -> None:
    """Never cheaper than the price rtpcalc assumed."""
    classic = games[CLASSIC]
    assert accounts.respin_cost(classic, Money(25)) == Money(50)

    odd_price = dataclasses.replace(
        classic, holds=dataclasses.replace(classic.holds, cost_percent=150)
    )
    assert accounts.respin_cost(odd_price, Money(25)) == Money(38)  # 37.5 rounded up


# ---------------------------------------------------------------------------
# Cashing out
# ---------------------------------------------------------------------------


def test_cashout_returns_credits(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    for _ in range(10):
        accounts.spin(alice, conn, gs, Money(100))
    left = credits(conn, gs)

    result = accounts.cash_out(alice, conn, gs)

    assert result.amount == left
    assert result.net == left - Money(5_000)
    assert result.spins == 10
    assert balance(conn, alice) == Money(15_000) + left

    row = storage.get_session(conn, alice.user_id, gs.id)
    assert row is not None
    assert row["ended_at"] is not None
    assert row["credits_cents"] == 0  # the money is in the account now, not in both places

    [entry] = audit.read(conn, action=Action.SESSION_CASHOUT)
    assert f"amount={left} net={result.net} spins=10" in entry["detail"]


def test_nothing_after_cashing_out(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    accounts.cash_out(alice, conn, gs)

    with pytest.raises(AccountError, match="already been cashed out"):
        accounts.spin(alice, conn, gs, Money(100))
    with pytest.raises(AccountError, match="already been cashed out"):
        accounts.cash_out(alice, conn, gs)
    with pytest.raises(AccountError, match="no open game"):
        accounts.get_credits(alice, conn, gs)
    assert balance(conn, alice) == Money(20_000)


def test_disabled_game_lets_open_sessions_finish(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    storage.set_game_enabled(conn, CLASSIC, False)

    accounts.spin(alice, conn, gs, Money(100))
    accounts.cash_out(alice, conn, gs)

    with pytest.raises(AccountError, match="not available"):
        accounts.start_session(alice, conn, CLASSIC, Money(5_000))


# ---------------------------------------------------------------------------
# Whose session
# ---------------------------------------------------------------------------


def test_cannot_use_other_users_session(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    """IDOR, CWE-639: knowing a session's id must not let you spend or take its credits."""
    bob = make_player(conn, "bob")
    storage.adjust_balance(conn, bob.user_id, Money(10_000))
    bobs = accounts.start_session(bob, conn, CLASSIC, Money(5_000))
    accounts.spin(bob, conn, bobs, Money(100))
    bobs_credits = credits(conn, bobs)

    attempts = [
        lambda gs: accounts.spin(alice, conn, gs, Money(100)),
        lambda gs: accounts.hold_and_respin(alice, conn, gs, [0]),
        lambda gs: accounts.cash_out(alice, conn, gs),
        lambda gs: accounts.get_credits(alice, conn, gs),
    ]
    for attempt in attempts:
        with pytest.raises(AccountError) as theirs:
            attempt(bobs)
        with pytest.raises(AccountError) as missing:
            attempt(GameSession(id=9999, game_id=CLASSIC))
        assert str(theirs.value) == str(missing.value)

    assert credits(conn, bobs) == bobs_credits
    assert balance(conn, alice) == Money(20_000)
    # Spending and cashing out are logged; reading credits is not.
    assert len(audit.read(conn, actor="alice", action=Action.ACCESS_DENIED)) == 6
    assert audit.verify_chain(conn).ok


@pytest.mark.parametrize("handle", [GameSession(id="1", game_id=CLASSIC), "1", 1, None])  # type: ignore[arg-type]
def test_made_up_handle_refused(
    conn: sqlite3.Connection, alice: StubSession, games: object, handle: object
) -> None:
    accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    with pytest.raises(AccountError, match="no open game"):
        accounts.spin(alice, conn, handle, Money(100))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", [RuntimeError("power cut"), KeyboardInterrupt()])
def test_crash_mid_spin_rolls_back(
    conn: sqlite3.Connection,
    alice: StubSession,
    games: object,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """Fail after the bet is taken and before the payout: nothing happened.

    KeyboardInterrupt is the case that matters most. A player who could press
    Ctrl-C after seeing a losing result would be playing with no downside.
    """
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))

    def crash(*args: object, **kwargs: object) -> int:
        raise error

    monkeypatch.setattr(storage, "record_spin", crash)

    with pytest.raises(type(error)):
        accounts.spin(alice, conn, gs, Money(100))

    assert credits(conn, gs) == Money(5_000)
    assert spin_rows(conn, gs) == []
    assert audit.read(conn, action=Action.SPIN) == []


def test_keyboardinterrupt_mid_spin_rolls_back(
    conn: sqlite3.Connection,
    alice: StubSession,
    games: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same, interrupted while the result is being worked out."""
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))

    def interrupted(*args: object, **kwargs: object) -> SpinResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(accounts, "evaluate", interrupted)

    with pytest.raises(KeyboardInterrupt):
        accounts.spin(alice, conn, gs, Money(100))
    assert credits(conn, gs) == Money(5_000)
    assert spin_rows(conn, gs) == []


def test_credits_never_negative(
    conn: sqlite3.Connection, alice: StubSession, games: object
) -> None:
    """Skip accounts.py's check entirely: the schema's CHECK still refuses."""
    gs = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        storage.adjust_credits(conn, gs.id, Money(-5_001))
    assert credits(conn, gs) == Money(5_000)


def test_conservation(conn: sqlite3.Connection, alice: StubSession, games: object) -> None:
    """Over a scripted 1,000-spin session, with every bet tier and holds mixed
    in, the player's money and the casino's take add up exactly, to the cent."""
    storage.adjust_balance(conn, alice.user_id, Money(980_000))  # $10,000.00 in all
    start = balance(conn, alice)
    tiers = [Money(25), Money(100), Money(500)]

    gs = accounts.start_session(alice, conn, CLASSIC, Money(500_000))
    outcome: SpinOutcome | None = None
    for turn in range(1_000):
        if outcome is not None and outcome.holds_left and turn % 4 == 0:
            outcome = accounts.hold_and_respin(alice, conn, gs, [turn % 3])
        else:
            outcome = accounts.spin(alice, conn, gs, tiers[turn % 3])

        if turn % 100 == 0:
            casino_profit = _casino_profit(conn, alice)
            in_play = balance(conn, alice) + credits(conn, gs)
            assert (in_play - start) + casino_profit == Money(0)

    accounts.cash_out(alice, conn, gs)

    account_delta = balance(conn, alice) - start
    credits_delta = credits(conn, gs) - Money(0)
    assert account_delta + credits_delta + _casino_profit(conn, alice) == Money(0)
    assert len(spin_rows(conn, gs)) == 1_000
    assert sum(row["is_hold_respin"] for row in spin_rows(conn, gs)) > 50
    assert audit.verify_chain(conn).ok


def _casino_profit(conn: sqlite3.Connection, user: StubSession) -> Money:
    row = conn.execute(
        "SELECT COALESCE(SUM(bet_cents), 0) - COALESCE(SUM(payout_cents), 0) AS profit"
        " FROM spins WHERE user_id = ?",
        (user.user_id,),
    ).fetchone()
    return Money(int(row["profit"]))


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


def test_orphaned_session_recovered_on_startup(
    conn: sqlite3.Connection, alice: StubSession, games: object, tmp_path: Path
) -> None:
    bob = make_player(conn, "bob")
    storage.adjust_balance(conn, bob.user_id, Money(3_000))

    alices = accounts.start_session(alice, conn, CLASSIC, Money(5_000))
    for _ in range(5):
        accounts.spin(alice, conn, alices, Money(100))
    alices_credits = credits(conn, alices)
    accounts.start_session(bob, conn, DELUXE, Money(2_000))

    # The program dies here. On the next start, a fresh connection recovers.
    restarted = storage.connect(tmp_path / "test.db")
    try:
        assert accounts.recover_open_sessions(restarted) == 2
        assert accounts.recover_open_sessions(restarted) == 0
    finally:
        restarted.close()

    assert balance(conn, alice) == Money(15_000) + alices_credits
    assert balance(conn, bob) == Money(3_000)
    assert storage.get_open_session(conn, alice.user_id) is None
    assert storage.get_open_session(conn, bob.user_id) is None

    recovered = audit.read(conn, action=Action.SESSION_RECOVERED)
    assert [entry["actor"] for entry in recovered] == ["system", "system"]
    assert audit.verify_chain(conn).ok


def test_recovery_on_a_clean_start_does_nothing(conn: sqlite3.Connection) -> None:
    assert accounts.recover_open_sessions(conn) == 0
    assert audit.read(conn) == []
