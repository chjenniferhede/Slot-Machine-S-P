"""The JSON API behind the pages: one small function per route.

Each route reads its fields from the request body, checks their types, and
calls one role-gated action in ``accounts``, ``payment``, ``technician`` or
``commissioner``. Routes make no access decisions of their own: a player who
calls a technician route reaches ``technician.*``, and ``auth.require_role``
refuses them there, exactly as it would refuse a direct call. The menus a
page shows are a convenience, never the control.

Values from the browser are untrusted. Amounts arrive as text such as
"12.50" and go through ``Money.parse``; JSON numbers with a fraction never
get this far (the server refuses them while parsing). Ids are whole numbers,
and bools are not accepted as numbers.

Seeds never appear in a player's response. Only the commissioner's test
report includes them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final

from slotmachine import accounts, auth, commissioner, payment, stats, technician
from slotmachine.audit import VerifyResult
from slotmachine.game.spec import GameSpec
from slotmachine.money import Money
from slotmachine.tools import rtpcalc
from slotmachine.web.sessions import LoginStore, WebLogin

TEXT_MAX: Final[int] = 128
PASSWORD_FIELD_MAX: Final[int] = 1024  # auth enforces the real policy
WHOLE_MAX: Final[int] = 2**53  # ids and counts; well inside SQLite's INTEGER


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


def _flag(call: Call, field: str) -> bool:
    value = call.body.get(field)
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


def game_info(spec: GameSpec) -> Json:
    base, optimal = stats.spec_rtp(spec)
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
        "rtp": base,
        "rtp_with_holds": optimal,
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


def _game_stats(s: stats.GameStats) -> Json:
    return {
        "game_id": s.game_id,
        "name": s.name,
        "enabled": s.enabled,
        "spins": s.spins,
        "wins": s.wins,
        "losses": s.losses,
        "wagered": money(s.wagered),
        "paid": money(s.paid),
        "house_net": money(s.house_net),
        "largest_win": money(s.largest_win),
        "unique_players": s.unique_players,
        "active_days": s.active_days,
        "plays_per_day": s.plays_per_day,
        "observed_rtp": s.observed_rtp,
        "hit_rate": s.hit_rate,
        "theoretical_rtp": s.theoretical_rtp,
        "optimal_hold_rtp": s.optimal_hold_rtp,
    }


def _par_sheet(sheet: rtpcalc.ParSheet) -> Json:
    return {
        "game": game_info(sheet.spec),
        "reel_shares": [
            [{"symbol": s.symbol, "positions": s.positions, "chance": s.chance} for s in reel]
            for reel in sheet.reel_shares
        ],
        "pay_lines": [
            {
                "symbol": line.symbol,
                "count": line.count,
                "multiplier": line.multiplier,
                "chance": line.chance,
                "rtp_share": line.rtp_share,
            }
            for line in sheet.pay_lines
        ],
        "base_rtp": sheet.base_rtp,
        "optimal_hold_rtp": sheet.optimal_hold_rtp,
        "hit_frequency": sheet.hit_frequency,
        "hit_frequency_is_estimate": sheet.hit_frequency_is_estimate,
        "text": rtpcalc.render(sheet),
    }


def _verify(result: VerifyResult) -> Json:
    return {
        "ok": result.ok,
        "entries_checked": result.entries_checked,
        "first_bad_id": result.first_bad_id,
        "reason": result.reason,
    }


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
    if call.login is not None:
        call.store.end(call.login.sid)
    call.ended = True
    return {"signed_in": False}


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
    s = accounts.own_statistics(call.who, call.conn)
    return {
        "sessions": s.sessions,
        "spins": s.spins,
        "wins": s.wins,
        "win_rate": s.win_rate,
        "wagered": money(s.wagered),
        "won": money(s.won),
        "net": money(s.net),
        "biggest_payout": money(s.biggest_payout),
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
                "net": money(r.paid - r.staked),
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
            {**game_info(g.spec), "enabled": g.enabled, "created_at": g.created_at}
            for g in technician.list_games(call.who, call.conn)
        ]
    }


def tech_set_enabled(call: Call) -> Json:
    changed = technician.set_game_enabled(
        call.who, call.conn, _text(call, "game_id", max_len=32), _flag(call, "enabled")
    )
    return {"changed": changed}


def tech_install(call: Call) -> Json:
    spec = technician.install_game(call.who, call.conn, _text(call, "config", max_len=technician.MAX_CONFIG_CHARS))
    return {"game": game_info(spec)}


def tech_par(call: Call) -> Json:
    return _par_sheet(technician.par_sheet(call.who, call.conn, _text(call, "game_id", max_len=32)))


def tech_diagnostic(call: Call) -> Json:
    return {"games": [_game_stats(s) for s in technician.system_diagnostic(call.who, call.conn)]}


def tech_audit(call: Call) -> Json:
    limit = _whole(call, "limit") if "limit" in call.body else 50
    offset = _whole(call, "offset") if "offset" in call.body else 0
    entries = technician.read_audit_log(
        call.who,
        call.conn,
        actor=_text(call, "actor", optional=True) or None,
        action=_text(call, "action", optional=True) or None,
        since=_text(call, "since", optional=True) or None,
        until=_text(call, "until", optional=True) or None,
        limit=limit,
        offset=offset,
    )
    return {
        "entries": [
            {"id": e.id, "ts": e.ts, "actor": e.actor, "action": e.action, "detail": e.detail}
            for e in entries
        ],
        "limit": limit,
        "offset": offset,
    }


def tech_verify(call: Call) -> Json:
    return _verify(technician.verify_audit_chain(call.who, call.conn))


def tech_staff(call: Call) -> Json:
    return {
        "staff": [
            {
                "username": s.username,
                "role": s.role,
                "must_change_password": s.must_change_password,
                "locked_until": s.locked_until,
                "created_at": s.created_at,
            }
            for s in technician.list_staff(call.who, call.conn)
        ]
    }


def tech_create_staff(call: Call) -> Json:
    role_name = _text(call, "role", max_len=16)
    roles = {role.value: role for role in technician.STAFF_ROLES}
    if role_name not in roles:
        raise BadRequest("role must be technician or commissioner")
    created = technician.create_staff_account(call.who, call.conn, _text(call, "username", max_len=64), roles[role_name])
    return {"username": created.username, "role": created.role, "one_time_password": created.one_time_password}


# ---------------------------------------------------------------------------
# Commissioners
# ---------------------------------------------------------------------------


def comm_games(call: Call) -> Json:
    return {
        "games": [
            {**game_info(g.spec), "enabled": g.enabled} for g in commissioner.list_games(call.who, call.conn)
        ]
    }


def comm_analyze(call: Call) -> Json:
    test = _flag(call, "test") if "test" in call.body else False
    analysis = commissioner.analyze_game(call.who, call.conn, _text(call, "game_id", max_len=32), test=test)
    return {
        "stats": _game_stats(analysis.stats),
        "histogram": [{"bucket": label, "count": count} for label, count in analysis.histogram],
        "return_stdev": analysis.return_stdev,
        "test": test,
    }


def comm_par(call: Call) -> Json:
    return _par_sheet(commissioner.par_sheet(call.who, call.conn, _text(call, "game_id", max_len=32)))


def comm_test(call: Call) -> Json:
    report = commissioner.run_test_session(
        call.who,
        call.conn,
        _text(call, "game_id", max_len=32),
        _whole(call, "rounds"),
        _amount(call, "bet"),
        _amount(call, "bankroll"),
    )
    return {
        "game_id": report.game_id,
        "name": report.name,
        "rounds_requested": report.rounds_requested,
        "rounds_played": report.rounds_played,
        "stopped_early": report.stopped_early,
        "bet": money(report.bet),
        "start_bankroll": money(report.start_bankroll),
        "end_bankroll": money(report.end_bankroll),
        "total_bet": money(report.total_bet),
        "total_paid": money(report.total_paid),
        "hits": report.hits,
        "hit_rate": report.hit_rate,
        "observed_rtp": report.observed_rtp,
        "theoretical_rtp": report.theoretical_rtp,
        "optimal_hold_rtp": report.optimal_hold_rtp,
        "largest_win": money(report.largest_win),
        "first_spin_id": report.first_spin_id,
        "last_spin_id": report.last_spin_id,
        "replay_checked": report.replay_checked,
        "replay_ok": report.replay_ok,
        "rounds_shown": len(report.rounds),
        "rounds": [
            {
                "round": r.round,
                "spin_id": r.spin_id,
                "seed": r.seed_hex,
                "stops": list(r.stops),
                "grid": [list(row) for row in r.grid],
                "bet": money(r.bet),
                "payout": money(r.payout),
                "bankroll": money(r.bankroll),
            }
            for r in report.rounds
        ],
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
    "/api/tech/staff": tech_staff,
    "/api/tech/staff/create": tech_create_staff,
    "/api/comm/games": comm_games,
    "/api/comm/analyze": comm_analyze,
    "/api/comm/par": comm_par,
    "/api/comm/test": comm_test,
}
