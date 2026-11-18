"""The web server end to end, over real HTTP on 127.0.0.1.

The server runs in a thread with its own SQLite connection, as it does in
production; the tests talk to it with http.client so every header, including
Host, is under the test's control.
"""

from __future__ import annotations

import http.client
import json
import re
import threading
from datetime import datetime, timezone
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from slotmachine import audit, auth, storage
from slotmachine.__main__ import startup
from slotmachine.cli.menus import UNEXPECTED
from slotmachine.web import api
from slotmachine.web.server import CSP, MAX_BODY, STATIC, CasinoServer
from slotmachine.web.sessions import ABSOLUTE_SECONDS, IDLE_SECONDS, MAX_SESSIONS, LoginStore

PLAYER_PASSWORD = "a long player test phrase"
NEW_STAFF_PASSWORD = "a long replacement staff phrase"
FUTURE_YEAR = datetime.now(timezone.utc).year + 3


@dataclass
class Reply:
    status: int
    headers: http.client.HTTPMessage
    raw: bytes

    @property
    def data(self) -> dict:
        return json.loads(self.raw.decode("utf-8"))


@dataclass
class Browser:
    """One browser: its cookie jar of one, and the CSRF value its page holds."""

    port: int
    sid: str | None = None
    csrf: str | None = None
    seen: list[bytes] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        *,
        host: str | None = None,
    ) -> Reply:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Host": host or f"127.0.0.1:{self.port}"}
        if self.sid:
            sent["Cookie"] = f"casino_sid={self.sid}"
        sent.update(headers or {})
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in sent.items():
                conn.putheader(name, value)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            response = conn.getresponse()
            reply = Reply(response.status, response.headers, response.read())
        finally:
            conn.close()
        cookie = reply.headers.get("Set-Cookie")
        if cookie:
            match = re.match(r"casino_sid=([^;]*);", cookie)
            assert match is not None
            self.sid = match.group(1) or None
        self.seen.append(reply.raw)
        return reply

    def post(self, path: str, body: object = None, *, csrf: bool = True, extra: dict[str, str] | None = None) -> Reply:
        headers = {"Content-Type": "application/json", "X-Casino": "1"}
        if csrf and self.csrf:
            headers["X-CSRF"] = self.csrf
        headers.update(extra or {})
        reply = self.request("POST", path, json.dumps(body or {}).encode(), headers)
        if reply.status == 200 and "csrf" in reply.data:
            self.csrf = reply.data["csrf"]
        return reply

    def ok(self, path: str, body: object = None) -> dict:
        reply = self.post(path, body)
        assert reply.status == 200, (path, reply.status, reply.raw)
        return reply.data

    def login(self, username: str, password: str) -> dict:
        return self.ok("/api/login", {"username": username, "password": password})


@dataclass
class Casino:
    port: int
    db: Path
    bootstrap_password: str

    def browser(self) -> Browser:
        return Browser(self.port)

    def player(self, name: str = "loser9527") -> Browser:
        b = self.browser()
        b.ok("/api/register", {"username": name, "password": PLAYER_PASSWORD})
        b.login(name, PLAYER_PASSWORD)
        return b

    def technician(self) -> Browser:
        b = self.browser()
        b.login("tech-admin", self.bootstrap_password)
        b.ok("/api/password", {"current": self.bootstrap_password, "new": NEW_STAFF_PASSWORD})
        return b

    def commissioner(self, tech: Browser, name: str = "regulator") -> Browser:
        made = tech.ok("/api/tech/staff/create", {"username": name, "role": "commissioner"})
        b = self.browser()
        b.login(name, made["one_time_password"])
        b.ok("/api/password", {"current": made["one_time_password"], "new": NEW_STAFF_PASSWORD})
        return b


@pytest.fixture()
def casino(tmp_path: Path) -> Generator[Casino, None, None]:
    db = tmp_path / "casino.db"
    ready = threading.Event()
    box: dict[str, object] = {}

    def run() -> None:
        conn = storage.connect(db)  # made in the server's own thread, as in production
        try:
            box["password"] = startup(conn)
            server = CasinoServer(conn, 0, quiet=True)
            box["server"] = server
            ready.set()
            server.serve_forever(poll_interval=0.05)
            server.server_close()
        finally:
            conn.close()
            ready.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(30)
    server = box["server"]
    assert isinstance(server, CasinoServer)
    try:
        yield Casino(server.server_address[1], db, str(box["password"]))
    finally:
        server.shutdown()
        thread.join(10)


def db_conn(casino: Casino):
    return storage.connect(casino.db)


# ---------------------------------------------------------------------------
# Serving pages
# ---------------------------------------------------------------------------


def test_pages_are_served_with_security_headers(casino):
    b = casino.browser()
    reply = b.request("GET", "/")
    assert reply.status == 200
    assert reply.headers["Content-Type"].startswith("text/html")
    assert reply.headers["Content-Security-Policy"] == CSP
    assert "script-src 'self'" in CSP and "unsafe-inline" not in CSP and "frame-ancestors 'none'" in CSP
    assert reply.headers["X-Content-Type-Options"] == "nosniff"
    assert reply.headers["X-Frame-Options"] == "DENY"
    assert reply.headers["Referrer-Policy"] == "no-referrer"
    assert "Python" not in reply.headers["Server"]
    for path, (_, kind) in STATIC.items():
        page = b.request("GET", path)
        assert page.status == 200, path
        assert page.headers["Content-Type"] == kind


@pytest.mark.parametrize(
    "path",
    ["/../schema.sql", "/static/../../schema.sql", "/%2e%2e/schema.sql", "/casino.js/..", "/admin", "/static/casino.js", "/api"],
)
def test_only_listed_files_are_served(casino, path):
    reply = casino.browser().request("GET", path)
    assert reply.status == 404
    assert b"CREATE TABLE" not in reply.raw


def test_head_has_no_body(casino):
    reply = casino.browser().request("HEAD", "/")
    assert reply.status == 200 and reply.raw == b""


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH", "OPTIONS"])
def test_other_methods_refused(casino, method):
    assert casino.browser().request(method, "/api/login").status == 405


# ---------------------------------------------------------------------------
# Request checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8888", "127.0.0.1", "10.0.0.5:80"])
def test_wrong_host_refused(casino, host):
    """A DNS-rebinding page arrives with its own host name and is turned away."""
    b = casino.browser()
    assert b.request("GET", "/", host=host).status == 421
    assert b.request("GET", "/api/me", host=host).status == 421


def test_localhost_name_accepted(casino):
    assert casino.browser().request("GET", "/", host=f"localhost:{casino.port}").status == 200


def test_post_needs_the_marker_header(casino):
    b = casino.browser()
    reply = b.request(
        "POST", "/api/login", json.dumps({"username": "x", "password": "y"}).encode(), {"Content-Type": "application/json"}
    )
    assert reply.status == 403


@pytest.mark.parametrize(
    "header,value",
    [("Origin", "http://evil.example"), ("Origin", "null"), ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Site", "same-site")],
)
def test_cross_site_posts_refused(casino, header, value):
    b = casino.player()
    reply = b.post("/api/player/wallet", extra={header: value})
    assert reply.status == 403


def test_same_origin_headers_accepted(casino):
    b = casino.player()
    reply = b.post("/api/player/wallet", extra={"Origin": f"http://127.0.0.1:{casino.port}", "Sec-Fetch-Site": "same-origin"})
    assert reply.status == 200


def test_json_content_type_required(casino):
    b = casino.browser()
    reply = b.request("POST", "/api/login", b"username=x", {"Content-Type": "application/x-www-form-urlencoded", "X-Casino": "1"})
    assert reply.status == 415


def test_body_size_limit(casino):
    b = casino.browser()
    body = b'{"username": "' + b"a" * MAX_BODY + b'"}'
    reply = b.request("POST", "/api/login", body, {"Content-Type": "application/json", "X-Casino": "1"})
    assert reply.status == 413


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, 2]", b'{"amount": 12.5}', b'{"amount": NaN}', b'{"amount": 1e3}', b"\xff\xfe"],
    ids=["garbage", "array", "fraction", "nan", "exponent", "bad-utf8"],
)
def test_malformed_bodies_refused(casino, body):
    b = casino.player()
    reply = b.request(
        "POST", "/api/player/deposit", body, {"Content-Type": "application/json", "X-Casino": "1", "X-CSRF": b.csrf or ""}
    )
    assert reply.status == 400
    assert b"Traceback" not in reply.raw


def test_signed_in_routes_need_a_login(casino):
    for path in api.SIGNED_IN:
        assert casino.browser().post(path).status == 401, path


def test_csrf_value_required_once_signed_in(casino):
    b = casino.player()
    assert b.post("/api/player/wallet", csrf=False).status == 403
    assert b.post("/api/player/wallet", extra={"X-CSRF": "0" * 64}).status == 403
    assert b.post("/api/player/wallet").status == 200
    # Logging out (or in again) from a signed-in browser needs it too.
    assert b.post("/api/logout", csrf=False).status == 403


def test_cookie_is_http_only_and_strict(casino):
    b = casino.browser()
    b.ok("/api/register", {"username": "alice", "password": PLAYER_PASSWORD})
    reply = b.post("/api/login", {"username": "alice", "password": PLAYER_PASSWORD})
    cookie = reply.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert f"Max-Age={ABSOLUTE_SECONDS}" in cookie
    assert "alice" not in cookie and "player" not in cookie
    assert reply.headers["Cache-Control"] == "no-store"


def test_login_failure_is_generic(casino):
    b = casino.browser()
    b.ok("/api/register", {"username": "alice", "password": PLAYER_PASSWORD})
    wrong = b.post("/api/login", {"username": "alice", "password": "not the right phrase"})
    missing = b.post("/api/login", {"username": "nobody", "password": "not the right phrase"})
    assert wrong.status == missing.status == 400
    assert wrong.data == missing.data == {"error": auth.LOGIN_FAILURE_MESSAGE}
    assert b.sid is None


def test_unexpected_errors_reveal_nothing(casino, monkeypatch):
    def broken(call):
        raise RuntimeError("C:\\secret\\path.py line 42 internal detail")

    monkeypatch.setitem(api.SIGNED_IN, "/api/player/stats", broken)
    reply = casino.player().post("/api/player/stats")
    assert reply.status == 500
    assert reply.data == {"error": UNEXPECTED}
    assert b"secret" not in reply.raw and b"Traceback" not in reply.raw


def test_logout_ends_the_login(casino):
    b = casino.player()
    old = b.sid
    b.ok("/api/logout")
    assert b.sid is None
    b.sid = old
    assert b.post("/api/player/wallet").status == 401


# ---------------------------------------------------------------------------
# A whole visit, as each role
# ---------------------------------------------------------------------------


def test_player_full_flow(casino):
    b = casino.player()
    me = b.request("GET", "/api/me").data
    assert me["signed_in"] and me["role"] == "player" and not me["must_change_password"]

    games = b.ok("/api/player/games")["games"]
    assert {g["id"] for g in games} == {"classic_sevens", "five_line_deluxe"}

    card = b.ok("/api/player/cards/add", {"number": "4242424242424242", "exp_month": 12, "exp_year": FUTURE_YEAR, "cvv": "123"})
    assert card["last4"] == "4242"
    assert b.ok("/api/player/deposit", {"method_id": card["id"], "amount": "100.00"})["balance"]["cents"] == 10_000

    assert b.ok("/api/player/game")["game"] is None
    assert b.ok("/api/player/buyin", {"game_id": "classic_sevens", "amount": "10.00"})["credits"]["cents"] == 1_000
    current = b.ok("/api/player/game")
    assert current["game"]["id"] == "classic_sevens" and current["credits"]["cents"] == 1_000

    spins = 0
    held = False
    for _ in range(12):
        outcome = b.ok("/api/player/spin", {"bet": "0.25"})
        spins += 1
        assert set(outcome) >= {"grid", "stops", "wins", "payout", "credits", "holds_left"}
        if not held and outcome["holds_left"] > 0 and outcome["credits"]["cents"] >= outcome["respin_cost"]["cents"]:
            again = b.ok("/api/player/hold", {"reels": [0]})
            assert again["held"] == [0] and again["stops"][0] == outcome["stops"][0]
            spins += 1
            held = True
    assert held

    done = b.ok("/api/player/cashout")
    assert done["spins"] == spins
    assert b.ok("/api/player/game")["game"] is None
    balance = b.ok("/api/player/withdraw", {"method_id": card["id"], "amount": "1.00"})["balance"]["cents"]

    stats = b.ok("/api/player/stats")
    assert stats["spins"] == spins and stats["sessions"] == 1
    assert stats["net"]["cents"] == stats["won"]["cents"] - stats["wagered"]["cents"]
    assert balance == 10_000 - 100 + stats["net"]["cents"]

    # The seed is recorded but never reaches a player.
    assert not any(b"seed" in raw for raw in b.seen)
    with db_conn(casino) as c:
        assert c.execute("SELECT COUNT(*) FROM spins WHERE seed IS NOT NULL").fetchone()[0] == spins
        assert audit.verify_chain(c).ok
        assert "4242424242424242" not in json.dumps([list(r) for r in c.execute("SELECT * FROM payment_methods")])


def test_player_input_errors_are_messages(casino):
    b = casino.player()
    assert b.post("/api/player/spin", {"bet": "0.25"}).data == {"error": "buy in to a game first"}
    assert b.post("/api/player/deposit", {"method_id": True, "amount": "5.00"}).status == 400
    assert b.post("/api/player/deposit", {"method_id": 1, "amount": "-5.00"}).status == 400
    assert b.post("/api/player/deposit", {"method_id": 2**60, "amount": "5.00"}).status == 400
    assert b.post("/api/player/hold", {"reels": [0]}).status == 400
    assert b.post("/api/player/buyin", {"game_id": "nope", "amount": "5.00"}).status == 400


def test_forced_password_change_over_http(casino):
    b = casino.browser()
    me = b.login("tech-admin", casino.bootstrap_password)
    assert me["must_change_password"]
    assert b.post("/api/tech/games").status == 403
    old_sid = b.sid
    me = b.ok("/api/password", {"current": casino.bootstrap_password, "new": NEW_STAFF_PASSWORD})
    assert not me["must_change_password"]
    assert b.sid != old_sid  # a new id after the change
    assert b.ok("/api/tech/games")["games"]
    stale = Browser(casino.port, sid=old_sid, csrf=b.csrf)
    assert stale.post("/api/tech/games").status == 401


def test_bootstrap_password_never_sent(casino):
    b = casino.technician()
    for path in ("/api/tech/users", "/api/tech/audit", "/api/tech/diagnostic"):
        b.ok(path)
    assert not any(casino.bootstrap_password.encode() in raw for raw in b.seen[1:])


ROLE_PROBES = {
    "player": ["/api/player/wallet", "/api/player/stats", "/api/player/games"],
    "technician": ["/api/tech/games", "/api/tech/diagnostic", "/api/tech/audit", "/api/tech/audit/verify", "/api/tech/users"],
    "commissioner": ["/api/comm/games", "/api/comm/stats"],
}


def test_roles_cannot_use_each_others_routes(casino):
    tech = casino.technician()
    comm = casino.commissioner(tech)
    player = casino.player()
    browsers = {"player": player, "technician": tech, "commissioner": comm}
    for role, b in browsers.items():
        for owner, paths in ROLE_PROBES.items():
            for path in paths:
                status = b.post(path).status
                assert status == (200 if owner == role else 403), (role, path, status)
    assert player.post("/api/comm/test", {"game_id": "classic_sevens", "rounds": 5, "bet": "0.25", "bankroll": "10.00"}).status == 403
    assert comm.post("/api/tech/staff/create", {"username": "sneaky", "role": "technician"}).status == 403
    assert player.post("/api/tech/games/enable", {"game_id": "classic_sevens", "enabled": False}).status == 403
    with db_conn(casino) as c:
        assert storage.get_user_by_username(c, "sneaky") is None
        assert storage.get_game(c, "classic_sevens")["enabled"] == 1
        denied = audit.read(c, action=audit.Action.ACCESS_DENIED, limit=500)
        assert len(denied) >= 3


def test_technician_tools_over_http(casino):
    tech = casino.technician()
    assert tech.ok("/api/tech/games/enable", {"game_id": "five_line_deluxe", "enabled": False})["enabled"] is False
    player = casino.player()
    assert [g["id"] for g in player.ok("/api/player/games")["games"]] == ["classic_sevens"]
    # The last playable game cannot be switched off.
    assert tech.post("/api/tech/games/enable", {"game_id": "classic_sevens", "enabled": False}).status == 400
    par = tech.ok("/api/tech/par", {"game_id": "classic_sevens"})
    assert "PROBABILITY ACCOUNTING REPORT" in par["text"]
    log = tech.ok("/api/tech/audit", {"action": "game.disabled"})
    assert [e["detail"] for e in log["entries"]] == ["game=five_line_deluxe"]
    assert "audit.verified" in log["actions"]
    assert tech.post("/api/tech/audit", {"since": "last tuesday"}).status == 400
    assert tech.post("/api/tech/audit", {"limit": 0}).status == 400
    assert tech.ok("/api/tech/audit", {"since": "2000-01-01", "until": "2999-12-31", "limit": 2, "offset": 1})["entries"]
    verified = tech.ok("/api/tech/audit/verify")
    assert verified["ok"] and verified["entries_checked"] > 0
    assert tech.ok("/api/tech/audit", {"action": "audit.verified"})["entries"]  # the check itself is logged
    assert tech.post("/api/tech/staff/create", {"username": "x2", "role": "player"}).status == 400
    users = tech.ok("/api/tech/users")["users"]
    assert {u["role"] for u in users} == {"technician", "player"}
    assert all("pw_hash" not in u for u in users)


def _add_card_and_deposit(player, amount):
    card = player.ok(
        "/api/player/cards/add", {"number": "4242424242424242", "exp_month": 12, "exp_year": FUTURE_YEAR, "cvv": "123"}
    )
    player.ok("/api/player/deposit", {"method_id": card["id"], "amount": amount})
    return card


def test_diagnostic_over_http(casino):
    tech = casino.technician()
    player = casino.player()
    _add_card_and_deposit(player, "50.00")
    player.ok("/api/player/buyin", {"game_id": "classic_sevens", "amount": "5.00"})
    player.ok("/api/player/spin", {"bet": "0.25"})
    d = tech.ok("/api/tech/diagnostic")
    classic = {g["game_id"]: g for g in d["games"]}["classic_sevens"]
    assert classic["spins"] == 1 and classic["players"] == 1
    assert 0.9 < classic["theoretical_rtp"] < 1
    system = d["system"]
    assert system["open_sessions"] == 1 and system["database_ok"] and system["audit_chain"]["ok"]
    assert system["users_by_role"] == {"player": 1, "technician": 1}


def _shipped_config(game_id):
    from slotmachine.game.spec import dump_spec
    from slotmachine.tools.rtpcalc import load_shipped_specs

    return json.loads(dump_spec({s.id: s for s in load_shipped_specs()}[game_id]))


def test_install_game_over_http(casino):
    tech = casino.technician()
    config = _shipped_config("classic_sevens")
    config.update({"id": "lucky_bells", "name": "Lucky Bells"})
    reply = tech.ok("/api/tech/games/install", {"config": json.dumps(config)})
    assert reply["game"]["id"] == "lucky_bells"
    games = {g["id"]: g for g in tech.ok("/api/tech/games")["games"]}
    assert games["lucky_bells"]["enabled"] is False
    assert tech.post("/api/tech/games/install", {"config": json.dumps(config)}).status == 400
    wide = dict(config, id="too_wide", reels=[config["reels"][0]] * 6, paylines=[[0] * 6], holds=dict(config["holds"], enabled=False))
    refused = tech.post("/api/tech/games/install", {"config": json.dumps(wide)})
    assert refused.status == 400 and "at most 5 reels" in refused.data["error"]
    assert tech.post("/api/tech/games/install", {"config": "not json"}).status == 400


def test_commissioner_test_session_over_http(casino):
    comm = casino.commissioner(casino.technician())
    report = comm.ok("/api/comm/test", {"game_id": "five_line_deluxe", "rounds": 25, "bet": "1.00"})
    assert report["rounds_played"] == 25 and report["replay_ok"] and report["replay_checked"] == 25
    assert report["start_bankroll"] is None and report["export"] is None
    assert all(re.fullmatch(r"[0-9a-f]{64}", r["seed"]) for r in report["rounds"])
    assert len(report["rounds"][0]["grid"]) == 3
    tested = {g["game_id"]: g for g in comm.ok("/api/comm/stats", {"test": True})["games"]}
    live = {g["game_id"]: g for g in comm.ok("/api/comm/stats")["games"]}
    assert tested["five_line_deluxe"]["spins"] == 25 and live["five_line_deluxe"]["spins"] == 0
    assert sum(b["count"] for b in tested["five_line_deluxe"]["histogram"]) == 25
    assert comm.post("/api/comm/test", {"game_id": "five_line_deluxe", "rounds": 25, "bet": 1}).status == 400


def test_commissioner_bankroll_and_export(casino, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # reports/ is written under the current directory
    comm = casino.commissioner(casino.technician())
    report = comm.ok(
        "/api/comm/test",
        {"game_id": "five_line_deluxe", "rounds": 100_000, "bet": "25.00", "bankroll": "50.00", "export": True},
    )
    assert report["stopped_early"] and report["rounds_played"] < 100_000
    assert report["end_bankroll"]["cents"] < 2_500
    assert report["start_bankroll"]["cents"] == 5_000
    exported = tmp_path / report["export"]
    lines = exported.read_text(encoding="utf-8").splitlines()
    assert len(lines) == report["rounds_played"] + 1
    assert comm.post("/api/comm/test", {"game_id": "five_line_deluxe", "rounds": 5, "bet": "25.00", "bankroll": "1.00"}).status == 400


def test_replay_over_http(casino):
    tech = casino.technician()
    comm = casino.commissioner(tech)
    player = casino.player()
    _add_card_and_deposit(player, "50.00")
    player.ok("/api/player/buyin", {"game_id": "classic_sevens", "amount": "10.00"})
    first = player.ok("/api/player/spin", {"bet": "0.25"})
    held = player.ok("/api/player/hold", {"reels": [1]})
    live = comm.ok("/api/comm/replay", {"spin_id": first["spin_id"]})
    assert live["verified"] and not live["is_test"] and re.fullmatch(r"[0-9a-f]{64}", live["seed"])
    again = comm.ok("/api/comm/replay", {"spin_id": held["spin_id"]})
    assert again["verified"] and again["recorded_stops"][1] == first["stops"][1]
    assert comm.post("/api/comm/replay", {"spin_id": 999_999}).status == 400
    assert player.post("/api/comm/replay", {"spin_id": first["spin_id"]}).status == 403
    assert tech.post("/api/comm/replay", {"spin_id": first["spin_id"]}).status == 403


def test_logout_cashes_out_an_open_game(casino):
    player = casino.player()
    _add_card_and_deposit(player, "20.00")
    player.ok("/api/player/buyin", {"game_id": "classic_sevens", "amount": "5.00"})
    bye = player.ok("/api/logout")
    assert bye["cashed_out"]["cents"] == 500
    player.login("loser9527", PLAYER_PASSWORD)
    assert player.ok("/api/player/game")["game"] is None
    assert player.ok("/api/player/wallet")["balance"]["cents"] == 2_000


def test_tamper_is_reported_over_http(casino):
    tech = casino.technician()
    with db_conn(casino) as c:
        c.execute("DROP TRIGGER audit_log_is_immutable")
        c.execute("UPDATE audit_log SET actor = 'nobody' WHERE id = 2")
    result = tech.ok("/api/tech/audit/verify")
    assert not result["ok"] and result["first_bad_id"] == 2


# ---------------------------------------------------------------------------
# The login table on its own
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def who() -> auth.Session:
    return auth.Session(1, "alice", auth.Role.PLAYER)


def test_idle_and_absolute_expiry():
    clock = Clock()
    store = LoginStore(clock)
    login = store.start(who())
    clock.now += IDLE_SECONDS - 1
    assert store.find(login.sid) is not None
    clock.now += IDLE_SECONDS + 1
    assert store.find(login.sid) is None

    login = store.start(who())
    for _ in range(ABSOLUTE_SECONDS // (IDLE_SECONDS // 2) + 1):
        clock.now += IDLE_SECONDS // 2
        store.find(login.sid)
    assert store.find(login.sid) is None


def test_renew_replaces_the_id_and_keeps_the_deadline():
    clock = Clock()
    store = LoginStore(clock)
    first = store.start(who())
    clock.now += 100
    second = store.renew(first)
    assert second.sid != first.sid and second.csrf != first.csrf
    assert store.find(first.sid) is None
    assert second.created == first.created


def test_ids_are_long_and_unrelated():
    store = LoginStore()
    logins = [store.start(who()) for _ in range(50)]
    assert all(len(l.sid) == 64 and len(l.csrf) == 64 for l in logins)
    assert len({l.sid for l in logins} | {l.csrf for l in logins}) == 100
    assert "sid" not in repr(logins[0]) and logins[0].sid not in repr(logins[0])


def test_table_is_bounded():
    store = LoginStore()
    for _ in range(MAX_SESSIONS + 25):
        store.start(who())
    assert len(store) <= MAX_SESSIONS
