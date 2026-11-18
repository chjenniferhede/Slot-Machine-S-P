"""What a technician can do: run the games, read the diagnostics, guard the log.

Every public function here is a technician action and carries
``@require_role(Role.TECHNICIAN)``, and nothing else. A test imports this
module and fails if any public function is missing the decorator or allows
another role, so an unguarded action cannot be added by accident.

Technicians never touch player money and have no wallet. They also cannot
see spin seeds: those are for commissioners (``commissioner.py``).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Final

from slotmachine import audit, auth, stats, storage
from slotmachine.audit import Action, VerifyResult
from slotmachine.auth import Role
from slotmachine.game.spec import GameSpec, SpecError, dump_spec, load_spec
from slotmachine.money import Money
from slotmachine.tools import rtpcalc

# Limits on a game a technician installs. They keep the exact RTP proof
# below quick to run, and stop a config from being a denial of service.
MAX_CONFIG_CHARS: Final[int] = 64 * 1024
MAX_REELS: Final[int] = 5
MAX_SYMBOLS: Final[int] = 10
MAX_STRIP: Final[int] = 128
MAX_ROWS: Final[int] = 5
MAX_PAYLINES: Final[int] = 20
MAX_BET_TIERS: Final[int] = 6
MAX_BET: Final[Money] = Money(100_000)  # $1,000.00
MAX_HOLD_REELS: Final[int] = 3  # the perfect-hold proof grows fast with reels

AUDIT_PAGE_MAX: Final[int] = 500
FILTER_MAX: Final[int] = 64
_TIMESTAMP: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?Z?)?", re.ASCII
)

STAFF_ROLES: Final[frozenset[Role]] = frozenset({Role.TECHNICIAN, Role.COMMISSIONER})


class TechnicianError(ValueError):
    """A technician request was refused. The message is safe to show as-is."""


@dataclass(frozen=True)
class GameListing:
    spec: GameSpec
    enabled: bool
    created_at: str


@dataclass(frozen=True)
class AuditEntry:
    id: int
    ts: str
    actor: str
    action: str
    detail: str


@dataclass(frozen=True)
class StaffMember:
    username: str
    role: str
    must_change_password: bool
    locked_until: str | None
    created_at: str


@dataclass(frozen=True)
class NewStaff:
    username: str
    role: str
    one_time_password: str  # shown once, never stored in the clear

    def __repr__(self) -> str:
        return f"NewStaff(username={self.username!r}, role={self.role!r})"


# ---------------------------------------------------------------------------
# Games
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def list_games(session: auth.Session, conn: sqlite3.Connection) -> list[GameListing]:
    """Every installed game, enabled or not."""
    return [
        GameListing(spec=stats.spec_of(row), enabled=bool(row["enabled"]), created_at=str(row["created_at"]))
        for row in storage.list_games(conn, enabled_only=False)
    ]


@auth.require_role(Role.TECHNICIAN)
def set_game_enabled(session: auth.Session, conn: sqlite3.Connection, game_id: str, enabled: bool) -> bool:
    """Show a game to players, or hide it. Returns whether anything changed.

    Disabling refuses new buy-ins only. A session already open carries on and
    cashes out normally, so no player's credits are stranded in it.
    """
    if not isinstance(game_id, str) or not isinstance(enabled, bool):
        raise TechnicianError("choose a game and whether to enable it")
    with storage.transaction(conn):
        row = storage.get_game(conn, game_id)
        if row is None:
            raise TechnicianError("there is no game with that id")
        if bool(row["enabled"]) == enabled:
            return False
        storage.set_game_enabled(conn, game_id, enabled)
        audit.write(
            conn,
            session.username,
            Action.GAME_ENABLED if enabled else Action.GAME_DISABLED,
            f"game={game_id}",
        )
    return True


@auth.require_role(Role.TECHNICIAN)
def install_game(session: auth.Session, conn: sqlite3.Connection, config_json: str) -> GameSpec:
    """Add a new game from a JSON config. It starts disabled.

    The config is parsed as JSON only, never unpickled or run (CWE-502), and
    checked field by field. Its exact return to player is then worked out,
    including perfect hold play, and a game that would pay back more than it
    takes is refused (OWASP A04). It goes live only when a technician has
    read its PAR sheet and enables it.
    """
    if not isinstance(config_json, str) or not config_json.strip():
        raise TechnicianError("paste the game's JSON config")
    if len(config_json) > MAX_CONFIG_CHARS:
        raise TechnicianError(f"a game config can be at most {MAX_CONFIG_CHARS:,} characters")
    try:
        spec = load_spec(config_json)
    except SpecError as exc:
        raise TechnicianError(f"that config is not a valid game: {exc}") from None

    _check_size(spec)
    base, _ = rtpcalc.base_rtp(spec)
    optimal = rtpcalc.optimal_hold_rtp(spec)
    worst = optimal if optimal is not None else base
    if worst >= 1:
        raise TechnicianError(
            f"this game would pay back {worst:.2%} of every bet; a game must keep a house edge"
        )

    with storage.transaction(conn):
        if not storage.insert_game_if_missing(conn, spec.id, spec.name, dump_spec(spec), enabled=False):
            raise TechnicianError("a game with that id is already installed")
        audit.write(
            conn,
            session.username,
            Action.GAME_INSTALLED,
            f"game={spec.id} source=technician rtp={base:.4f}",
        )
    return spec


@auth.require_role(Role.TECHNICIAN)
def par_sheet(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> rtpcalc.ParSheet:
    """The game's PAR sheet: strips, paytable, exact RTP and hit frequency."""
    row = storage.get_game(conn, game_id) if isinstance(game_id, str) else None
    if row is None:
        raise TechnicianError("there is no game with that id")
    return stats.par_sheet(str(row["config_json"]))


def _check_size(spec: GameSpec) -> None:
    if spec.reel_count > MAX_REELS:
        raise TechnicianError(f"a game can have at most {MAX_REELS} reels")
    if len(spec.symbols) > MAX_SYMBOLS:
        raise TechnicianError(f"a game can have at most {MAX_SYMBOLS} symbols")
    if any(len(reel) > MAX_STRIP for reel in spec.reels):
        raise TechnicianError(f"a reel can have at most {MAX_STRIP} positions")
    if spec.rows > MAX_ROWS:
        raise TechnicianError(f"a game can show at most {MAX_ROWS} rows")
    if len(spec.paylines) > MAX_PAYLINES:
        raise TechnicianError(f"a game can have at most {MAX_PAYLINES} paylines")
    if len(spec.bet_tiers) > MAX_BET_TIERS or any(tier > MAX_BET.cents for tier in spec.bet_tiers):
        raise TechnicianError(f"a game can have at most {MAX_BET_TIERS} bets, each at most {MAX_BET}")
    if spec.holds.enabled and spec.reel_count > MAX_HOLD_REELS:
        raise TechnicianError(f"holds are allowed only on games with at most {MAX_HOLD_REELS} reels")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def system_diagnostic(session: auth.Session, conn: sqlite3.Connection) -> list[stats.GameStats]:
    """Live wins, losses, money in and out, and play frequency for every game.

    Commissioner test rounds are left out: they are not casino money.
    """
    return stats.game_stats(conn, test=False)


# ---------------------------------------------------------------------------
# The audit log
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def read_audit_log(
    session: auth.Session,
    conn: sqlite3.Connection,
    *,
    actor: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AuditEntry]:
    """A page of the audit log, newest first, filtered on any of its fields."""
    for value in (actor, action):
        if value is not None and (not isinstance(value, str) or len(value) > FILTER_MAX):
            raise TechnicianError(f"a filter can be at most {FILTER_MAX} characters")
    since = _timestamp(since, end_of_day=False)
    until = _timestamp(until, end_of_day=True)
    if not _whole(limit) or not 1 <= limit <= AUDIT_PAGE_MAX:
        raise TechnicianError(f"show between 1 and {AUDIT_PAGE_MAX} entries at a time")
    if not _whole(offset) or offset < 0:
        raise TechnicianError("the page must be 0 or more")

    rows = audit.read(
        conn, actor=actor, action=action, since=since, until=until, limit=limit, offset=offset
    )
    return [
        AuditEntry(
            id=int(row["id"]),
            ts=str(row["ts"]),
            actor=str(row["actor"]),
            action=str(row["action"]),
            detail=str(row["detail"]),
        )
        for row in rows
    ]


@auth.require_role(Role.TECHNICIAN)
def verify_audit_chain(session: auth.Session, conn: sqlite3.Connection) -> VerifyResult:
    """Walk the whole hash chain and report the first entry that does not fit."""
    return audit.verify_chain(conn)


def _timestamp(value: str | None, *, end_of_day: bool) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise TechnicianError("dates look like 2026-11-17 or 2026-11-17T09:30")
    if len(value) == 10:
        return value + ("T23:59:59Z" if end_of_day else "T00:00:00Z")
    return value


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Staff accounts
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def list_staff(session: auth.Session, conn: sqlite3.Connection) -> list[StaffMember]:
    return [
        StaffMember(
            username=str(row["username"]),
            role=str(row["role"]),
            must_change_password=bool(row["must_change_pw"]),
            locked_until=None if row["locked_until"] is None else str(row["locked_until"]),
            created_at=str(row["created_at"]),
        )
        for row in storage.list_staff(conn)
    ]


@auth.require_role(Role.TECHNICIAN)
def create_staff_account(
    session: auth.Session, conn: sqlite3.Connection, username: str, role: Role
) -> NewStaff:
    """Create a technician or commissioner with a one-time password.

    The password is random, returned once for the technician to hand over,
    and must be changed at first login. Staff get no wallet, so they can
    never play or hold player money. Players register themselves instead.
    """
    if not isinstance(role, Role) or role not in STAFF_ROLES:
        raise TechnicianError("staff accounts are technicians or commissioners")
    if not isinstance(username, str):
        raise TechnicianError("choose a username")
    try:
        auth.validate_username(username)
    except auth.AuthError as exc:
        raise TechnicianError(str(exc)) from None

    password = auth.generate_initial_password()
    pw_hash = auth.hash_password(password)
    with storage.transaction(conn):
        if storage.get_user_by_username(conn, username) is not None:
            raise TechnicianError("that username is unavailable")
        storage.create_user(conn, username, pw_hash, role.value, must_change_pw=True)
        audit.write(
            conn, session.username, Action.USER_STAFF_CREATED, f"user={username} role={role.value}"
        )
    return NewStaff(username=username, role=role.value, one_time_password=password)
