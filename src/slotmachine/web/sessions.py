"""Browser logins, kept in server memory and named by an unguessable cookie.

The cookie holds a random id and nothing else: no username, no role, nothing
signed or encoded that a client could edit. The identity behind it is the
frozen ``auth.Session`` that ``auth.login`` returned, and every action still
goes through ``auth.require_role``, which rereads the user from the database.
So a stolen or stale cookie is worth exactly one live login, and a role
changed in the database takes effect on the next request.

Each login also gets a separate CSRF value, which the page sends back in a
header on every change. A cross-site page can make the browser send the
cookie but cannot read the CSRF value, so it cannot forge a request.

Logins end after 30 idle minutes or 8 hours in all, on logout, and when the
server stops. Logging in or changing a password issues a new id, so an id
planted before login is useless after it (session fixation, CWE-384).
"""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from slotmachine import auth, rng

IDLE_SECONDS: Final[int] = 30 * 60
ABSOLUTE_SECONDS: Final[int] = 8 * 60 * 60
MAX_SESSIONS: Final[int] = 1_000
ID_BYTES: Final[int] = 32


@dataclass(frozen=True)
class WebLogin:
    sid: str = ""
    csrf: str = ""
    identity: auth.Session | None = None
    created: float = 0.0
    last_seen: float = 0.0

    def __repr__(self) -> str:
        # Never print the id or the CSRF value, even in a traceback.
        who = self.identity.username if self.identity else None
        return f"WebLogin(user={who!r})"

    def csrf_matches(self, presented: str | None) -> bool:
        return presented is not None and hmac.compare_digest(self.csrf, presented)


class LoginStore:
    """Every live browser login, by cookie id."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._logins: dict[str, WebLogin] = {}

    def start(self, identity: auth.Session) -> WebLogin:
        self._evict()
        now = self._clock()
        login = WebLogin(
            sid=rng.random_token(ID_BYTES),
            csrf=rng.random_token(ID_BYTES),
            identity=identity,
            created=now,
            last_seen=now,
        )
        self._logins[login.sid] = login
        return login

    def find(self, sid: str | None) -> WebLogin | None:
        """The login for this cookie id, if it is still live. Refreshes its idle clock."""
        if not sid:
            return None
        login = self._logins.get(sid)
        if login is None:
            return None
        now = self._clock()
        if now - login.last_seen > IDLE_SECONDS or now - login.created > ABSOLUTE_SECONDS:
            del self._logins[sid]
            return None
        login = replace(login, last_seen=now)
        self._logins[sid] = login
        return login

    def renew(self, login: WebLogin) -> WebLogin:
        """The same identity under a new id and CSRF value; the old id stops working."""
        self._logins.pop(login.sid, None)
        assert login.identity is not None
        fresh = replace(self.start(login.identity), created=login.created)
        self._logins[fresh.sid] = fresh  # keeps the original 8-hour limit
        return fresh

    def end(self, sid: str | None) -> None:
        if sid:
            self._logins.pop(sid, None)

    def __len__(self) -> int:
        return len(self._logins)

    def _evict(self) -> None:
        """Drop expired logins, and the oldest if the table is full."""
        now = self._clock()
        for sid, login in list(self._logins.items()):
            if now - login.last_seen > IDLE_SECONDS or now - login.created > ABSOLUTE_SECONDS:
                del self._logins[sid]
        while len(self._logins) >= MAX_SESSIONS:
            oldest = min(self._logins.values(), key=lambda entry: entry.last_seen)
            del self._logins[oldest.sid]
