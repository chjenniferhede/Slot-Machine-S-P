"""Layer 5: the technician, commissioner and player-statistics actions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import make_player

from slotmachine import accounts, audit, auth, stats, storage
from slotmachine.__main__ import install_shipped_games
from slotmachine.audit import Action
from slotmachine.cli import commissioner, technician
from slotmachine.cli.commissioner import TestSessionError
from slotmachine.cli.technician import GameAdminError
from slotmachine.money import Money

SPECS = Path(__file__).resolve().parents[1] / "src" / "slotmachine" / "game" / "specs"


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
        storage.adjust_balance(casino, session.user_id, Money(10_000))
    return session


# ---------------------------------------------------------------------------
# Shipped games
# ---------------------------------------------------------------------------


def test_shipped_games_installed_once(casino: sqlite3.Connection) -> None:
    assert {r["id"] for r in storage.list_games(casino)} == {"classic_sevens", "five_line_deluxe"}
    assert install_shipped_games(casino) == 0


def test_reinstall_keeps_a_disabled_game_disabled(casino: sqlite3.Connection, tech: auth.Session) -> None:
    technician.disable_game(tech, casino, "classic_sevens")
    install_shipped_games(casino)
    assert not storage.get_game(casino, "classic_sevens")["enabled"]


# ---------------------------------------------------------------------------
# Role separation across the three interfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda s, c: technician.enable_game(s, c, "classic_sevens"),
        lambda s, c: technician.run_system_diagnostic(s, c),
        lambda s, c: technician.view_audit_log(s, c),
        lambda s, c: technician.add_staff(s, c, "mallory", auth.Role.TECHNICIAN),
        lambda s, c: commissioner.run_test_session(s, c, "classic_sevens", Money(25), 5),
        lambda s, c: commissioner.view_spin_seeds(s, c, 1),
    ],
)
def test_a_player_is_refused_every_staff_action(casino: sqlite3.Connection, bob: auth.Session, call) -> None:
    with pytest.raises(auth.AccessDenied):
        call(bob, casino)
    assert audit.read(casino, actor="bob", action=Action.ACCESS_DENIED)


def test_technician_cannot_run_tests_or_see_seeds(casino: sqlite3.Connection, tech: auth.Session) -> None:
    with pytest.raises(auth.AccessDenied):
        commissioner.run_test_session(tech, casino, "classic_sevens", Money(25), 5)
    with pytest.raises(auth.AccessDenied):
        commissioner.view_spin_seeds(tech, casino, 1)


def test_commissioner_cannot_manage_games_or_play(casino: sqlite3.Connection, comm: auth.Session) -> None:
    with pytest.raises(auth.AccessDenied):
        technician.disable_game(comm, casino, "classic_sevens")
    with pytest.raises(auth.AccessDenied):
        accounts.start_session(comm, casino, "classic_sevens", Money(100))


# ---------------------------------------------------------------------------
# Technician: games
# ---------------------------------------------------------------------------


def test_disable_and_enable_are_audited(casino: sqlite3.Connection, tech: auth.Session) -> None:
    technician.disable_game(tech, casino, "classic_sevens")
    assert [r["id"] for r in storage.list_games(casino)] == ["five_line_deluxe"]
    technician.enable_game(tech, casino, "classic_sevens")
    assert len(storage.list_games(casino)) == 2
    events = [r["action"] for r in audit.read(casino, actor="tech")]
    assert Action.GAME_DISABLED in events and Action.GAME_ENABLED in events


def test_last_playable_game_cannot_be_disabled(casino: sqlite3.Connection, tech: auth.Session) -> None:
    technician.disable_game(tech, casino, "classic_sevens")
    with pytest.raises(GameAdminError, match="last playable"):
        technician.disable_game(tech, casino, "five_line_deluxe")


def test_disabled_game_refuses_buy_in(casino: sqlite3.Connection, tech: auth.Session, bob: auth.Session) -> None:
    technician.disable_game(tech, casino, "classic_sevens")
    with pytest.raises(accounts.AccountError):
        accounts.start_session(bob, casino, "classic_sevens", Money(100))


def _write_game(tmp_path: Path, **changes: object) -> Path:
    raw = json.loads((SPECS / "classic_sevens.json").read_text())
    raw.update(changes)
    path = tmp_path / "game.json"
    path.write_text(json.dumps(raw))
    return path


def test_install_new_game_starts_disabled(casino: sqlite3.Connection, tech: auth.Session, tmp_path: Path) -> None:
    spec = technician.install_game(tech, casino, str(_write_game(tmp_path, id="lucky_bells", name="Lucky Bells")))
    row = storage.get_game(casino, spec.id)
    assert row is not None and not row["enabled"]
    assert audit.read(casino, action=Action.GAME_INSTALLED, actor="tech")


def test_install_refuses_replacing_an_existing_game(casino: sqlite3.Connection, tech: auth.Session, tmp_path: Path) -> None:
    with pytest.raises(GameAdminError, match="already installed"):
        technician.install_game(tech, casino, str(_write_game(tmp_path)))


def test_install_refuses_a_game_that_pays_over_100(casino: sqlite3.Connection, tech: auth.Session, tmp_path: Path) -> None:
    raw = json.loads((SPECS / "classic_sevens.json").read_text())
    for rule in raw["paytable"]:
        rule["multiplier"] = rule["multiplier"] * 10
    path = _write_game(tmp_path, id="generous", paytable=raw["paytable"])
    with pytest.raises(GameAdminError, match="under 100%"):
        technician.install_game(tech, casino, str(path))
    assert storage.get_game(casino, "generous") is None


@pytest.mark.parametrize(("name", "text", "message"), [
    ("game.txt", "{}", ".json file"),
    ("game.json", "not json", "invalid game config"),
    ("game.json", '{"id": "x"}', "invalid game config"),
])
def test_install_refuses_bad_files(casino, tech, tmp_path, name, text, message) -> None:
    path = tmp_path / name
    path.write_text(text)
    with pytest.raises(GameAdminError, match=message):
        technician.install_game(tech, casino, str(path))


# ---------------------------------------------------------------------------
# Technician: diagnostics, audit, staff
# ---------------------------------------------------------------------------


def test_diagnostic_counts_live_play_only(
    casino: sqlite3.Connection, tech: auth.Session, comm: auth.Session, bob: auth.Session
) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    for _ in range(10):
        accounts.spin(bob, casino, gs, Money(25))
    commissioner.run_test_session(comm, casino, "classic_sevens", Money(25), 50)

    diag = technician.run_system_diagnostic(tech, casino)
    sevens = next(f for f, _ in diag.games if f.game_id == "classic_sevens")
    assert sevens.spins == 10 and sevens.wins + sevens.losses == 10
    assert sevens.wagered == Money(250)
    assert diag.open_sessions == 1 and diag.audit_chain.ok and diag.database_ok


def test_verify_audit_chain_detects_an_edit(casino: sqlite3.Connection, tech: auth.Session) -> None:
    assert technician.verify_audit_chain(tech, casino).ok
    casino.execute("DROP TRIGGER audit_log_is_immutable")
    casino.execute("UPDATE audit_log SET detail = 'nothing happened' WHERE id = 1")
    result = technician.verify_audit_chain(tech, casino)
    assert not result.ok and result.first_bad_id == 1


def test_staff_account_must_change_password(casino: sqlite3.Connection, tech: auth.Session) -> None:
    password = technician.add_staff(tech, casino, "inspector", auth.Role.COMMISSIONER)
    session = auth.login(casino, "inspector", password)
    assert session.role is auth.Role.COMMISSIONER
    assert auth.must_change_password(casino, session)
    with pytest.raises(auth.AccessDenied):
        commissioner.view_game_statistics(session, casino)
    assert password not in json.dumps([dict(r) for r in audit.read(casino, limit=500)])


def test_staff_cannot_be_created_as_player(casino: sqlite3.Connection, tech: auth.Session) -> None:
    with pytest.raises(auth.AuthError):
        technician.add_staff(tech, casino, "sneaky", auth.Role.PLAYER)


# ---------------------------------------------------------------------------
# Commissioner: test mode and replay
# ---------------------------------------------------------------------------


def test_test_session_records_seeds_and_leaves_money_alone(
    casino: sqlite3.Connection, comm: auth.Session, bob: auth.Session
) -> None:
    before = storage.get_balance(casino, bob.user_id)
    report = commissioner.run_test_session(comm, casino, "five_line_deluxe", Money(100), 200)

    assert len(report.rounds) == 200
    assert len({r.seed_hex for r in report.rounds}) == 200
    assert all(len(r.seed_hex) == 64 for r in report.rounds)
    assert report.wagered == Money(20_000)
    assert storage.get_balance(casino, bob.user_id) == before
    rows = casino.execute("SELECT COUNT(*) AS n FROM test_spins").fetchone()
    assert rows["n"] == 200
    assert casino.execute("SELECT COUNT(*) AS n FROM live_spins").fetchone()["n"] == 0
    assert len(audit.read(casino, action=Action.TEST_SESSION)) == 1


@pytest.mark.parametrize(("bet", "rounds"), [(Money(30), 5), (Money(25), 0), (Money(25), 100_001)])
def test_test_session_refuses_bad_requests(casino, comm, bet, rounds) -> None:
    with pytest.raises(TestSessionError):
        commissioner.run_test_session(comm, casino, "classic_sevens", bet, rounds)


def test_replay_verifies_live_spins_and_holds(
    casino: sqlite3.Connection, comm: auth.Session, bob: auth.Session
) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    first = accounts.spin(bob, casino, gs, Money(25))
    held = accounts.hold_and_respin(bob, casino, gs, [0])
    for outcome in (first, held):
        replay = commissioner.view_spin_seeds(comm, casino, outcome.spin_id)
        assert replay.verified
    assert commissioner.view_spin_seeds(comm, casino, held.spin_id).recorded_stops[0] == first.result.stops[0]


def test_replay_catches_a_forged_spin(casino: sqlite3.Connection, comm: auth.Session, bob: auth.Session) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    outcome = accounts.spin(bob, casino, gs, Money(25))
    casino.execute("DROP TRIGGER spins_are_immutable")
    casino.execute("UPDATE spins SET payout_cents = 999999 WHERE id = ?", (outcome.spin_id,))
    assert not commissioner.view_spin_seeds(comm, casino, outcome.spin_id).verified


def test_export_writes_every_round(
    casino: sqlite3.Connection, comm: auth.Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    report = commissioner.run_test_session(comm, casino, "classic_sevens", Money(25), 30)
    path = commissioner.export_test_report(comm, casino, report)
    lines = (tmp_path / path).read_text().splitlines()
    assert len(lines) == 31 and lines[0].startswith("round,spin_id,seed_hex")


def test_commissioner_statistics_separate_live_and_test(
    casino: sqlite3.Connection, comm: auth.Session, bob: auth.Session
) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    accounts.spin(bob, casino, gs, Money(25))
    commissioner.run_test_session(comm, casino, "classic_sevens", Money(25), 40)
    live = {a.figures.game_id: a for a in commissioner.view_game_statistics(comm, casino)}
    test = {a.figures.game_id: a for a in commissioner.view_game_statistics(comm, casino, True)}
    assert live["classic_sevens"].figures.spins == 1
    assert test["classic_sevens"].figures.spins == 40
    assert sum(n for _, n in test["classic_sevens"].distribution.buckets) == 40
    assert 85 < live["classic_sevens"].theory.base_rtp < 100


# ---------------------------------------------------------------------------
# Player statistics
# ---------------------------------------------------------------------------


def test_player_statistics_match_the_spins(casino: sqlite3.Connection, bob: auth.Session) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    outcomes = [accounts.spin(bob, casino, gs, Money(25)) for _ in range(20)]
    s = stats.view_own_statistics(bob, casino)
    assert s.spins == 20 and s.sessions == 1
    assert s.wagered == Money(500)
    assert s.won.cents == sum(o.payout.cents for o in outcomes)
    assert s.wins == sum(1 for o in outcomes if o.payout.is_positive())
    assert s.biggest_payout.cents == max(o.payout.cents for o in outcomes)
    assert s.favourite_game == "Classic Sevens"


def test_player_statistics_are_own_only(casino: sqlite3.Connection, bob: auth.Session) -> None:
    gs = accounts.start_session(bob, casino, "classic_sevens", Money(1_000))
    accounts.spin(bob, casino, gs, Money(25))
    carol = make_player(casino, "carol")
    assert stats.view_own_statistics(carol, casino).spins == 0
