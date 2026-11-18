"""Shipped games are installed once, and an installed game is never overwritten."""

from __future__ import annotations

import sqlite3

from slotmachine import audit, storage
from slotmachine.__main__ import main, startup
from slotmachine.game.catalog import install_shipped_games, load_shipped_specs
from slotmachine.game.spec import load_spec


def test_fresh_database_gets_both_games(conn: sqlite3.Connection) -> None:
    added = install_shipped_games(conn)
    assert sorted(added) == ["classic_sevens", "five_line_deluxe"]
    rows = storage.list_games(conn)
    assert {row["id"] for row in rows} == {"classic_sevens", "five_line_deluxe"}
    for row in rows:
        assert load_spec(row["config_json"]).id == row["id"]
    assert len(audit.read(conn, action=audit.Action.GAME_INSTALLED)) == 2
    assert audit.verify_chain(conn).ok


def test_install_is_idempotent(conn: sqlite3.Connection) -> None:
    install_shipped_games(conn)
    assert install_shipped_games(conn) == []
    assert len(storage.list_games(conn, enabled_only=False)) == 2
    assert len(audit.read(conn, action=audit.Action.GAME_INSTALLED)) == 2


def test_restart_keeps_a_disabled_game_disabled(conn: sqlite3.Connection) -> None:
    install_shipped_games(conn)
    with storage.transaction(conn):
        storage.set_game_enabled(conn, "classic_sevens", False)
    install_shipped_games(conn)
    assert [row["id"] for row in storage.list_games(conn)] == ["five_line_deluxe"]


def test_restart_keeps_installed_rules(conn: sqlite3.Connection) -> None:
    with storage.transaction(conn):
        storage.upsert_game(conn, "classic_sevens", "Renamed", '{"kept": true}')
    install_shipped_games(conn)
    row = storage.get_game(conn, "classic_sevens")
    assert row is not None and row["name"] == "Renamed" and row["config_json"] == '{"kept": true}'


def test_startup_order_leaves_bootstrap_last(conn: sqlite3.Connection) -> None:
    password = startup(conn)
    assert password is not None
    assert audit.read(conn)[0]["action"] == audit.Action.BOOTSTRAP_TECHNICIAN_CREATED
    assert len(storage.list_games(conn)) == len(load_shipped_specs())


def test_init_installs_games(tmp_path, capsys) -> None:
    db = tmp_path / "casino.db"
    assert main(["--db", str(db), "--init"]) == 0
    capsys.readouterr()
    c = storage.connect(db)
    try:
        assert len(storage.list_games(c)) == 2
    finally:
        c.close()
