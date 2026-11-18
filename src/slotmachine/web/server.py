"""A small local web server for the pages and the JSON API, on the standard library.

It is built to be reached from this machine only, by one browser user at a
time, and every choice below follows from that:

- It listens on 127.0.0.1 and nowhere else, and refuses a request whose Host
  header is not this server. A web page elsewhere cannot point a DNS name at
  127.0.0.1 and talk to it (DNS rebinding).
- One thread, one SQLite connection, requests in turn. That matches the rest
  of the system's assumption of one running copy per database, and there is
  no shared connection across threads to get wrong. Each connection is closed
  after its response (HTTP/1.0) and given a short timeout, so one idle or slow
  client cannot hold the server.
- Every change is a POST with a JSON body, the ``X-Casino`` header, and, once
  signed in, the login's CSRF value in ``X-CSRF``. A cross-site form cannot
  send JSON or custom headers, and the cookie is ``SameSite=Strict`` as well.
  An ``Origin`` or ``Sec-Fetch-Site`` header that says the request came from
  another site is refused outright.
- Pages may load scripts and styles from this server only (a strict Content
  Security Policy), may not be framed, and are never cached with an answer.
- An expected refusal comes back as its message. Anything unexpected comes
  back as one fixed line and never as a traceback (CWE-209). The access log
  records the method, the path without its query, and the status: never a
  body, so a card number or password typed into a form is not written down.

``http.server`` is not meant for the open internet, and this server is not
put there. It has no outbound requests at all, which is the A10 (SSRF)
answer: nothing here fetches a URL.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib import resources
from typing import Final, NoReturn

from slotmachine.auth import AccessDenied, AuthError
from slotmachine.cli.menus import UNEXPECTED, USER_ERRORS
from slotmachine.web import api
from slotmachine.web.sessions import ABSOLUTE_SECONDS, LoginStore, WebLogin

HOST: Final[str] = "127.0.0.1"
DEFAULT_PORT: Final[int] = 8888
MAX_BODY: Final[int] = 64 * 1024
DRAIN_MAX: Final[int] = 1024 * 1024
COOKIE_NAME: Final[str] = "casino_sid"
CSRF_HEADER: Final[str] = "X-CSRF"
MARKER_HEADER: Final[str] = "X-Casino"

CSP: Final[str] = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;"
    " connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none';"
    " frame-ancestors 'none'"
)
SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("Cache-Control", "no-store"),
)

#: Every file the server will send, by URL path. Nothing else is served,
#: and no part of a request path is ever joined onto a file name.
STATIC: Final[dict[str, tuple[str, str]]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/admin.html": ("admin.html", "text/html; charset=utf-8"),
    "/common.js": ("common.js", "text/javascript; charset=utf-8"),
    "/strings.js": ("strings.js", "text/javascript; charset=utf-8"),
    "/casino.js": ("casino.js", "text/javascript; charset=utf-8"),
    "/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
    "/casino.css": ("casino.css", "text/css; charset=utf-8"),
    "/admin.css": ("admin.css", "text/css; charset=utf-8"),
    "/logo.svg": ("logo.svg", "image/svg+xml"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.svg", "image/svg+xml"),
}


def load_static() -> dict[str, tuple[bytes, str]]:
    """Read every served file once, at startup, from inside the package."""
    folder = resources.files("slotmachine.web").joinpath("static")
    return {path: (folder.joinpath(name).read_bytes(), kind) for path, (name, kind) in STATIC.items()}


class _Refused(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class CasinoServer(HTTPServer):
    """The listening socket, plus what every request needs."""

    def __init__(self, conn: sqlite3.Connection, port: int = DEFAULT_PORT, *, quiet: bool = False) -> None:
        self.conn = conn
        self.logins = LoginStore()
        self.static = load_static()
        self.quiet = quiet
        super().__init__((HOST, port), CasinoHandler)
        bound = self.server_address[1]
        self.allowed_hosts = frozenset({f"127.0.0.1:{bound}", f"localhost:{bound}"})
        self.allowed_origins = frozenset(f"http://{host}" for host in self.allowed_hosts)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/"


class CasinoHandler(BaseHTTPRequestHandler):
    server: CasinoServer
    server_version = "Casino"  # not "BaseHTTP/0.6 Python/3.x": no version to look up
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 10

    # -- entry points --------------------------------------------------------

    def do_GET(self) -> None:
        try:
            self._check_host()
            path = self._path()
            if path == "/api/me":
                self._send_json(HTTPStatus.OK, self._run(api.me, signed_in=False, body={}))
                return
            if path.startswith("/api/"):
                raise _Refused(HTTPStatus.METHOD_NOT_ALLOWED, "use POST")
            entry = self.server.static.get(path)
            if entry is None:
                raise _Refused(HTTPStatus.NOT_FOUND, "not found")
            data, kind = entry
            self._send(HTTPStatus.OK, data, kind)
        except _Refused as refused:
            self._send_json(refused.status, {"error": refused.message})

    def do_POST(self) -> None:
        try:
            self._check_host()
            self._check_same_site()
            path = self._path()
            public = api.PUBLIC.get(path)
            private = api.SIGNED_IN.get(path)
            if public is None and private is None:
                raise _Refused(HTTPStatus.NOT_FOUND, "not found")
            body = self._read_json()
            if public is not None:
                reply = self._run(public, signed_in=False, body=body)
            else:
                assert private is not None
                reply = self._run(private, signed_in=True, body=body)
            self._send_json(HTTPStatus.OK, reply)
        except _Refused as refused:
            self._send_json(refused.status, {"error": refused.message})

    def _unsupported(self) -> None:
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _unsupported
    do_HEAD = do_GET  # same headers, no body (see _send)

    # -- checks --------------------------------------------------------------

    def _path(self) -> str:
        return self.path.split("?", 1)[0].split("#", 1)[0]

    def _check_host(self) -> None:
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            raise _Refused(HTTPStatus.MISDIRECTED_REQUEST, "this server answers to 127.0.0.1 only")

    def _check_same_site(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins:
            raise _Refused(HTTPStatus.FORBIDDEN, "cross-site requests are refused")
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in ("same-origin", "none"):
            raise _Refused(HTTPStatus.FORBIDDEN, "cross-site requests are refused")
        if self.headers.get(MARKER_HEADER) != "1":
            raise _Refused(HTTPStatus.FORBIDDEN, "requests must come from the casino pages")

    def _read_json(self) -> dict[str, object]:
        kind = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if kind != "application/json":
            raise _Refused(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
        if self.headers.get("Transfer-Encoding"):
            raise _Refused(HTTPStatus.BAD_REQUEST, "send a Content-Length")
        length_text = self.headers.get("Content-Length")
        if length_text is None or not length_text.isdigit():
            raise _Refused(HTTPStatus.LENGTH_REQUIRED, "send a Content-Length")
        length = int(length_text)
        if length > MAX_BODY:
            # Read and discard a body up to DRAIN_MAX first: closing a socket
            # with unread data makes Windows reset the connection, and the
            # client would never see the 413. Anything bigger is just dropped.
            if length <= DRAIN_MAX:
                self.rfile.read(length)
            raise _Refused(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
        raw = self.rfile.read(length)
        try:
            # A fraction or NaN is refused here, so no float reaches money.
            body = json.loads(raw.decode("utf-8"), parse_float=_no_floats, parse_constant=_no_floats)
        except (UnicodeDecodeError, ValueError):
            raise _Refused(HTTPStatus.BAD_REQUEST, "send a JSON object; amounts as text like \"12.50\"") from None
        if not isinstance(body, dict):
            raise _Refused(HTTPStatus.BAD_REQUEST, "send a JSON object")
        return body

    def _cookie_sid(self) -> str | None:
        header = self.headers.get("Cookie")
        if not header:
            return None
        try:
            jar = SimpleCookie(header)
        except CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel is not None else None

    # -- running a route -----------------------------------------------------

    def _run(self, route: api.Route, *, signed_in: bool, body: dict[str, object]) -> object:
        server = self.server
        login = server.logins.find(self._cookie_sid())
        if signed_in:
            if login is None:
                raise _Refused(HTTPStatus.UNAUTHORIZED, "please log in")
            if not login.csrf_matches(self.headers.get(CSRF_HEADER)):
                raise _Refused(HTTPStatus.FORBIDDEN, "this page is out of date; reload it")
        elif login is not None and self.command == "POST" and not login.csrf_matches(self.headers.get(CSRF_HEADER)):
            # Signed in already: logging out or in again needs the CSRF value too.
            raise _Refused(HTTPStatus.FORBIDDEN, "this page is out of date; reload it")

        call = api.Call(conn=server.conn, store=server.logins, login=login, body=body)
        try:
            reply = route(call)
        except api.NotSignedIn:
            raise _Refused(HTTPStatus.UNAUTHORIZED, "please log in") from None
        except api.BadRequest as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, str(exc)) from None
        except AccessDenied as exc:
            raise _Refused(HTTPStatus.FORBIDDEN, str(exc)) from None
        except AuthError as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, str(exc)) from None
        except USER_ERRORS as exc:
            raise _Refused(HTTPStatus.BAD_REQUEST, str(exc)) from None
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            # sqlite3.Error included: say nothing about the cause.
            if server.conn.in_transaction:
                server.conn.execute("ROLLBACK")
            raise _Refused(HTTPStatus.INTERNAL_SERVER_ERROR, UNEXPECTED) from None

        self._cookie_change = call.new_login, call.ended
        return reply

    # -- responses -----------------------------------------------------------

    _cookie_change: tuple[WebLogin | None, bool] = (None, False)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, data, "application/json; charset=utf-8")

    def _send(self, status: HTTPStatus, data: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        new_login, ended = self._cookie_change
        if new_login is not None:
            self.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={new_login.sid}; Path=/; HttpOnly; SameSite=Strict; Max-Age={ABSOLUTE_SECONDS}",
            )
        elif ended:
            self.send_header("Set-Cookie", f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    # -- logging -------------------------------------------------------------

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if not self.server.quiet:
            sys.stderr.write(f"{self.log_date_time_string()} {self.command} {self._path()} {code}\n")

    def log_error(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's name
        # The base class would log the raw request line, query string and all.
        return


def _no_floats(text: str) -> NoReturn:
    raise ValueError("fractional numbers are not accepted")


def serve(conn: sqlite3.Connection, port: int = DEFAULT_PORT) -> CasinoServer:
    """Bind the server. The caller prints the address and calls serve_forever()."""
    return CasinoServer(conn, port)
