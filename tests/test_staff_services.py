"""Technician and commissioner actions, and the statistics behind every report."""

from __future__ import annotations

import json
import sqlite3

import pytest

from slotmachine import accounts, audit, auth, commissioner, stats, storage, technician
from slotmachine.auth import AccessDenied, Role, Session
from slotmachine.game.catalog import install_shipped_games, load_shipped_specs
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money

from conftest import dump_all_text, make_player

CLASSIC = "classic_sevens"
DELUXE = "five_line_deluxe"


def staff(conn: sqlite3.Connection, username: str, role: Role, must_change: bool = False) -> Session:
    with storage.transaction(conn):
        uid = storage.create_user(conn, username, auth.hash_password("A staff passphrase here"), role.value, must_change)
    return Session(uid, username, role)


@pytest.fixture()
def games(conn: sqlite3.Connection) -> None:
    install_shipped_games(conn)


@pytest.fixture()
def tech(conn: sqlite3.Connection, games: None) -> Session:
    return staff(conn, "tech", Role.TECHNICIAN)


@pytest.fixture()
def comm(conn: sqlite3.Connection, games: None) -> Session:
    return staff(conn, "comm", Role.COMMISSIONER)


def funded_player(conn: sqlite3.Connection, name: str, cents: int = 50_000) -> Session:
    session = make_player(conn, name)
    with storage.transaction(conn):
        storage.adjust_balance(conn, session.user_id, Money(cents))
    return session


def play(conn: sqlite3.Connection, player: Session, game: str, spins: int, bet: int) -> None:
    gs = accounts.start_session(player, conn, game, Money(bet * spins))
    for _ in range(spins):
        accounts.spin(player, conn, gs, Money(bet))
    accounts.cash_out(player, conn, gs)


def balances(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    return [(int(r["user_id"]), int(r["balance_cents"])) for r in conn.execute("SELECT * FROM accounts ORDER BY user_id")]


# ---------------------------------------------------------------------------
# Who may call what
# ---------------------------------------------------------------------------

TECH_CALLS = [
    ("list_games", (), {}),
    ("set_game_enabled", (CLASSIC, False), {}),
    ("install_game", ("{}",), {}),
    ("par_sheet", (CLASSIC,), {}),
    ("system_diagnostic", (), {}),
    ("read_audit_log", (), {}),
    ("verify_audit_chain", (), {}),
    ("list_staff", (), {}),
    ("create_staff_account", ("newcomm", Role.COMMISSIONER), {}),
]
COMM_CALLS = [
    ("list_games", (), {}),
    ("analyze_game", (CLASSIC,), {}),
    ("par_sheet", (CLASSIC,), {}),
    ("run_test_session", (CLASSIC, 5, Money(25), Money(1_000)), {}),
]


@pytest.mark.parametrize("name,args,kwargs", TECH_CALLS, ids=[c[0] for c in TECH_CALLS])
@pytest.mark.parametrize("who", ["anonymous", "player", "commissioner", "forced-technician"])
def test_technician_actions_refuse_everyone_else(conn, games, name, args, kwargs, who):
    caller = {
        "anonymous": lambda: None,
        "player": lambda: make_player(conn, "pat"),
        "commissioner": lambda: staff(conn, "comm", Role.COMMISSIONER),
        "forced-technician": lambda: staff(conn, "newtech", Role.TECHNICIAN, must_change=True),
    }[who]()
    before = dump_all_text(conn).count("game.")
    with pytest.raises(AccessDenied):
        getattr(technician, name)(caller, conn, *args, **kwargs)
    assert audit.read(conn, limit=1)[0]["action"] == audit.Action.ACCESS_DENIED
    assert dump_all_text(conn).count("game.") == before


@pytest.mark.parametrize("name,args,kwargs", COMM_CALLS, ids=[c[0] for c in COMM_CALLS])
@pytest.mark.parametrize("who", ["anonymous", "player", "technician", "forced-commissioner"])
def test_commissioner_actions_refuse_everyone_else(conn, games, name, args, kwargs, who):
    caller = {
        "anonymous": lambda: None,
        "player": lambda: make_player(conn, "pat"),
        "technician": lambda: staff(conn, "tech", Role.TECHNICIAN),
        "forced-commissioner": lambda: staff(conn, "newcomm", Role.COMMISSIONER, must_change=True),
    }[who]()
    with pytest.raises(AccessDenied):
        getattr(commissioner, name)(caller, conn, *args, **kwargs)
    assert conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0


def test_role_matrix_matches_the_real_entry_points():
    """Each action in the matrix is enforced by the functions that perform it."""
    performers = {
        "play_game": [accounts.start_session, accounts.spin, accounts.hold_and_respin, accounts.cash_out],
        "deposit": [accounts.deposit],
        "withdraw": [accounts.withdraw],
        "view_own_statistics": [accounts.own_statistics],
        "enable_game": [technician.set_game_enabled],
        "disable_game": [technician.set_game_enabled],
        "install_game": [technician.install_game],
        "run_system_diagnostic": [technician.system_diagnostic],
        "view_audit_log": [technician.read_audit_log],
        "verify_audit_chain": [technician.verify_audit_chain],
        "create_staff_account": [technician.create_staff_account],
        "run_test_session": [commissioner.run_test_session],
        "view_spin_seeds": [commissioner.run_test_session],
        "view_game_statistics": [technician.system_diagnostic, commissioner.analyze_game],
    }
    assert set(performers) == set(auth.ROLE_MATRIX)
    for action, functions in performers.items():
        allowed: set[Role] = set()
        for fn in functions:
            allowed |= set(fn.__required_roles__)
        assert allowed == set(auth.ROLE_MATRIX[action]), action


# ---------------------------------------------------------------------------
# Technician
# ---------------------------------------------------------------------------


def test_disable_hides_game_from_players_and_is_audited(conn, tech):
    player = funded_player(conn, "pat")
    assert technician.set_game_enabled(tech, conn, CLASSIC, False) is True
    assert technician.set_game_enabled(tech, conn, CLASSIC, False) is False
    assert [s.id for s in accounts.list_playable_games(player, conn)] == [DELUXE]
    with pytest.raises(accounts.AccountError):
        accounts.start_session(player, conn, CLASSIC, Money(100))
    [entry] = audit.read(conn, action=audit.Action.GAME_DISABLED)
    assert entry["actor"] == "tech" and entry["detail"] == f"game={CLASSIC}"
    technician.set_game_enabled(tech, conn, CLASSIC, True)
    assert len(accounts.list_playable_games(player, conn)) == 2


def test_disabled_game_keeps_its_statistics(conn, tech):
    player = funded_player(conn, "pat")
    play(conn, player, CLASSIC, 5, 25)
    technician.set_game_enabled(tech, conn, CLASSIC, False)
    [classic] = [g for g in technician.system_diagnostic(tech, conn) if g.game_id == CLASSIC]
    assert classic.spins == 5 and not classic.enabled


def test_unknown_game_is_refused(conn, tech):
    with pytest.raises(technician.TechnicianError):
        technician.set_game_enabled(tech, conn, "nope", True)
    with pytest.raises(technician.TechnicianError):
        technician.par_sheet(tech, conn, "nope")


def _custom_game(**changes: object) -> str:
    raw = json.loads(dump_spec(load_shipped_specs()[0]))
    raw.update({"id": "lucky_test", "name": "Lucky Test"})
    raw.update(changes)
    return json.dumps(raw)


def test_install_game_starts_disabled(conn, tech):
    spec = technician.install_game(tech, conn, _custom_game())
    row = storage.get_game(conn, spec.id)
    assert row is not None and not row["enabled"]
    assert "source=technician" in audit.read(conn, action=audit.Action.GAME_INSTALLED)[0]["detail"]
    with pytest.raises(technician.TechnicianError, match="already installed"):
        technician.install_game(tech, conn, _custom_game())


@pytest.mark.parametrize(
    "config",
    [
        "not json",
        "[]",
        '{"id": "x"}',
        "x" * (technician.MAX_CONFIG_CHARS + 1),
    ],
    ids=["not-json", "not-object", "missing-fields", "too-long"],
)
def test_install_game_rejects_bad_configs(conn, tech, config):
    with pytest.raises(technician.TechnicianError):
        technician.install_game(tech, conn, config)
    assert storage.get_game(conn, "x") is None


def test_install_game_refuses_a_game_without_house_edge(conn, tech):
    generous = [{"symbol": s, "count": 1, "multiplier": 50} for s in ["CHERRY", "PLUM", "BELL", "BAR", "SEVEN"]]
    with pytest.raises(technician.TechnicianError, match="house edge"):
        technician.install_game(tech, conn, _custom_game(paytable=generous))
    assert storage.get_game(conn, "lucky_test") is None


def test_audit_log_filters_and_verify(conn, tech):
    technician.set_game_enabled(tech, conn, CLASSIC, False)
    entries = technician.read_audit_log(tech, conn, actor="tech", action=audit.Action.GAME_DISABLED)
    assert [e.action for e in entries] == [audit.Action.GAME_DISABLED]
    assert technician.read_audit_log(tech, conn, since="2000-01-01", until="2999-12-31", limit=1)
    with pytest.raises(technician.TechnicianError):
        technician.read_audit_log(tech, conn, since="yesterday")
    with pytest.raises(technician.TechnicianError):
        technician.read_audit_log(tech, conn, limit=0)
    assert technician.verify_audit_chain(tech, conn).ok


def test_verify_reports_tampering(conn, tech):
    technician.set_game_enabled(tech, conn, CLASSIC, False)
    target = audit.read(conn, action=audit.Action.GAME_DISABLED)[0]["id"]
    conn.execute("DROP TRIGGER audit_log_is_immutable")  # what an attacker with the file would do
    conn.execute("UPDATE audit_log SET detail = 'game=nothing' WHERE id = ?", (target,))
    result = technician.verify_audit_chain(tech, conn)
    assert not result.ok and result.first_bad_id == target


def test_create_staff_account(conn, tech):
    new = technician.create_staff_account(tech, conn, "regulator", Role.COMMISSIONER)
    assert new.one_time_password not in repr(new)
    assert new.one_time_password not in dump_all_text(conn)
    row = storage.get_user_by_username(conn, "regulator")
    assert row is not None and row["role"] == "commissioner" and row["must_change_pw"] == 1
    assert storage.get_account(conn, int(row["id"])) is None  # staff never get a wallet
    session = auth.login(conn, "regulator", new.one_time_password)
    with pytest.raises(AccessDenied):
        commissioner.list_games(session, conn)  # until the password is changed
    auth.change_password(conn, session, new.one_time_password, "A brand new regulator phrase")
    assert commissioner.list_games(session, conn)
    assert [s.username for s in technician.list_staff(tech, conn)] == ["regulator", "tech"]
    [entry] = audit.read(conn, action=audit.Action.USER_STAFF_CREATED)
    assert entry["detail"] == "user=regulator role=commissioner"


@pytest.mark.parametrize(
    "username,role",
    [("bad name", Role.COMMISSIONER), ("tech", Role.TECHNICIAN), ("someone", Role.PLAYER), ("x", "commissioner")],
    ids=["bad-username", "taken", "player-role", "role-as-string"],
)
def test_create_staff_account_refusals(conn, tech, username, role):
    with pytest.raises(technician.TechnicianError):
        technician.create_staff_account(tech, conn, username, role)


# ---------------------------------------------------------------------------
# Commissioner
# ---------------------------------------------------------------------------


def test_test_session_moves_no_money_and_shows_seeds(conn, comm):
    player = funded_player(conn, "pat")
    play(conn, player, CLASSIC, 3, 25)
    before = balances(conn)
    sessions_before = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    report = commissioner.run_test_session(comm, conn, CLASSIC, 1_000, Money(25), Money(1_000_000))

    assert balances(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions_before
    assert report.rounds_played == 1_000 and len(report.rounds) == 1_000
    assert all(len(r.seed_hex) == 64 for r in report.rounds)
    assert len({r.seed_hex for r in report.rounds}) == 1_000
    assert report.replay_ok and report.replay_checked == 1_000
    flags = conn.execute(
        "SELECT DISTINCT is_test FROM spins WHERE id BETWEEN ? AND ?",
        (report.first_spin_id, report.last_spin_id),
    ).fetchall()
    assert [r[0] for r in flags] == [1]
    assert report.total_bet == Money(25 * 1_000)
    assert report.end_bankroll == Money(1_000_000) - report.total_bet + report.total_paid
    starts = audit.read(conn, action=audit.Action.TEST_SESSION)
    assert len(starts) == 2 and starts[1]["detail"].startswith("start")

    # Test rounds stay out of the player's and the casino's numbers.
    assert accounts.own_statistics(player, conn).spins == 3
    live = {g.game_id: g for g in stats.game_stats(conn)}
    assert live[CLASSIC].spins == 3
    tested = commissioner.analyze_game(comm, conn, CLASSIC, test=True)
    assert tested.stats.spins == 1_000


def test_test_session_stops_when_bankroll_runs_out(conn, comm):
    report = commissioner.run_test_session(comm, conn, DELUXE, 100_000, Money(2_500), Money(2_500))
    assert report.rounds_played >= 1
    assert report.end_bankroll < Money(2_500) or report.rounds_played == 100_000
    assert report.stopped_early == (report.rounds_played < 100_000)


def test_test_session_works_on_a_disabled_game(conn, comm, tech):
    technician.set_game_enabled(tech, conn, CLASSIC, False)
    report = commissioner.run_test_session(comm, conn, CLASSIC, 10, Money(100), Money(10_000))
    assert report.rounds_played == 10


@pytest.mark.parametrize(
    "game,rounds,bet,bankroll",
    [
        ("nope", 10, Money(25), Money(1_000)),
        (CLASSIC, 0, Money(25), Money(1_000)),
        (CLASSIC, commissioner.MAX_ROUNDS + 1, Money(25), Money(1_000)),
        (CLASSIC, True, Money(25), Money(1_000)),
        (CLASSIC, 10, Money(30), Money(1_000)),
        (CLASSIC, 10, 25, Money(1_000)),
        (CLASSIC, 10, Money(500), Money(100)),
        (CLASSIC, 10, Money(25), commissioner.MAX_BANKROLL + Money(1)),
    ],
    ids=["unknown-game", "zero-rounds", "too-many", "bool-rounds", "off-tier", "int-bet", "short-bankroll", "huge-bankroll"],
)
def test_test_session_refusals(conn, comm, game, rounds, bet, bankroll):
    with pytest.raises(commissioner.CommissionerError):
        commissioner.run_test_session(comm, conn, game, rounds, bet, bankroll)
    assert conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0


def test_interrupted_test_session_leaves_nothing(conn, comm, monkeypatch):
    calls = {"n": 0}
    real = storage.record_spin

    def failing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 50:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(storage, "record_spin", failing)
    with pytest.raises(KeyboardInterrupt):
        commissioner.run_test_session(comm, conn, CLASSIC, 100, Money(25), Money(10_000))
    assert conn.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0
    assert audit.read(conn, action=audit.Action.TEST_SESSION) == []
    assert not conn.in_transaction


def test_analysis_histogram_and_math(conn, comm):
    player = funded_player(conn, "pat", 100_000)
    play(conn, player, CLASSIC, 200, 25)
    analysis = commissioner.analyze_game(comm, conn, CLASSIC)
    s = analysis.stats
    assert s.wins + s.losses == s.spins == 200
    assert sum(count for _, count in analysis.histogram) == 200
    assert dict(analysis.histogram)["0x"] == s.losses
    assert s.wagered == Money(25 * 200)
    assert s.house_net == s.wagered - s.paid
    assert s.unique_players == 1 and s.active_days == 1 and s.plays_per_day == 200
    assert s.theoretical_rtp is not None and 0.9 < s.theoretical_rtp < 1
    assert analysis.return_stdev is not None and analysis.return_stdev >= 0


# ---------------------------------------------------------------------------
# Player statistics
# ---------------------------------------------------------------------------


def test_player_statistics_are_their_own(conn, games):
    alice = funded_player(conn, "alice")
    bob = funded_player(conn, "bob")
    play(conn, alice, CLASSIC, 10, 25)
    play(conn, bob, DELUXE, 4, 100)
    mine = accounts.own_statistics(alice, conn)
    assert mine.spins == 10 and mine.sessions == 1
    assert mine.wagered == Money(250)
    assert [g.game_id for g in mine.by_game] == [CLASSIC]
    assert mine.net == mine.won - mine.wagered
    assert mine.recent_sessions[0].spins == 10 and mine.recent_sessions[0].ended_at is not None
    assert accounts.own_statistics(bob, conn).spins == 4


def test_empty_statistics(conn, games):
    fresh = funded_player(conn, "fresh")
    summary = accounts.own_statistics(fresh, conn)
    assert summary.spins == 0 and summary.win_rate is None and summary.by_game == ()
    assert all(g.observed_rtp is None and g.hit_rate is None for g in stats.game_stats(conn))


def test_current_game(conn, games):
    player = funded_player(conn, "pat")
    assert accounts.current_game(player, conn) is None
    gs = accounts.start_session(player, conn, CLASSIC, Money(100))
    assert accounts.current_game(player, conn) == gs
    accounts.cash_out(player, conn, gs)
    assert accounts.current_game(player, conn) is None
