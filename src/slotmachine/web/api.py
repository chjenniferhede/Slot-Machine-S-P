"""The JSON API behind the pages: one small function per route.

Each route reads its fields from the request body, checks their types, and
calls one role-gated action: ``accounts``, ``payment`` and ``stats`` for
players, ``cli.technician`` and ``cli.commissioner`` for staff. Those are the
same guarded functions the terminal menus call, so the browser and the
terminal share one set of rules. Routes make no access decisions of their
own: a player who calls a technician route reaches ``cli.technician`` and
``auth.require_role`` refuses them there, exactly as it would refuse a direct
call. The menus a page shows are a convenience, never the control.

Values from the browser are untrusted. Amounts arrive as text such as
"12.50" and go through ``Money.parse``; JSON numbers with a fraction never
get this far (the server refuses them while parsing). Ids are whole numbers,
and bools are not accepted as numbers.

Seeds never appear in a player's response. Only commissioner routes include them.

Ratios (RTP, hit rate) are sent as fractions, 0.9137 for 91.37%; the
statistics module keeps them as percentages for the terminal.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from slotmachine import accounts, auth, payment, stats
from slotmachine.audit import Action, VerifyResult
from slotmachine.cli import commissioner, technician
from slotmachine.cli.menus import USER_ERRORS
from slotmachine.game.engine import grid_from_stops
from slotmachine.game.spec import GameSpec
from slotmachine.money import Money
from slotmachine.web.sessions import LoginStore, WebLogin

TEXT_MAX: Final[int] = 128
PASSWORD_FIELD_MAX: Final[int] = 1024  # auth enforces the real policy
WHOLE_MAX: Final[int] = 2**53  # ids and counts; well inside SQLite's INTEGER
#: Rounds of a test session listed one by one in the browser. Every round is
#: still recorded, replayed, and written to the CSV export.
ROUNDS_SHOWN: Final[int] = 1_000

#: Event names for the audit log's filter list.
AUDIT_ACTIONS: Final[tuple[str, ...]] = tuple(
    sorted(value for name, value in vars(Action).items() if not name.startswith("_") and isinstance(value, str))
)


class BadRequest(ValueError):
    """The request is malformed. The message is safe to show."""


class NotSignedIn(Exception):
    """The route needs a login and there is none, or it has expired."""


Json = dict[str, object]


@dataclass
class Call:
    """One request, as a route sees it."""

    conn: sqlite3.Connection
    store: LoginStore
    login: WebLogin | None
    body: Mapping[str, object]
    new_login: WebLogin | None = None  # the server sets this cookie
    ended: bool = False  # the server clears the cookie

    @property
    def who(self) -> auth.Session:
        if self.login is None or self.login.identity is None:
            raise NotSignedIn
        return self.login.identity


Route = Callable[[Call], object]


# ---------------------------------------------------------------------------
# Reading fields
# ---------------------------------------------------------------------------


def _text(call: Call, field: str, *, max_len: int = TEXT_MAX, optional: bool = False) -> str:
    value = call.body.get(field)
    if value is None and optional:
        return ""
    if not isinstance(value, str):
        raise BadRequest(f"{field} must be text")
    if len(value) > max_len:
        raise BadRequest(f"{field} can be at most {max_len} characters")
    return value


def _whole(call: Call, field: str) -> int:
    value = call.body.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or abs(value) > WHOLE_MAX:
        raise BadRequest(f"{field} must be a whole number")
    return value


def _flag(call: Call, field: str, default: bool | None = None) -> bool:
    value = call.body.get(field, default)
    if not isinstance(value, bool):
        raise BadRequest(f"{field} must be true or false")
    return value


def _amount(call: Call, field: str) -> Money:
    """An amount as typed: text such as "12.50", never a JSON number."""
    return Money.parse(_text(call, field, max_len=32))


def _whole_list(call: Call, field: str, *, max_items: int = 10) -> list[int]:
    value = call.body.get(field)
    if not isinstance(value, list) or len(value) > max_items:
        raise BadRequest(f"{field} must be a list of at most {max_items} numbers")
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or abs(item) > WHOLE_MAX:
            raise BadRequest(f"{field} must hold whole numbers")
    return list(value)


# ---------------------------------------------------------------------------
# Writing values
# ---------------------------------------------------------------------------


def money(value: Money) -> Json:
    return {"cents": value.cents, "text": str(value)}


def _fraction(percent: float | None) -> float | None:
    return None if percent is None else percent / 100


def game_info(spec: GameSpec) -> Json:
    theory = stats.theoretical(spec)
    return {
        "id": spec.id,
        "name": spec.name,
        "reels": spec.reel_count,
        "rows": spec.rows,
        "symbols": list(spec.symbols),
        "paylines": [list(line) for line in spec.paylines],
        "paytable": [
            {"symbol": rule.symbol, "count": rule.count, "multiplier": rule.multiplier}
            for rule in spec.paytable
        ],
        "bets": [money(Money(tier)) for tier in spec.bet_tiers],
        "holds": {
            "enabled": spec.holds.enabled and spec.holds.max_consecutive > 0,
            "max_consecutive": spec.holds.max_consecutive,
            "max_reels_held": spec.holds.max_reels_held,
            "cost_percent": spec.holds.cost_percent,
        },
        "rtp": _fraction(theory.base_rtp),
        "rtp_with_holds": _fraction(theory.hold_rtp),
        "hit_rate": _fraction(theory.hit_rate),
    }


def _spin(outcome: accounts.SpinOutcome) -> Json:
    result = outcome.result
    return {
        "spin_id": outcome.spin_id,
        "grid": [list(row) for row in result.grid],
        "stops": list(result.stops),
        "wins": [
            {"line": w.line_number, "symbol": w.symbol, "count": w.count, "payout": money(Money(w.payout_cents))}
            for w in result.wins
        ],
        "charged": money(outcome.charged),
        "payout": money(outcome.payout),
        "credits": money(outcome.credits),
        "held": list(outcome.held),
        "holds_left": outcome.holds_left,
        "respin_cost": None if outcome.respin_cost is None else money(outcome.respin_cost),
    }


def _figures(f: stats.GameFigures, theory: stats.Theory) -> Json:
    return {
        "game_id": f.game_id,
        "name": f.name,
        "enabled": f.enabled,
        "spins": f.spins,
        "wins": f.wins,
        "losses": f.losses,
        "wagered": money(f.wagered),
        "paid": money(f.paid),
        "house_net": money(f.house_net),
        "biggest_payout": money(f.biggest_payout),
        "players": f.players,
        "sessions": f.sessions,
        "spins_per_day": f.spins_per_day,
        "last_spin": f.last_spin,
        "observed_rtp": _fraction(f.observed_rtp) if f.spins else None,
        "hit_rate": _fraction(f.hit_rate) if f.spins else None,
        "theoretical_rtp": _fraction(theory.base_rtp),
        "optimal_hold_rtp": _fraction(theory.hold_rtp),
        "theoretical_hit_rate": _fraction(theory.hit_rate),
    }


def _verify(result: VerifyResult) -> Json:
    return {
        "ok": result.ok,
        "entries_checked": result.entries_checked,
        "first_bad_id": result.first_bad_id,
        "reason": result.reason,
    }


def _grid(spec: GameSpec, stops: tuple[int, ...]) -> list[list[str]]:
    return [list(row) for row in grid_from_stops(spec, list(stops))]


# ---------------------------------------------------------------------------
# Signing in
# ---------------------------------------------------------------------------


def me(call: Call) -> Json:
    login = call.new_login or call.login
    if login is None or login.identity is None:
        return {"signed_in": False}
    identity = login.identity
    return {
        "signed_in": True,
        "username": identity.username,
        "role": identity.role.value,
        "must_change_password": auth.must_change_password(call.conn, identity),
        "csrf": login.csrf,
    }


def login(call: Call) -> Json:
    identity = auth.login(
        call.conn, _text(call, "username", max_len=64), _text(call, "password", max_len=PASSWORD_FIELD_MAX)
    )
    if call.login is not None:
        call.store.end(call.login.sid)
    call.new_login = call.store.start(identity)
    return me(call)


def register(call: Call) -> Json:
    auth.register_player(
        call.conn, _text(call, "username", max_len=64), _text(call, "password", max_len=PASSWORD_FIELD_MAX)
    )
    return {"ok": True}


def logout(call: Call) -> Json:
    """End the login. A player's open game is cashed out first, as in the terminal."""
    cashed: Json | None = None
    if call.login is not None and call.login.identity is not None:
        identity = call.login.identity
        if identity.role is auth.Role.PLAYER:
            try:
                gs = accounts.current_game(identity, call.conn)
                if gs is not None:
                    cashed = money(accounts.cash_out(identity, call.conn, gs).amount)
            except USER_ERRORS:
                pass  # startup recovery returns anything left open
        call.store.end(call.login.sid)
    call.ended = True
    return {"signed_in": False, "cashed_out": cashed}


def change_password(call: Call) -> Json:
    current = _text(call, "current", max_len=PASSWORD_FIELD_MAX)
    new = _text(call, "new", max_len=PASSWORD_FIELD_MAX)
    auth.change_password(call.conn, call.who, current, new)
    assert call.login is not None
    call.new_login = call.store.renew(call.login)
    return me(call)


# ---------------------------------------------------------------------------
# Players
# ---------------------------------------------------------------------------


def player_games(call: Call) -> Json:
    return {"games": [game_info(spec) for spec in accounts.list_playable_games(call.who, call.conn)]}


def player_wallet(call: Call) -> Json:
    who = call.who
    methods = payment.list_payment_methods(who, call.conn)
    return {
        "balance": money(accounts.get_balance(who, call.conn)),
        "methods": [
            {"id": m.id, "brand": m.brand, "last4": m.last4, "exp": f"{m.exp_month:02d}/{m.exp_year}"}
            for m in methods
        ],
        "limits": {
            "deposit": money(accounts.DAILY_DEPOSIT_LIMIT),
            "withdraw": money(accounts.DAILY_WITHDRAW_LIMIT),
        },
    }


def add_card(call: Call) -> Json:
    card = payment.CardInput(
        number=_text(call, "number", max_len=32),
        exp_month=_whole(call, "exp_month"),
        exp_year=_whole(call, "exp_year"),
        cvv=_text(call, "cvv", max_len=8),
    )
    method = payment.add_payment_method(call.who, call.conn, card)
    return {"id": method.id, "brand": method.brand, "last4": method.last4}


def remove_card(call: Call) -> Json:
    payment.remove_payment_method(call.who, call.conn, _whole(call, "id"))
    return {"ok": True}


def deposit(call: Call) -> Json:
    balance = accounts.deposit(call.who, call.conn, _whole(call, "method_id"), _amount(call, "amount"))
    return {"balance": money(balance)}


def withdraw(call: Call) -> Json:
    balance = accounts.withdraw(call.who, call.conn, _whole(call, "method_id"), _amount(call, "amount"))
    return {"balance": money(balance)}


def current_game(call: Call) -> Json:
    who = call.who
    gs = accounts.current_game(who, call.conn)
    if gs is None:
        return {"game": None}
    rules = accounts.current_game_rules(who, call.conn)
    assert rules is not None
    return {
        "game": game_info(rules),
        "credits": money(accounts.get_credits(who, call.conn, gs)),
    }


def buy_in(call: Call) -> Json:
    who = call.who
    gs = accounts.start_session(who, call.conn, _text(call, "game_id", max_len=32), _amount(call, "amount"))
    return {"credits": money(accounts.get_credits(who, call.conn, gs))}


def _open_game(call: Call) -> accounts.GameSession:
    gs = accounts.current_game(call.who, call.conn)
    if gs is None:
        raise accounts.AccountError("buy in to a game first")
    return gs


def spin(call: Call) -> Json:
    gs = _open_game(call)
    return _spin(accounts.spin(call.who, call.conn, gs, _amount(call, "bet")))


def hold(call: Call) -> Json:
    gs = _open_game(call)
    return _spin(accounts.hold_and_respin(call.who, call.conn, gs, _whole_list(call, "reels")))


def cash_out(call: Call) -> Json:
    result = accounts.cash_out(call.who, call.conn, _open_game(call))
    return {"amount": money(result.amount), "net": money(result.net), "spins": result.spins}


def player_stats(call: Call) -> Json:
    s = stats.view_own_statistics(call.who, call.conn)
    return {
        "sessions": s.sessions,
        "spins": s.spins,
        "wins": s.wins,
        "win_rate": s.win_percent / 100 if s.spins else None,
        "wagered": money(s.wagered),
        "won": money(s.won),
        "net": money(s.net),
        "total_lost": money(s.total_lost),
        "biggest_payout": money(s.biggest_payout),
        "favourite_game": s.favourite_game,
        "by_game": [
            {"game_id": g.game_id, "name": g.name, "spins": g.spins, "wagered": money(g.wagered), "won": money(g.won)}
            for g in s.by_game
        ],
        "recent_sessions": [
            {
                "id": r.session_id,
                "name": r.name,
                "started_at": r.started_at,
                "ended_at": r.ended_at,
                "spins": r.spins,
                "staked": money(r.staked),
                "paid": money(r.paid),
                "net": money(r.net),
            }
            for r in s.recent_sessions
        ],
    }


# ---------------------------------------------------------------------------
# Technicians
# ---------------------------------------------------------------------------


def tech_games(call: Call) -> Json:
    return {
        "games": [
            {**game_info(spec), "enabled": enabled} for spec, enabled in technician.view_games(call.who, call.conn)
        ]
    }


def tech_set_enabled(call: Call) -> Json:
    game_id = _text(call, "game_id", max_len=32)
    enabled = _flag(call, "enabled")
    if enabled:
        technician.enable_game(call.who, call.conn, game_id)
    else:
        technician.disable_game(call.who, call.conn, game_id)
    return {"game_id": game_id, "enabled": enabled}


def tech_install(call: Call) -> Json:
    spec = technician.install_game_config(
        call.who, call.conn, _text(call, "config", max_len=technician.MAX_CONFIG_BYTES)
    )
    return {"game": game_info(spec)}


def tech_par(call: Call) -> Json:
    game_id = _text(call, "game_id", max_len=32)
    return {"game_id": game_id, "text": technician.view_par_sheet(call.who, call.conn, game_id)}


def tech_diagnostic(call: Call) -> Json:
    d = technician.run_system_diagnostic(call.who, call.conn)
    return {
        "games": [_figures(f, theory) for f, theory in d.games],
        "system": {
            "users_by_role": d.users_by_role,
            "locked_accounts": d.locked_accounts,
            "open_sessions": d.open_sessions,
            "credits_in_machines": money(d.credits_in_machines),
            "player_balances": money(d.player_balances),
            "daily_limits": {
                "deposit": money(accounts.DAILY_DEPOSIT_LIMIT),
                "withdraw": money(accounts.DAILY_WITHDRAW_LIMIT),
            },
            "audit_entries": d.audit_entries,
            "audit_chain": _verify(d.audit_chain),
            "database_ok": d.database_ok,
        },
    }


def tech_audit(call: Call) -> Json:
    limit = _whole(call, "limit") if "limit" in call.body else 50
    offset = _whole(call, "offset") if "offset" in call.body else 0
    rows = technician.view_audit_log(
        call.who,
        call.conn,
        _text(call, "actor", optional=True) or None,
        _text(call, "action", optional=True) or None,
        limit,
        since=_text(call, "since", optional=True) or None,
        until=_text(call, "until", optional=True) or None,
        offset=offset,
    )
    return {
        "entries": [
            {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": r["detail"]}
            for r in rows
        ],
        "actions": list(AUDIT_ACTIONS),
        "limit": limit,
        "offset": offset,
    }


def tech_verify(call: Call) -> Json:
    return _verify(technician.verify_audit_chain(call.who, call.conn))


def tech_users(call: Call) -> Json:
    return {
        "users": [
            {
                "username": r["username"],
                "role": r["role"],
                "must_change_password": bool(r["must_change_pw"]),
                "locked_until": r["locked_until"],
                "created_at": r["created_at"],
            }
            for r in technician.view_users(call.who, call.conn)
        ]
    }


def tech_create_staff(call: Call) -> Json:
    role_name = _text(call, "role", max_len=16)
    roles = {auth.Role.TECHNICIAN.value: auth.Role.TECHNICIAN, auth.Role.COMMISSIONER.value: auth.Role.COMMISSIONER}
    if role_name not in roles:
        raise BadRequest("role must be technician or commissioner")
    username = _text(call, "username", max_len=64)
    one_time = technician.add_staff(call.who, call.conn, username, roles[role_name])
    return {"username": username, "role": role_name, "one_time_password": one_time}


# ---------------------------------------------------------------------------
# Commissioners
# ---------------------------------------------------------------------------


def comm_games(call: Call) -> Json:
    return {
        "games": [
            {**game_info(spec), "enabled": enabled} for spec, enabled in commissioner.list_games(call.who, call.conn)
        ]
    }


def comm_stats(call: Call) -> Json:
    test = _flag(call, "test", False)
    analyses = commissioner.view_game_statistics(call.who, call.conn, test)
    return {
        "test": test,
        "games": [
            {
                **_figures(a.figures, a.theory),
                "histogram": [{"bucket": label, "count": count} for label, count in a.distribution.buckets],
                "mean_return": a.distribution.mean_return if a.figures.spins else None,
                "std_dev": a.distribution.std_dev if a.figures.spins else None,
            }
            for a in analyses
        ],
    }


def comm_par(call: Call) -> Json:
    game_id = _text(call, "game_id", max_len=32)
    return {"game_id": game_id, "text": commissioner.view_par_sheet(call.who, call.conn, game_id)}


def comm_test(call: Call) -> Json:
    bankroll_text = _text(call, "bankroll", max_len=32, optional=True)
    report = commissioner.run_test_session(
        call.who,
        call.conn,
        _text(call, "game_id", max_len=32),
        _amount(call, "bet"),
        _whole(call, "rounds"),
        bankroll=Money.parse(bankroll_text) if bankroll_text.strip() else None,
    )
    export = None
    if _flag(call, "export", False):
        export = str(commissioner.export_test_report(call.who, call.conn, report))
    theory = stats.theoretical(report.spec)
    shown = report.rounds[:ROUNDS_SHOWN]
    running = report.start_bankroll
    rows: list[Json] = []
    for r in shown:
        if running is not None:
            running = running - report.bet + r.payout
        rows.append(
            {
                "round": r.number,
                "spin_id": r.spin_id,
                "seed": r.seed_hex,
                "stops": list(r.stops),
                "grid": _grid(report.spec, r.stops),
                "payout": money(r.payout),
                "bankroll": None if running is None else money(running),
            }
        )
    return {
        "game_id": report.spec.id,
        "name": report.spec.name,
        "bet": money(report.bet),
        "rounds_requested": report.rounds_requested,
        "rounds_played": len(report.rounds),
        "stopped_early": report.stopped_early,
        "start_bankroll": None if report.start_bankroll is None else money(report.start_bankroll),
        "end_bankroll": None if report.end_bankroll is None else money(report.end_bankroll),
        "total_bet": money(report.wagered),
        "total_paid": money(report.paid),
        "wins": report.wins,
        "hit_rate": report.hit_rate / 100,
        "observed_rtp": report.rtp / 100,
        "theoretical_rtp": _fraction(theory.base_rtp),
        "theoretical_hit_rate": _fraction(theory.hit_rate),
        "largest_win": money(report.biggest),
        "first_spin_id": report.rounds[0].spin_id,
        "last_spin_id": report.rounds[-1].spin_id,
        "replay_checked": report.replay_checked,
        "replay_ok": report.replay_ok,
        "export": export,
        "rounds_shown": len(rows),
        "rounds": rows,
    }


def comm_replay(call: Call) -> Json:
    r = commissioner.view_spin_seeds(call.who, call.conn, _whole(call, "spin_id"))
    return {
        "spin_id": r.spin_id,
        "game_id": r.game.id,
        "name": r.game.name,
        "is_test": r.is_test,
        "created_at": r.created_at,
        "seed": r.seed_hex,
        "recorded_stops": list(r.recorded_stops),
        "derived_stops": list(r.derived_stops),
        "held_reels": list(r.held_reels),
        "grid": _grid(r.game, r.recorded_stops),
        "recorded_payout": money(r.recorded_payout),
        "recomputed_payout": money(r.recomputed_payout),
        "verified": r.verified,
    }


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

#: Routes anyone may call, signed in or not.
PUBLIC: Final[Mapping[str, Route]] = {
    "/api/login": login,
    "/api/register": register,
    "/api/logout": logout,
}

#: Routes that need a live login. Which role may use each is decided by the
#: action it calls, not here.
SIGNED_IN: Final[Mapping[str, Route]] = {
    "/api/password": change_password,
    "/api/player/games": player_games,
    "/api/player/wallet": player_wallet,
    "/api/player/cards/add": add_card,
    "/api/player/cards/remove": remove_card,
    "/api/player/deposit": deposit,
    "/api/player/withdraw": withdraw,
    "/api/player/game": current_game,
    "/api/player/buyin": buy_in,
    "/api/player/spin": spin,
    "/api/player/hold": hold,
    "/api/player/cashout": cash_out,
    "/api/player/stats": player_stats,
    "/api/tech/games": tech_games,
    "/api/tech/games/enable": tech_set_enabled,
    "/api/tech/games/install": tech_install,
    "/api/tech/par": tech_par,
    "/api/tech/diagnostic": tech_diagnostic,
    "/api/tech/audit": tech_audit,
    "/api/tech/audit/verify": tech_verify,
    "/api/tech/users": tech_users,
    "/api/tech/staff/create": tech_create_staff,
    "/api/comm/games": comm_games,
    "/api/comm/stats": comm_stats,
    "/api/comm/par": comm_par,
    "/api/comm/test": comm_test,
    "/api/comm/replay": comm_replay,
}
