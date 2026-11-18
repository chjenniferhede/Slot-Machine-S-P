"""The games that ship with the system, and putting them in the database.

A fresh database has an empty ``games`` table, and a player cannot buy into
a game that is not there. ``install_shipped_games`` runs at every startup and
adds any shipped game that is missing. It never touches a game that is already
installed: a technician may have disabled it, and restarting the program must
not quietly turn it back on or swap its rules.
"""

from __future__ import annotations

import sqlite3
from importlib import resources
from typing import Final

from slotmachine import audit, storage
from slotmachine.game.spec import GameSpec, dump_spec, load_spec

SHIPPED_SPECS: Final[tuple[str, ...]] = ("classic_sevens.json", "five_line_deluxe.json")


def load_shipped_specs() -> list[GameSpec]:
    """Load the games that ship with the system, checked like any other config."""
    specs = []
    for filename in SHIPPED_SPECS:
        spec_file = resources.files("slotmachine.game.specs").joinpath(filename)
        text = spec_file.read_text(encoding="utf-8")
        specs.append(load_spec(text))
    return specs


def install_shipped_games(conn: sqlite3.Connection) -> list[str]:
    """Add each shipped game that is not installed yet. Returns the ids added.

    One transaction, with an audit entry per game added, so a game is never
    playable without a record of where it came from.
    """
    added: list[str] = []
    specs = load_shipped_specs()
    with storage.transaction(conn):
        for spec in specs:
            if storage.insert_game_if_missing(conn, spec.id, spec.name, dump_spec(spec)):
                audit.write(conn, "system", audit.Action.GAME_INSTALLED, f"game={spec.id} source=shipped")
                added.append(spec.id)
    return added
