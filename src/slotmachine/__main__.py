"""Entry point: bring the database up, then open the web casino or the identity CLI.

``slotmachine --web`` serves every role's pages on http://127.0.0.1:8888/.
Plain ``slotmachine`` keeps the terminal menus for logging in, registering
and changing passwords. Either way, the first technician's one-time password
is printed here, on the terminal, and never sent to a browser.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys
import webbrowser

from slotmachine import __version__, accounts, auth, storage
from slotmachine.game.catalog import install_shipped_games
from slotmachine.web.server import DEFAULT_PORT, serve


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
    print("Games, payments, and staff tools are in the web casino: run slotmachine --web")
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


def startup(conn: sqlite3.Connection) -> str | None:
    """Bring a database up to date before anyone can use it.

    Schema, then the shipped games, then any session a crash left open, and
    last the first technician. Returns that technician's one-time password
    when one was just created, for the terminal only.
    """
    storage.init_db(conn)
    install_shipped_games(conn)
    accounts.recover_open_sessions(conn)
    return auth.bootstrap_if_empty(conn)


def _serve_web(conn: sqlite3.Connection, port: int, *, open_browser: bool) -> int:
    try:
        server = serve(conn, port)
    except OSError:
        print(f"Port {port} is in use. Close the other program or choose --port.")
        return 1
    with server:
        # Flushed, so a piped or logged stdout shows the one-time password and
        # the address now rather than when the server stops.
        print(f"Casino open at {server.url}  (Ctrl-C to close)", flush=True)
        if open_browser:
            webbrowser.open(server.url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nCasino closed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="slotmachine", description="Big Casino slot machine")
    parser.add_argument("--version", action="version", version=f"slotmachine {__version__}")
    parser.add_argument("--db", default=storage.DEFAULT_DB_PATH, help="database file (default: casino.db)")
    parser.add_argument("--init", action="store_true", help="initialise database and bootstrap technician, then exit")
    parser.add_argument("--web", action="store_true", help="serve the casino in a browser on 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port for --web (default: {DEFAULT_PORT})")
    parser.add_argument("--no-browser", action="store_true", help="with --web, do not open a browser")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    conn: sqlite3.Connection | None = None
    try:
        conn = storage.connect(args.db)
        password = startup(conn)
        print(f"slotmachine {__version__}")
        if password is not None:
            print("Technician account created\n  username: tech-admin")
            print(f"  password: {password}")
            print("This password is shown once. Save it now; you must change it at first login.")
        if args.init:
            print(f"Initialised {args.db}")
            return 0
        if args.web:
            return _serve_web(conn, args.port, open_browser=not args.no_browser)
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
