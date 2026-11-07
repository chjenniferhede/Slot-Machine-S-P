"""Database bootstrap and identity CLI; game and staff tools arrive in Layer 5."""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from slotmachine import __version__, accounts, auth, storage


def _change_password(conn: sqlite3.Connection, session: auth.Session) -> None:
    current = getpass.getpass("Current password: ")
    new = getpass.getpass("New password (at least 12 characters): ")
    if new != getpass.getpass("Confirm new password: "):
        raise auth.AuthError("Passwords do not match.")
    auth.change_password(conn, session, current, new)
    print("Password changed.")


def _account_menu(conn: sqlite3.Connection, session: auth.Session) -> None:
    # This gate also lives in require_role, protecting direct API calls.
    while auth.must_change_password(conn, session):
        print("You must change your password before continuing.")
        try:
            _change_password(conn, session)
        except auth.AuthError as exc:
            print(str(exc))
    print(f"Signed in as {session.username} ({session.role.value}).")
    print("Game, payment, and staff tools will be added in Layer 5.")
    while True:
        choice = input("1 Change password | 2 Log out: ").strip()
        if choice == "2":
            return
        if choice == "1":
            try:
                _change_password(conn, session)
            except (auth.AuthError, auth.AccessDenied) as exc:
                print(str(exc))
        else:
            print("Choose 1 or 2.")


def _identity_menu(conn: sqlite3.Connection) -> None:
    while True:
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
