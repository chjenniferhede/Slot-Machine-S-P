"""Screens every role shares: changing a password, and small display helpers."""

from __future__ import annotations

import getpass
import sqlite3

from slotmachine import auth


class RequestError(ValueError):
    """A staff request was refused. The message is safe to show the user."""


def change_password(conn: sqlite3.Connection, session: auth.Session) -> None:
    """Ask for the current password and a new one, typed twice, all hidden."""
    current = getpass.getpass("Current password: ")
    new = getpass.getpass("New password (at least 12 characters): ")
    if new != getpass.getpass("Confirm new password: "):
        raise auth.AuthError("Passwords do not match.")
    auth.change_password(conn, session, current, new)
    print("Password changed.")


def heading(title: str) -> None:
    """A section title with a rule under it."""
    print()
    print(f"  {title}")
    print("  " + "─" * len(title))


def percent(value: float) -> str:
    return f"{value:.2f}%"
