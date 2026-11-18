"""What the web client's branch added on top of the Layer 5 interfaces.

Installing a game from pasted JSON with size limits, a technician PAR sheet,
audit paging and date filters, a commissioner game list, a virtual bankroll
with replay verification, richer player statistics, and the player helpers
the browser needs. The role matrix is also checked against the real entry points.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from conftest import make_player

from slotmachine import accounts, audit, auth, stats, storage
from slotmachine.__main__ import install_shipped_games, startup
from slotmachine.audit import Action
from slotmachine.cli import commissioner, technician
from slotmachine.cli.commissioner import TestSessionError
from slotmachine.cli.technician import GameAdminError
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import load_shipped_specs

CLASSIC = "classic_sevens"
DELUXE = "five_line_deluxe"


def _staff(conn: sqlite3.Connection, username: str, role: auth.Role) -> auth.Session:
    with storage.transaction(conn):
        uid = storage.create_user(conn, username, auth.hash_password("x" * 16), role.value)
    return auth.Session(uid, username, role)


@pytest.fixture()
def casino(conn: sqlite3.Connection) -> sqlite3.Connection:
    install_shipped_games(conn)
    return conn


@pytest.fixture()
def tech(casino: sqlite3.Connection) -> auth.Session:
    return _staff(casino, "tech", auth.Role.TECHNICIAN)


@pytest.fixture()
def comm(casino: sqlite3.Connection) -> auth.Session:
    return _staff(casino, "comm", auth.Role.COMMISSIONER)


@pytest.fixture()
def bob(casino: sqlite3.Connection) -> auth.Session:
    session = make_player(casino, "bob")
    with storage.transaction(casino):
        storage.adjust_balance(casino, session.user_id, Money(50_000))
    return session


def _config(**changes: object) -> str:
    raw = json.loads(dump_spec({s.id: s for s in load_shipped_specs()}[CLASSIC]))
    raw.update({"id": "lucky_bells", "name": "Lucky Bells"})
    raw.update(changes)
    return json.dumps(raw)


def _balances(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    return [(int(r[0]), int(r[1])) for r in conn.execute("SELECT user_id, balance_cents FROM accounts ORDER BY user_id")]


# ---------------------------------------------------------------------------
# The role matrix, against the functions that perform each action
# ---------------------------------------------------------------------------


def test_role_matrix_matches_the_real_entry_points() -> None:
    performers = {
        "play_game": [accounts.start_session, accounts.spin, accounts.hold_and_respin, accounts.cash_out],
        "deposit": [accounts.deposit],
        "withdraw": [accounts.withdraw],
        "view_own_statistics": [stats.view_own_statistics],
        "enable_game": [technician.enable_game],
        "disable_game": [technician.disable_game],
        "install_game": [technician.install_game, technician.install_game_config],
        "run_system_diagnostic": [technician.run_system_diagnostic],
        "view_audit_log": [technician.view_audit_log],
        "verify_audit_chain": [technician.verify_audit_chain],
        "create_staff_account": [technician.add_staff, auth.create_staff_account],
        "run_test_session": [commissioner.run_test_session],
        "view_spin_seeds": [commissioner.view_spin_seeds],
        "view_game_statistics": [technician.run_system_diagnostic, commissioner.view_game_statistics],
    }
    assert set(performers) == set(auth.ROLE_MATRIX)
    for action, functions in performers.items():
        allowed: set[auth.Role] = set()
        for fn in functions:
            allowed |= set(fn.__required_roles__)
        assert allowed == set(auth.ROLE_MATRIX[action]), action


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def test_startup_installs_games_before_the_bootstrap(conn: sqlite3.Connection) -> None:
    assert startup(conn) is not None
    assert audit.read(conn, limit=1)[0]["action"] == Action.BOOTSTRAP_TECHNICIAN_CREATED
    assert {g["id"] for g in storage.list_games(conn)} == {CLASSIC, DELUXE}
    assert startup(conn) is None


# ---------------------------------------------------------------------------
# Technician
# ---------------------------------------------------------------------------


def test_install_from_text_starts_disabled(casino, tech) -> None:
    spec = technician.install_game_config(tech, casino, _config())
    row = storage.get_game(casino, spec.id)
    assert row is not None and not row["enabled"]
    [entry] = audit.read(casino, action=Action.GAME_INSTALLED, actor="tech")
    assert entry["detail"] == "game=lucky_bells name=Lucky Bells"
    with pytest.raises(GameAdminError, match="already installed"):
        technician.install_game_config(tech, casino, _config())


def _wide(reels: int) -> str:
    raw = json.loads(_config())
    raw["reels"] = [raw["reels"][0]] * reels
    raw["paylines"] = [[0] * reels]
    raw["holds"]["enabled"] = False
    return json.dumps(raw)


@pytest.mark.parametrize(
    "config,message",
    [
        ("", "paste"),
        ("not json", "invalid game config"),
        ("x" * (technician.MAX_CONFIG_BYTES + 1), "too large"),
        (_wide(6), "at most 5 reels"),
        (_config(bet_tiers=[25, 100, 500, 1000, 2000, 5000, 10000]), "at most 6 bets"),
        (_config(bet_tiers=[25, 200_000]), "at most 6 bets"),
    ],
    ids=["empty", "not-json", "too-long", "too-many-reels", "too-many-bets", "bet-too-big"],
)
def test_install_from_text_refusals(casino, tech, config, message) -> None:
    with pytest.raises(GameAdminError, match=message):
        technician.install_game_config(tech, casino, config)
    assert storage.get_game(casino, "lucky_bells") is None


def test_size_limits_run_before_the_rtp_proof(casino, tech, monkeypatch) -> None:
    """A config too big to prove must be refused without starting the proof."""
    def proof(*args, **kwargs):
        raise AssertionError("the RTP proof ran on an oversized game")

    monkeypatch.setattr(stats, "theoretical", proof)
    with pytest.raises(GameAdminError, match="at most 5 reels"):
        technician.install_game_config(tech, casino, _wide(9))


def test_install_from_file_gets_the_same_limits(casino, tech, tmp_path) -> None:
    path = tmp_path / "wide.json"
    path.write_text(_wide(6), encoding="utf-8")
    with pytest.raises(GameAdminError, match="at most 5 reels"):
        technician.install_game(tech, casino, str(path))


def test_technician_par_sheet(casino, tech) -> None:
    assert "PROBABILITY ACCOUNTING REPORT" in technician.view_par_sheet(tech, casino, CLASSIC)
    with pytest.raises(GameAdminError):
        technician.view_par_sheet(tech, casino, "nope")


def test_audit_paging_and_dates(casino, tech) -> None:
    for _ in range(3):
        technician.disable_game(tech, casino, DELUXE)
        technician.enable_game(tech, casino, DELUXE)
    # Filtered on an event that reading the log does not itself write.
    newest = technician.view_audit_log(tech, casino, "tech", Action.GAME_DISABLED, 2)
    older = technician.view_audit_log(tech, casino, "tech", Action.GAME_DISABLED, 2, offset=2)
    assert len(newest) == 2 and len(older) == 1 and newest[-1]["id"] > older[0]["id"]
    assert technician.view_audit_log(tech, casino, since="2000-01-01", until="2999-12-31")
    assert technician.view_audit_log(tech, casino, until="2000-01-01") == []
    for bad in [{"since": "yesterday"}, {"until": "2026-13"}, {"offset": -1}]:
        with pytest.raises(GameAdminError):
            technician.view_audit_log(tech, casino, **bad)
    for limit in (0, 501, True):
        with pytest.raises(GameAdminError):
            technician.view_audit_log(tech, casino, None, None, limit)


# ---------------------------------------------------------------------------
# Commissioner
# ---------------------------------------------------------------------------


def test_commissioner_lists_every_game(casino, comm, tech) -> None:
    technician.disable_game(tech, casino, DELUXE)
    listed = {spec.id: enabled for spec, enabled in commissioner.list_games(comm, casino)}
    assert listed == {CLASSIC: True, DELUXE: False}


def test_test_session_replays_every_round(casino, comm) -> None:
    report = commissioner.run_test_session(comm, casino, DELUXE, Money(100), 200)
    assert report.replay_ok and report.replay_checked == 200
    assert report.start_bankroll is None and report.end_bankroll is None and not report.stopped_early


def test_bankroll_stops_early_and_moves_no_money(casino, comm, bob) -> None:
    before = _balances(casino)
    report = commissioner.run_test_session(comm, casino, DELUXE, Money(2_500), 100_000, bankroll=Money(5_000))
    assert report.stopped_early and len(report.rounds) < 100_000
    assert report.end_bankroll is not None and report.end_bankroll < Money(2_500)
    assert report.end_bankroll == Money(5_000) - report.wagered + report.paid
    assert report.replay_ok and report.replay_checked == len(report.rounds)
    assert _balances(casino) == before
    assert casino.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    [entry] = audit.read(casino, action=Action.TEST_SESSION)
    assert "bankroll=$50.00" in entry["detail"]


@pytest.mark.parametrize("bankroll", [Money(100), Money(0), commissioner.MAX_BANKROLL + Money(1), 5_000])
def test_bankroll_refusals(casino, comm, bankroll) -> None:
    with pytest.raises(TestSessionError):
        commissioner.run_test_session(comm, casino, CLASSIC, Money(500), 10, bankroll=bankroll)
    assert casino.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0


def test_interrupted_test_session_leaves_nothing(casino, comm, monkeypatch) -> None:
    calls = {"n": 0}
    real = storage.record_spin

    def failing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 50:
            raise KeyboardInterrupt
        return real(*args, **kwargs)

    monkeypatch.setattr(storage, "record_spin", failing)
    with pytest.raises(KeyboardInterrupt):
        commissioner.run_test_session(comm, casino, CLASSIC, Money(25), 100)
    assert casino.execute("SELECT COUNT(*) FROM spins").fetchone()[0] == 0
    assert audit.read(casino, action=Action.TEST_SESSION) == []
    assert not casino.in_transaction


def test_replay_reports_a_forged_test_round(casino, comm) -> None:
    report = commissioner.run_test_session(comm, casino, CLASSIC, Money(25), 5)
    target = report.rounds[2].spin_id
    casino.execute("DROP TRIGGER spins_are_immutable")  # what someone with the file could do
    casino.execute("UPDATE spins SET payout_cents = payout_cents + 100000 WHERE id = ?", (target,))
    assert not commissioner.view_spin_seeds(comm, casino, target).verified


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def _play(conn, player, game, spins, bet):
    gs = accounts.start_session(player, conn, game, Money(bet * spins))
    for _ in range(spins):
        accounts.spin(player, conn, gs, Money(bet))
    accounts.cash_out(player, conn, gs)


def test_player_statistics_break_down_by_game_and_visit(casino, bob, comm) -> None:
    _play(casino, bob, CLASSIC, 6, 25)
    _play(casino, bob, DELUXE, 3, 100)
    commissioner.run_test_session(comm, casino, CLASSIC, Money(25), 50)  # never counted for bob
    s = stats.view_own_statistics(bob, casino)
    assert s.spins == 9 and s.sessions == 2
    assert {g.game_id: g.spins for g in s.by_game} == {CLASSIC: 6, DELUXE: 3}
    assert [r.spins for r in s.recent_sessions] == [3, 6]
    assert all(r.ended_at is not None for r in s.recent_sessions)
    assert sum((r.net for r in s.recent_sessions), Money.zero()) == s.net


def test_player_helpers(casino, bob, tech) -> None:
    assert {spec.id for spec in accounts.list_playable_games(bob, casino)} == {CLASSIC, DELUXE}
    assert accounts.current_game(bob, casino) is None
    assert accounts.current_game_rules(bob, casino) is None
    gs = accounts.start_session(bob, casino, DELUXE, Money(500))
    assert accounts.current_game(bob, casino) == gs
    technician.disable_game(tech, casino, DELUXE)
    rules = accounts.current_game_rules(bob, casino)
    assert rules is not None and rules.id == DELUXE  # still playable to the end
    assert [spec.id for spec in accounts.list_playable_games(bob, casino)] == [CLASSIC]
    accounts.cash_out(bob, casino, gs)
    assert accounts.current_game(bob, casino) is None


def test_other_players_games_are_invisible(casino, bob) -> None:
    alice = make_player(casino, "alice")
    with storage.transaction(casino):
        storage.adjust_balance(casino, alice.user_id, Money(1_000))
    accounts.start_session(bob, casino, CLASSIC, Money(500))
    assert accounts.current_game(alice, casino) is None


# ---------------------------------------------------------------------------
# The terminal screens that changed
# ---------------------------------------------------------------------------


def test_terminal_test_session_with_bankroll(casino, comm, keyboard, capsys) -> None:
    # Game 1 (Classic Sevens, listed by name), bet 3 ($5.00), 50 rounds,
    # a $10.00 bankroll, no CSV.
    keyboard.type("1", "3", "50", "y", "10.00", "n")
    commissioner._test_game(comm, casino)
    out = capsys.readouterr().out
    assert "Replay verified" in out
    assert "Virtual bankroll" in out and "$10.00 ->" in out
    [entry] = audit.read(casino, action=Action.TEST_SESSION)
    assert "bankroll=$10.00" in entry["detail"]


def test_terminal_par_sheet_for_technicians(casino, tech, keyboard, capsys) -> None:
    keyboard.type(CLASSIC)
    technician._show_par(tech, casino)
    assert "PROBABILITY ACCOUNTING REPORT -- Classic Sevens" in capsys.readouterr().out
