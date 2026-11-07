"""Identity for the local CLI: Argon2id, account lockout, and one role gate.

Sessions are trusted in-process values, not credentials to deserialize from input.
Auth mutations own their transactions; call them outside another transaction so
failed-login evidence commits before AuthError is raised. The role decorator may
join a caller's transaction, in which case that caller owns commit and rollback.
"""

from __future__ import annotations

import functools
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from types import MappingProxyType
from typing import Concatenate, ParamSpec, TypeVar

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError, InvalidHashError

from slotmachine import audit, storage

_ph = PasswordHasher(
    time_cost=2,
    memory_cost=19456,  # KiB: 19 MiB, not bytes.
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)
MAX_FAILURES = 5
LOCKOUT_MINS = 15
LOGIN_FAILURE_MESSAGE = "Invalid username or password"  # noqa: credential (public error message)
COMMON_PASSWORDS = frozenset({
    "password", "password123", "password1234", "password12345", "password123456",
    "123456789012", "1234567890123", "12345678901234", "qwertyuiop12",
    "qwertyuiop123", "letmeinletmein", "welcome12345", "welcome123456",
    "adminadmin12", "administrator", "iloveyou12345", "changeme12345",
    "correcthorsebatterystaple", "abc123abc123", "111111111111",
})


class AuthError(Exception):
    """An expected authentication or account-management error, safe for the CLI."""


class AccessDenied(Exception):
    """A caller is not allowed to perform an action."""


class Role(str, Enum):
    PLAYER = "player"
    TECHNICIAN = "technician"
    COMMISSIONER = "commissioner"


@dataclass(frozen=True)
class Session:
    """An immutable, authenticated identity for a local CLI session."""

    user_id: int
    username: str
    role: Role


ROLE_MATRIX: Mapping[str, frozenset[Role]] = MappingProxyType({
    "play_game": frozenset({Role.PLAYER}),
    "deposit": frozenset({Role.PLAYER}),
    "withdraw": frozenset({Role.PLAYER}),
    "view_own_statistics": frozenset({Role.PLAYER}),
    "enable_game": frozenset({Role.TECHNICIAN}),
    "disable_game": frozenset({Role.TECHNICIAN}),
    "run_system_diagnostic": frozenset({Role.TECHNICIAN}),
    "view_audit_log": frozenset({Role.TECHNICIAN}),
    "verify_audit_chain": frozenset({Role.TECHNICIAN}),
    "run_test_session": frozenset({Role.COMMISSIONER}),
    "view_game_statistics": frozenset({Role.TECHNICIAN, Role.COMMISSIONER}),
    "view_spin_seeds": frozenset({Role.COMMISSIONER}),
})


def hash_password(password: str) -> str:
    """Hash with a fresh per-password salt; never retain the plaintext."""
    return _ph.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """Return False for mismatches and malformed stored hashes."""
    try:
        return _ph.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


# Random, unusable dummy credential: comparable verification work for unknown users.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def validate_password_strength(pw: str) -> str | None:
    if len(pw) < 12:
        return "Use at least 12 characters."
    if len(pw) > 1024:
        return "Use at most 1024 characters."
    if pw.casefold() in COMMON_PASSWORDS:
        return "Choose a less common password."
    return None


def _validate_username(username: str) -> None:
    if re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username) is None:
        raise AuthError("Use 3–32 letters, digits, dots, hyphens, or underscores for your username.")


def _identity_row(conn: sqlite3.Connection, session: Session | None) -> sqlite3.Row | None:
    if not isinstance(session, Session) or not isinstance(session.role, Role):
        return None
    row = storage.get_user(conn, session.user_id)
    if row is None or row["username"] != session.username or row["role"] != session.role.value:
        return None
    return row


P = ParamSpec("P")
R = TypeVar("R")


def require_role(
    *allowed: Role,
) -> Callable[
    [Callable[Concatenate[Session, sqlite3.Connection, P], R]],
    Callable[Concatenate[Session | None, sqlite3.Connection, P], R],
]:
    """Check current database identity and password-change gate before any action.

    The access event commits before dispatch when no transaction is active. The
    action owns its business transaction. Within an existing transaction, access
    evidence shares the caller's commit/rollback fate; never nest BEGINs.
    """
    if not allowed or any(not isinstance(role, Role) for role in allowed):
        raise ValueError("require_role needs at least one explicit Role")

    def decorator(
        fn: Callable[Concatenate[Session, sqlite3.Connection, P], R],
    ) -> Callable[Concatenate[Session | None, sqlite3.Connection, P], R]:
        @functools.wraps(fn)
        def wrapper(
            session: Session | None, conn: sqlite3.Connection, /,
            *args: P.args, **kwargs: P.kwargs,
        ) -> R:
            with nullcontext() if conn.in_transaction else storage.transaction(conn):
                row = _identity_row(conn, session)
                granted = (
                    row is not None and session is not None
                    and session.role in allowed and not row["must_change_pw"]
                )
                actor = str(row["username"]) if row is not None else "anonymous"
                audit.write(
                    conn, actor,
                    audit.Action.ACCESS_GRANTED if granted else audit.Action.ACCESS_DENIED,
                    fn.__name__,
                )
            # Raise outside our transaction: denials must remain in the audit log.
            if not granted or session is None:
                raise AccessDenied("You are not permitted to perform this action. Change your password if required.")
            return fn(session, conn, *args, **kwargs)

        setattr(wrapper, "__required_roles__", frozenset(allowed))
        return wrapper

    return decorator


def login(conn: sqlite3.Connection, username: str, password: str) -> Session:
    """Authenticate with one failure message, persisted lockout, and audit events."""
    session: Session | None = None
    with storage.transaction(conn):
        row = storage.get_user_by_username(conn, username)
        valid = verify_password(str(row["pw_hash"]) if row else _DUMMY_HASH, password)
        if row is None:
            audit.write(conn, username, audit.Action.LOGIN_FAILURE, "unknown account")
        else:
            now = datetime.now(timezone.utc)
            locked_until = row["locked_until"]
            locked = locked_until is not None and datetime.fromisoformat(locked_until) > now
            if locked:
                audit.write(conn, row["username"], audit.Action.LOGIN_FAILURE, "account locked")
            elif not valid:
                # An expired lockout starts a fresh sequence of attempts.
                if locked_until is not None:
                    storage.reset_login_failures(conn, row["id"])
                failures = 1 if locked_until is not None else int(row["failed_count"]) + 1
                until = None
                if failures >= MAX_FAILURES:
                    until = (now + timedelta(minutes=LOCKOUT_MINS)).isoformat(timespec="seconds").replace("+00:00", "Z")
                storage.record_login_failure(conn, row["id"], until)
                audit.write(conn, row["username"], audit.Action.LOGIN_FAILURE, f"attempt {failures}")
                if until is not None:
                    audit.write(conn, row["username"], audit.Action.LOCKOUT, until)
            else:
                storage.reset_login_failures(conn, row["id"])
                if _ph.check_needs_rehash(row["pw_hash"]):
                    # Preserve must_change_pw; a rehash is not a password change.
                    conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (hash_password(password), row["id"]))
                audit.write(conn, row["username"], audit.Action.LOGIN_SUCCESS)
                session = Session(row["id"], row["username"], Role(row["role"]))
    if session is None:
        raise AuthError(LOGIN_FAILURE_MESSAGE)
    return session


def register_player(conn: sqlite3.Connection, username: str, password: str) -> int:
    """Create a player and zero-balance account atomically; never accept a role."""
    _validate_username(username)
    error = validate_password_strength(password)
    if error is not None:
        raise AuthError(error)
    pw_hash = hash_password(password)
    with storage.transaction(conn):
        if storage.get_user_by_username(conn, username) is not None:
            raise AuthError("That username is unavailable.")
        uid = storage.create_user(conn, username, pw_hash, Role.PLAYER.value)
        storage.create_account(conn, uid)
        audit.write(conn, username, audit.Action.USER_REGISTER, Role.PLAYER.value)
    return uid


def bootstrap_if_empty(conn: sqlite3.Connection) -> str | None:
    """Create tech-admin once; return its random password for terminal display only."""
    with storage.transaction(conn):
        if storage.count_users(conn) != 0:
            return None
        password = secrets.token_urlsafe(18)
        storage.create_user(conn, "tech-admin", hash_password(password), Role.TECHNICIAN.value, True)
        audit.write(conn, "tech-admin", audit.Action.BOOTSTRAP_TECHNICIAN_CREATED)
    return password


def must_change_password(conn: sqlite3.Connection, session: Session) -> bool:
    row = _identity_row(conn, session)
    if row is None:
        raise AuthError(LOGIN_FAILURE_MESSAGE)
    return bool(row["must_change_pw"])


def change_password(
    conn: sqlite3.Connection, session: Session, current_password: str, new_password: str,
) -> None:
    """Require the current password, enforce policy, and unlock the role gate."""
    error = validate_password_strength(new_password)
    if error is not None:
        raise AuthError(error)
    with storage.transaction(conn):
        row = _identity_row(conn, session)
        valid = verify_password(str(row["pw_hash"]) if row else _DUMMY_HASH, current_password)
        if row is None or not valid:
            raise AuthError(LOGIN_FAILURE_MESSAGE)
        if current_password == new_password:
            raise AuthError("Choose a different password.")
        storage.set_password_hash(conn, session.user_id, hash_password(new_password))
        storage.reset_login_failures(conn, session.user_id)
        audit.write(conn, session.username, audit.Action.PASSWORD_CHANGE)
