"""The Big Casino technician interface.

Every public function here is a privileged action and carries
``@auth.require_role(Role.TECHNICIAN)``, and nothing else. A test in
tests/test_source_invariants.py fails if one is missing or names another role,
so a Phase II change that quietly drops the check is caught. The decorator
re-reads the caller's role from the database and writes the attempt to the
audit log, granted or denied.

Private ``_helpers`` only prompt and print; they reach data through the
public, guarded functions.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from slotmachine import accounts, audit, auth, stats, storage
from slotmachine.audit import Action
from slotmachine.cli.common import RequestError, change_password, heading, percent
from slotmachine.cli.menus import MenuItem, menu, table
from slotmachine.cli.prompts import (
    check_game_id,
    clean,
    info,
    prompt_int,
    prompt_text,
    prompt_username,
    prompt_yes_no,
    success,
)
from slotmachine.game import GameSpec, SpecError, load_spec
from slotmachine.game.spec import dump_spec
from slotmachine.money import Money
from slotmachine.tools.rtpcalc import par_sheet, render

Role = auth.Role

#: A game config is a few kilobytes; anything far larger is not one.
MAX_CONFIG_BYTES: Final[int] = 256 * 1024

#: A game must keep the house edge positive, even against perfect hold play.
MAX_RTP_PERCENT: Final[float] = 100.0

# Limits on an installed game's shape. They keep the exact RTP proof quick
# (it grows with symbols ** reels) and stop a config from being a denial of
# service.
MAX_REELS: Final[int] = 5
MAX_SYMBOLS: Final[int] = 10
MAX_STRIP: Final[int] = 128
MAX_ROWS: Final[int] = 5
MAX_PAYLINES: Final[int] = 20
MAX_BET_TIERS: Final[int] = 6
MAX_BET: Final[Money] = Money(100_000)  # $1,000.00
MAX_HOLD_REELS: Final[int] = 3  # the perfect-hold proof grows fastest of all

AUDIT_PAGE_MAX: Final[int] = 500
FILTER_MAX: Final[int] = 64
_TIMESTAMP: Final[re.Pattern[str]] = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:T[0-9]{2}:[0-9]{2}(?::[0-9]{2})?Z?)?", re.ASCII
)


class GameAdminError(RequestError):
    """A game-management request was refused. The message is safe to show."""


# ---------------------------------------------------------------------------
# Games: list, add, enable and disable
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def view_games(session: auth.Session, conn: sqlite3.Connection) -> list[tuple[GameSpec, bool]]:
    """Every installed game and whether players can currently choose it."""
    return [(load_spec(row["config_json"]), bool(row["enabled"])) for row in storage.list_games(conn, enabled_only=False)]


@auth.require_role(Role.TECHNICIAN)
def enable_game(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> None:
    """Put a game on the playable interface."""
    with storage.transaction(conn):
        row = storage.get_game(conn, game_id)
        if row is None:
            raise GameAdminError("there is no game with that id")
        _check_house_edge(load_spec(row["config_json"]))
        storage.set_game_enabled(conn, game_id, True)
        audit.write(conn, session.username, Action.GAME_ENABLED, f"game={game_id}")


@auth.require_role(Role.TECHNICIAN)
def disable_game(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> None:
    """Take a game off the playable interface.

    Its spins stay on record, so it is disabled rather than deleted. Players
    already in a session on it may finish and cash out; nobody can buy in.
    """
    with storage.transaction(conn):
        if storage.get_game(conn, game_id) is None:
            raise GameAdminError("there is no game with that id")
        enabled = storage.list_games(conn, enabled_only=True)
        if len(enabled) == 1 and enabled[0]["id"] == game_id:
            raise GameAdminError("this is the last playable game; enable another one first")
        storage.set_game_enabled(conn, game_id, False)
        audit.write(conn, session.username, Action.GAME_DISABLED, f"game={game_id}")


@auth.require_role(Role.TECHNICIAN)
def install_game(session: auth.Session, conn: sqlite3.Connection, config_path: str) -> GameSpec:
    """Add a new game from a JSON config file. It starts disabled, for review.

    The terminal's way in; the back office pastes the same JSON into
    install_game_config. Both end in _install, so they apply the same checks.
    """
    path = Path(config_path).expanduser()
    if path.suffix.lower() != ".json":
        raise GameAdminError("a game config is a .json file")
    if not path.is_file():
        raise GameAdminError("no such file")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise GameAdminError("that file is too large to be a game config")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise GameAdminError("that file could not be read as UTF-8 text") from None
    return _install(session, conn, text)


@auth.require_role(Role.TECHNICIAN)
def install_game_config(session: auth.Session, conn: sqlite3.Connection, config_json: str) -> GameSpec:
    """Add a new game from JSON text pasted into the back office. It starts disabled."""
    if not isinstance(config_json, str) or not config_json.strip():
        raise GameAdminError("paste the game's JSON config")
    if len(config_json.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise GameAdminError("that is too large to be a game config")
    return _install(session, conn, config_json)


def _install(session: auth.Session, conn: sqlite3.Connection, text: str) -> GameSpec:
    """Validate, size-check, prove the house edge, then add the game disabled.

    Parsed as JSON and validated field by field (never pickle or any format
    that runs code). The size limits come before the RTP proof, because that
    proof grows with symbols ** reels and a hostile config could otherwise
    keep the program busy for years. An installed game's rules can never be
    replaced, because past spins must keep replaying to the same result;
    publish a new id instead.
    """
    try:
        spec = load_spec(text)
    except SpecError as exc:
        raise GameAdminError(f"invalid game config: {exc}") from None
    _check_size(spec)
    _check_house_edge(spec)

    with storage.transaction(conn):
        if storage.get_game(conn, spec.id) is not None:
            raise GameAdminError(f"a game with id {spec.id!r} is already installed")
        storage.upsert_game(conn, spec.id, spec.name, dump_spec(spec), enabled=False)
        audit.write(conn, session.username, Action.GAME_INSTALLED, f"game={spec.id} name={spec.name}")
    return spec


def _check_size(spec: GameSpec) -> None:
    if spec.reel_count > MAX_REELS:
        raise GameAdminError(f"a game can have at most {MAX_REELS} reels")
    if len(spec.symbols) > MAX_SYMBOLS:
        raise GameAdminError(f"a game can have at most {MAX_SYMBOLS} symbols")
    if any(len(reel) > MAX_STRIP for reel in spec.reels):
        raise GameAdminError(f"a reel can have at most {MAX_STRIP} positions")
    if spec.rows > MAX_ROWS:
        raise GameAdminError(f"a game can show at most {MAX_ROWS} rows")
    if len(spec.paylines) > MAX_PAYLINES:
        raise GameAdminError(f"a game can have at most {MAX_PAYLINES} paylines")
    if len(spec.bet_tiers) > MAX_BET_TIERS or any(tier > MAX_BET.cents for tier in spec.bet_tiers):
        raise GameAdminError(f"a game can have at most {MAX_BET_TIERS} bets, each at most {MAX_BET}")
    if spec.holds.enabled and spec.reel_count > MAX_HOLD_REELS:
        raise GameAdminError(f"holds are allowed only on games with at most {MAX_HOLD_REELS} reels")


@auth.require_role(Role.TECHNICIAN)
def view_par_sheet(session: auth.Session, conn: sqlite3.Connection, game_id: str) -> str:
    """A game's PAR sheet, to read before enabling a newly installed game."""
    row = storage.get_game(conn, game_id) if isinstance(game_id, str) else None
    if row is None:
        raise GameAdminError("there is no game with that id")
    return render(par_sheet(load_spec(row["config_json"]), samples=50_000))


def _check_house_edge(spec: GameSpec) -> None:
    theory = stats.theoretical(spec)
    worst = theory.hold_rtp if theory.hold_rtp is not None else theory.base_rtp
    if max(worst, theory.base_rtp) >= MAX_RTP_PERCENT:
        raise GameAdminError(
            f"this game would pay back {max(worst, theory.base_rtp):.2f}%; it must stay under 100%"
        )


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Diagnostic:
    games: list[tuple[stats.GameFigures, stats.Theory]]
    users_by_role: dict[str, int]
    locked_accounts: int
    open_sessions: int
    credits_in_machines: Money
    player_balances: Money
    audit_entries: int
    audit_chain: audit.VerifyResult
    database_ok: bool


@auth.require_role(Role.TECHNICIAN)
def run_system_diagnostic(session: auth.Session, conn: sqlite3.Connection) -> Diagnostic:
    """Wins, losses and play frequency per game, plus the health of the system."""
    games = []
    for figures in stats.game_figures(conn):
        row = storage.get_game(conn, figures.game_id)
        assert row is not None
        games.append((figures, stats.theoretical(load_spec(row["config_json"]))))

    roles = {str(r["role"]): int(r["n"]) for r in conn.execute("SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    locked = conn.execute(
        "SELECT COUNT(*) AS n FROM users WHERE locked_until IS NOT NULL AND locked_until > ?",
        (storage.utc_now(),),
    ).fetchone()
    open_row = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(credits_cents), 0) AS credits FROM sessions WHERE ended_at IS NULL"
    ).fetchone()
    balances = conn.execute("SELECT COALESCE(SUM(balance_cents), 0) AS total FROM accounts").fetchone()
    entries = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()

    return Diagnostic(
        games=games,
        users_by_role=roles,
        locked_accounts=int(locked["n"]),
        open_sessions=int(open_row["n"]),
        credits_in_machines=Money(int(open_row["credits"])),
        player_balances=Money(int(balances["total"])),
        audit_entries=int(entries["n"]),
        audit_chain=audit.verify_chain(conn),
        database_ok=str(integrity[0]) == "ok",
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def view_audit_log(
    session: auth.Session,
    conn: sqlite3.Connection,
    actor: str | None = None,
    action: str | None = None,
    limit: int = 25,
    *,
    since: str | None = None,
    until: str | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """A page of audit entries, newest first, filtered by user, event or date."""
    for value in (actor, action):
        if value is not None and (not isinstance(value, str) or len(value) > FILTER_MAX):
            raise GameAdminError(f"a filter can be at most {FILTER_MAX} characters")
    if not _whole(limit) or not 1 <= limit <= AUDIT_PAGE_MAX:
        raise GameAdminError(f"show between 1 and {AUDIT_PAGE_MAX} entries at a time")
    if not _whole(offset) or offset < 0:
        raise GameAdminError("the page must be 0 or more")
    return audit.read(
        conn,
        actor=actor,
        action=action,
        since=_timestamp(since, end_of_day=False),
        until=_timestamp(until, end_of_day=True),
        limit=limit,
        offset=offset,
    )


def _timestamp(value: str | None, *, end_of_day: bool) -> str | None:
    """A date or date-time filter in the audit log's format. A bare date covers the whole day."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise GameAdminError("dates look like 2026-11-17 or 2026-11-17T09:30")
    if len(value) == 10:
        return value + ("T23:59:59Z" if end_of_day else "T00:00:00Z")
    return value


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@auth.require_role(Role.TECHNICIAN)
def verify_audit_chain(session: auth.Session, conn: sqlite3.Connection) -> audit.VerifyResult:
    """Recompute every hash in the audit log and report the first broken link."""
    result = audit.verify_chain(conn)
    with storage.transaction(conn):
        outcome = "intact" if result.ok else f"BROKEN at id={result.first_bad_id} ({result.reason})"
        audit.write(conn, session.username, Action.AUDIT_VERIFIED, f"{result.entries_checked} entries {outcome}")
    return result


# ---------------------------------------------------------------------------
# Staff accounts
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def view_users(session: auth.Session, conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every account, without password hashes."""
    return auth.list_users(conn)


@auth.require_role(Role.TECHNICIAN)
def add_staff(session: auth.Session, conn: sqlite3.Connection, username: str, role: auth.Role) -> str:
    """Create a technician or commissioner and return their one-time password."""
    return auth.create_staff_account(session, conn, username, role)


# ---------------------------------------------------------------------------
# The menu
# ---------------------------------------------------------------------------


@auth.require_role(Role.TECHNICIAN)
def technician_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    """The technician's main menu."""
    menu(
        "BIG CASINO TECHNICIAN",
        [
            MenuItem("Manage games", lambda: _games_menu(session, conn)),
            MenuItem("Run system diagnostic", lambda: _show_diagnostic(session, conn)),
            MenuItem("View audit log", lambda: _show_audit_log(session, conn)),
            MenuItem("Verify audit log integrity", lambda: _show_verify(session, conn)),
            MenuItem("Staff accounts", lambda: _staff_menu(session, conn)),
            MenuItem("Change my password", lambda: change_password(conn, session)),
        ],
        session,
        back_label="Log out",
    )


def _games_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    menu(
        "MANAGE GAMES",
        [
            MenuItem("List games", lambda: _show_games(session, conn)),
            MenuItem("Enable a game (add to the playable interface)", lambda: _toggle(session, conn, True)),
            MenuItem("Disable a game (remove from the playable interface)", lambda: _toggle(session, conn, False)),
            MenuItem("Install a new game from a JSON config file", lambda: _install_from_file(session, conn)),
            MenuItem("View a game's PAR sheet", lambda: _show_par(session, conn)),
        ],
        session,
    )


def _show_games(session: auth.Session, conn: sqlite3.Connection) -> None:
    rows = []
    for spec, enabled in view_games(session, conn):
        theory = stats.theoretical(spec)
        rows.append([
            spec.id,
            spec.name,
            "playable" if enabled else "disabled",
            f"{spec.reel_count}x{spec.rows}",
            len(spec.paylines),
            ", ".join(str(Money(t)) for t in spec.bet_tiers),
            "yes" if spec.holds.enabled else "no",
            percent(theory.base_rtp),
            "-" if theory.hold_rtp is None else percent(theory.hold_rtp),
        ])
    heading("Installed games")
    print(table(["Id", "Name", "Status", "Reels", "Lines", "Bets", "Holds", "RTP", "RTP (best holds)"], rows))


def _pick_game_id(session: auth.Session, conn: sqlite3.Connection) -> str:
    _show_games(session, conn)
    return prompt_text("Game id", validator=check_game_id)


def _toggle(session: auth.Session, conn: sqlite3.Connection, enable: bool) -> None:
    game_id = _pick_game_id(session, conn)
    if enable:
        enable_game(session, conn, game_id)
        success(f"{game_id} is now playable.")
    else:
        disable_game(session, conn, game_id)
        success(f"{game_id} is no longer playable. Open sessions on it can still cash out.")


def _show_par(session: auth.Session, conn: sqlite3.Connection) -> None:
    print(view_par_sheet(session, conn, _pick_game_id(session, conn)))


def _install_from_file(session: auth.Session, conn: sqlite3.Connection) -> None:
    info("The file is checked field by field and must keep the house edge positive.")
    path = prompt_text("Path to the .json file", max_length=512)
    spec = install_game(session, conn, path)
    success(f"Installed {spec.name} ({spec.id}). It is disabled until you enable it.")


def _show_diagnostic(session: auth.Session, conn: sqlite3.Connection) -> None:
    diag = run_system_diagnostic(session, conn)

    heading("Wins and losses per game (live play)")
    print(table(
        ["Game", "Status", "Spins", "Wins", "Losses", "Hit rate", "Wagered", "Paid out", "House net"],
        [
            [f.name, "playable" if f.enabled else "disabled", f"{f.spins:,}", f"{f.wins:,}", f"{f.losses:,}",
             percent(f.hit_rate), str(f.wagered), str(f.paid), str(f.house_net)]
            for f, _ in diag.games
        ],
    ))

    heading("Return to player and frequency of play")
    print(table(
        ["Game", "RTP observed", "RTP theory", "Hit theory", "Biggest win", "Players", "Sessions", "Spins/day", "Last played"],
        [
            [f.name, percent(f.observed_rtp), percent(t.base_rtp), percent(t.hit_rate), str(f.biggest_payout),
             f"{f.players:,}", f"{f.sessions:,}", f"{f.spins_per_day:,.1f}", f.last_spin or "never"]
            for f, t in diag.games
        ],
    ))

    heading("System")
    users = ", ".join(f"{n} {role}(s)" for role, n in sorted(diag.users_by_role.items()))
    chain = diag.audit_chain
    lines = [
        ("Accounts", users),
        ("Locked accounts", str(diag.locked_accounts)),
        ("Open game sessions", f"{diag.open_sessions} holding {diag.credits_in_machines}"),
        ("Player balances", str(diag.player_balances)),
        ("Daily limits", f"deposit {accounts.DAILY_DEPOSIT_LIMIT}, withdraw {accounts.DAILY_WITHDRAW_LIMIT}"),
        ("Audit entries", f"{diag.audit_entries:,}"),
        ("Audit chain", "intact" if chain.ok else f"BROKEN at entry {chain.first_bad_id} ({chain.reason})"),
        ("Database integrity", "ok" if diag.database_ok else "FAILED"),
    ]
    print(table(["Check", "Result"], lines, align="<<"))


_ACTION_CHOICES: Final = sorted(
    value for name, value in vars(Action).items() if not name.startswith("_") and isinstance(value, str)
)


def _show_audit_log(session: auth.Session, conn: sqlite3.Connection) -> None:
    actor = prompt_text("Only this user (Enter for everyone)", allow_empty=True, max_length=32) or None
    action: str | None = None
    if prompt_yes_no("Filter by event type?", default=False):
        print(table(["#", "Event"], [[i, name] for i, name in enumerate(_ACTION_CHOICES, start=1)]))
        action = _ACTION_CHOICES[prompt_int("Event #", minimum=1, maximum=len(_ACTION_CHOICES)) - 1]
    limit = prompt_int("How many entries (newest first)", minimum=1, maximum=500)
    rows = view_audit_log(session, conn, actor, action, limit)
    heading(f"Audit log — {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}")
    if not rows:
        info("Nothing matches.")
        return
    print(table(
        ["Id", "Time (UTC)", "Actor", "Event", "Detail"],
        [[r["id"], r["ts"], r["actor"], r["action"], r["detail"]] for r in rows],
        align=">><<<",
    ))


def _show_verify(session: auth.Session, conn: sqlite3.Connection) -> None:
    result = verify_audit_chain(session, conn)
    if result.ok:
        success(f"Audit log intact: all {result.entries_checked:,} entries verified.")
    else:
        info(f"✗ TAMPERING DETECTED at entry {result.first_bad_id}: {clean(result.reason)}.")
        info("Entries from that point on cannot be trusted. Preserve the database for investigation.")


def _staff_menu(session: auth.Session, conn: sqlite3.Connection) -> None:
    menu(
        "STAFF ACCOUNTS",
        [
            MenuItem("List accounts", lambda: _show_users(session, conn)),
            MenuItem("Create a commissioner account", lambda: _create_staff(session, conn, Role.COMMISSIONER)),
            MenuItem("Create a technician account", lambda: _create_staff(session, conn, Role.TECHNICIAN)),
        ],
        session,
    )


def _show_users(session: auth.Session, conn: sqlite3.Connection) -> None:
    now = storage.utc_now()
    rows = [
        [r["username"], r["role"], "yes" if r["must_change_pw"] else "no",
         "locked" if r["locked_until"] and r["locked_until"] > now else "ok", r["created_at"]]
        for r in view_users(session, conn)
    ]
    heading("Accounts")
    print(table(["Username", "Role", "Must change password", "Login", "Created (UTC)"], rows))


def _create_staff(session: auth.Session, conn: sqlite3.Connection, role: auth.Role) -> None:
    username = prompt_username("New username")
    password = add_staff(session, conn, username, role)
    success(f"Created {role.value} {username}.")
    info(f"One-time password: {password}")
    info("It is shown once. Hand it over in person; it must be changed at first login.")


__all__ = [
    "GameAdminError",
    "add_staff",
    "disable_game",
    "enable_game",
    "install_game",
    "install_game_config",
    "run_system_diagnostic",
    "technician_menu",
    "verify_audit_chain",
    "view_audit_log",
    "view_games",
    "view_par_sheet",
    "view_users",
]
