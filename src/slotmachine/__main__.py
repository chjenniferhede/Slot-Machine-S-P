"""Entry point: database setup, first-run bootstrap, login, and role dispatch.

After login each role gets its own interface, and nothing else:
players go to cli/player.py, technicians to cli/technician.py, commissioners
to cli/commissioner.py. Each of those entry points carries @require_role, so
dispatching to the wrong one is refused by the role gate, not just hidden.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from slotmachine import __version__, accounts, audit, auth, storage
from slotmachine.cli.commissioner import commissioner_menu
from slotmachine.cli.common import change_password as _change_password
from slotmachine.cli.menus import run_action
from slotmachine.cli.player import player_menu
from slotmachine.cli.technician import technician_menu
from slotmachine.game.spec import dump_spec
from slotmachine.tools.rtpcalc import load_shipped_specs


def install_shipped_games(conn: sqlite3.Connection) -> int:
    """Install the bundled games on a new database. Returns how many were added.

    A game that is already installed is left exactly as it is: its rules
    must never change under recorded spins, and a technician may have
    disabled it on purpose.
    """
    added = 0
    with storage.transaction(conn):
        for spec in load_shipped_specs():
            if storage.get_game(conn, spec.id) is None:
                storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec))
                audit.write(conn, "system", audit.Action.GAME_INSTALLED, f"game={spec.id} name={spec.name}")
                added = added + 1
    return added


def _account_menu(conn: sqlite3.Connection, session: auth.Session) -> None:
    # This gate also lives in require_role, protecting direct API calls.
    while auth.must_change_password(conn, session):
        print("You must change your password before continuing.")
        try:
            _change_password(conn, session)
        except auth.AuthError as exc:
            print(str(exc))
    print(f"Signed in as {session.username} ({session.role.value}).")
    interfaces = {
        auth.Role.PLAYER: player_menu,
        auth.Role.TECHNICIAN: technician_menu,
        auth.Role.COMMISSIONER: commissioner_menu,
    }
    interface = interfaces[session.role]
    run_action(lambda: interface(session, conn))
    print(f"Logged out {session.username}.")


def _identity_menu(conn: sqlite3.Connection) -> None:
    while True:
        print()
        print("  BIG CASINO SLOT MACHINE")
        choice = input("1 Log in | 2 Register player | 3 Quit: ").strip()
        if choice == "3":
            return
        try:
            if choice == "1":
                session = auth.login(conn, input("Username: ").strip(), getpass.getpass("Password: "))
                _account_menu(conn, session)
            elif choice == "2":
                username = input("Username: ").strip()
                password = getpass.getpass("Password (at least 12 characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise auth.AuthError("Passwords do not match.")
                auth.register_player(conn, username, password)
                print("Player registered. You can now log in.")
            else:
                print("Choose 1, 2, or 3.")
        except (auth.AuthError, auth.AccessDenied) as exc:
            print(str(exc))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slotmachine", description="Big Casino slot machine")
    parser.add_argument("--version", action="version", version=f"slotmachine {__version__}")
    parser.add_argument("--db", default=storage.DEFAULT_DB_PATH, help="database file (default: casino.db)")
    parser.add_argument("--init", action="store_true", help="initialise database and bootstrap technician, then exit")
    args = parser.parse_args(argv)
    conn: sqlite3.Connection | None = None
    try:
        conn = storage.connect(args.db)
        storage.init_db(conn)
        install_shipped_games(conn)
        accounts.recover_open_sessions(conn)
        password = auth.bootstrap_if_empty(conn)
        print(f"slotmachine {__version__}")
        if password is not None:
            print("Technician account created\n  username: tech-admin")
            print(f"  password: {password}")
            print("This password is shown once. Save it now; you must change it at first login.")
        if args.init:
            print(f"Initialised {args.db}")
            return 0
        _identity_menu(conn)
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye.")
    except (auth.AuthError, auth.AccessDenied) as exc:
        print(str(exc))
        return 1
    except sqlite3.Error:
        print("The database is unavailable. Check its location and permissions.")
        return 1
    finally:
        if conn is not None:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
