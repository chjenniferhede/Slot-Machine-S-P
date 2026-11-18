# Security Design — OWASP Top 10 (2021) and CWE Mapping

Design principle: **one enforcement point per security property.** There is one RNG
(`rng.py`), one money type (`money.py`), one role gate (`auth.require_role`), one audit
writer (`audit.write`), and one checked entry point per front end for untrusted input
(`cli/prompts.py` in the terminal, the field readers in `web/api.py` in the browser).
Each is small enough to read in full, and tests in `tests/test_source_invariants.py` scan the
whole source tree so that a change bypassing one of them fails the build.

Only CWEs that MITRE marks "Vulnerability Mapping: Allowed" are cited: Base and Variant
weaknesses, plus the two well-known composites CWE-352 (CSRF) and CWE-384 (session
fixation). Categories and groupings such as CWE-840 are Prohibited, deprecated entries such
as CWE-613 are Prohibited, and pillars and most classes (CWE-284, CWE-285, CWE-311) are
Discouraged. One exception: CWE-798 (hard-coded credentials) is "Allowed with review";
it is cited only for the scan that forbids hard-coded credentials, which is exactly what it
describes. Every ID here was checked at cwe.mitre.org (CWE 5.0) on 2026-11-17.

## Architecture at a glance

```
 browser pages (web/static/*.js: textContent only, strict CSP)
        |  JSON over HTTP on 127.0.0.1 only
 web/server.py + web/api.py + web/sessions.py   ← Host/Origin/CSRF checks, cookie login,
        |                                          no access decisions of its own
 cli/player.py   cli/technician.py   cli/commissioner.py     ← one interface per role
        \               |                  /                    (prompts.py validates all input,
         \              |                 /                      menus.py turns errors into one line)
          accounts.py  payment.py  stats.py                   ← business rules, each action
                \          |         /                          guarded by @require_role
           auth.py (Argon2id, lockout, @require_role)
           game/engine.py + game/spec.py (pure, JSON-only)
           rng.py (secrets → HMAC-SHA256 → unbiased stops)
           audit.py (SHA-256 hash chain)   money.py (integer cents)
                         storage.py + schema.sql (SQLite, bound parameters,
                                                   triggers, one transaction per action)
```

---

## A01 Broken Access Control

**Threats:** a player reaching technician or commissioner actions; one player using
another's card or game session by guessing ids; a commissioner moving money; a test spin
being passed off as a live spin or the reverse; a forced password change being skipped.

| Control | Where | CWE |
|---|---|---|
| Every privileged action carries `@require_role(...)`. The decorator re-reads the user's role from the database on every call (a forged or stale `Session` is refused), blocks everything until a pending password change is done, and audits granted and denied attempts. | `auth.py: require_role` | CWE-425, CWE-620 |
| A test fails if any public function in `cli/technician.py` or `cli/commissioner.py` lacks the decorator or allows a second role. The role menus themselves are guarded too, so routing to the wrong interface is refused rather than hidden. | `tests/test_source_invariants.py: test_every_privileged_function_is_decorated` | CWE-425 |
| Cards and sessions are always looked up by id **and** owner (`WHERE id = ? AND user_id = ?`). "Not yours" and "does not exist" get the same answer, and the attempt is logged. | `storage.py`, `accounts.py: _record_refusal`, `payment.py: remove_payment_method` | CWE-639 (IDOR) |
| A database trigger refuses a test spin from anyone but a commissioner and a live spin from a commissioner. Spins and audit rows cannot be updated or deleted. | `schema.sql` triggers | CWE-266 |
| Staff accounts cannot self-register: `register_player` never accepts a role, and only a technician can create staff. | `auth.py: register_player, create_staff_account` | CWE-266 |
| Seeds are shown only in commissioner screens (replay and test mode). | `cli/commissioner.py` | — |
| The web API makes no access decisions: each route calls one of the decorated functions above (the same ones the terminal menus call), so a player posting to a technician route is refused by `require_role`. A test drives the whole role matrix over HTTP. | `web/api.py: SIGNED_IN`; `tests/test_web.py: test_roles_cannot_use_each_others_routes` | CWE-425 |
| A player's open game is looked up on the server from the caller's own user id (`accounts.current_game`); the browser never names a session id. | `accounts.py: current_game`, `web/api.py` | CWE-639 |

## A02 Cryptographic Failures

**Threats:** predictable spins; cracked passwords from a stolen database; stored card
numbers; timing leaks.

| Control | Where | CWE |
|---|---|---|
| All randomness comes from `secrets` (the OS CSPRNG). Each spin draws a fresh 32-byte seed; reel stops are derived with HMAC-SHA256 and **rejection sampling**, so there is no modulo bias. `random` is never imported (test-enforced), and only `rng.py`, `auth.py` and `payment.py` may draw entropy. | `rng.py`; scans in `test_source_invariants.py` | CWE-338, CWE-337 |
| Passwords hashed with **Argon2id** (m = 19 MiB, t = 2, p = 1, 16-byte salt): the OWASP Password Storage Cheat Sheet's first choice. Old hashes are re-hashed on login if parameters change. | `auth.py` | CWE-916, CWE-759 |
| Card numbers and CVVs are never stored or logged: a random token, last 4 digits, brand and expiry only. The token is random, not a hash of the number, so it cannot be brute-forced back. `CardInput` hides the number and CVV from `repr()`. | `payment.py` | CWE-312, CWE-532 |
| Hash and digest comparisons use `hmac.compare_digest`. Unknown usernames still run a full Argon2 verify against a dummy hash, so timing does not reveal which accounts exist. | `audit.py`, `auth.py: login` | CWE-208 |
| Browser login ids and CSRF values are 32 random bytes each from `rng.random_token`; the CSRF value is compared with `hmac.compare_digest`. The web server binds to `127.0.0.1` only, so HTTP traffic never leaves the machine; a networked deployment would need TLS and the `Secure` cookie flag first. | `web/sessions.py`, `web/server.py: HOST` | CWE-208 |

## A03 Injection

**Threats:** SQL injection via usernames, game ids or audit filters; terminal escape
sequences injected through usernames or audit details; code injection through game files.

| Control | Where | CWE |
|---|---|---|
| Every query uses `?` bound parameters. Query text is fixed; the audit filter builds its `WHERE` only from constant strings. A test parses every source file and fails on f-string, `%`, `.format()` or `+` SQL. | `storage.py`, `audit.py: read`; `test_no_sql_string_building` | CWE-89 |
| All input goes through `cli/prompts.py`: length-limited, allowlist regexes (usernames `[A-Za-z0-9_.-]{3,32}`, game ids `[a-z0-9_]{1,32}`), ASCII-only digits for numbers and money. | `cli/prompts.py`, `money.py: parse` | CWE-1284, CWE-1286 |
| Everything printed that came from a user goes through `clean()`, which replaces control characters, so stored text cannot rewrite the terminal. | `cli/prompts.py: clean`, `cli/menus.py: table` | CWE-150 |
| Game configs are JSON only, validated field by field; never `pickle`, `eval`, `exec` or unsafe YAML (test-enforced). | `game/spec.py: load_spec`; `test_no_unsafe_deserialisation` | CWE-502 |
| The pages put server data on screen with `textContent` only, through one `el()` helper; a scan fails the build on `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval` or `new Function`, and on inline script, style or event handlers in the HTML. The CSP (`script-src 'self'`, no `unsafe-inline`) blocks injected script as a second layer. | `web/static/common.js: el`; `tests/test_web_static.py`; `web/server.py: CSP` | CWE-79 |
| Browser input arrives as JSON with each field type-checked. Amounts are text such as `"12.50"` and go through `Money.parse`; a JSON number with a fraction, an exponent or NaN is refused while parsing, so no float reaches money. Ids must be whole numbers, and `true` is not a number. | `web/server.py: _read_json`, `web/api.py: _text, _whole, _amount` | CWE-1284, CWE-681 |

## A04 Insecure Design

**Threats:** a game (or a hold strategy) that pays more than it takes; money minted or
lost through a crash, a Ctrl-C or two concurrent requests; arbitrary bet amounts;
rounding errors on balances.

| Control | Where | CWE |
|---|---|---|
| Exact RTP is computed (not simulated), including the optimal hold strategy by dynamic programming. A technician cannot install or enable a game whose base or perfect-hold RTP is 100% or more. | `tools/rtpcalc.py`, `cli/technician.py: _check_house_edge` | (design control; no single CWE) |
| Bet, spin record, payout and audit entry happen in one `BEGIN IMMEDIATE` transaction. Checks are made inside the same transaction that changes the money. `Ctrl-C` (`BaseException`) rolls back. Open sessions left by a crash are cashed out at the next start. | `storage.py: transaction`, `accounts.py` | CWE-367 |
| Money is integer cents in an immutable `Money` type; no floats in money modules (test-enforced). The schema refuses negative balances and credits even if a check above is missing. | `money.py`, `schema.sql`; `test_no_float_in_money_paths` | CWE-681, CWE-1339 |
| Bets must be one of the game's tiers; hold rules (reels, count, price) come from the game spec and the immutable spin history, never from the caller. Buy-in/cash-out bounds what a session can lose. Daily deposit and withdrawal limits. | `accounts.py: _check_bet, _check_hold, _hold_chain` | CWE-1284 |
| An installed game's rules can never be replaced, so recorded spins always replay to the same result. | `cli/technician.py: _install` | — |
| Resource limits: a game config is at most 256 KB and at most 5 reels, 10 symbols, 128 positions per reel, 5 rows, 20 paylines and 6 bets of up to $1,000, checked **before** the exact-RTP proof, whose cost grows as symbols ** reels. Web request bodies are capped at 64 KiB, the login table at 1,000 entries, a test session at 100,000 rounds. | `cli/technician.py: _check_size`, `web/server.py: MAX_BODY`, `web/sessions.py: MAX_SESSIONS`, `cli/commissioner.py: MAX_TEST_ROUNDS` | CWE-770 |
| A commissioner's test session uses the real RNG and engine, but its virtual bankroll exists only in that call; afterwards every round is replayed from its stored seed and the result reported. | `cli/commissioner.py: run_test_session, _replay_rounds` | — |

## A05 Security Misconfiguration

**Threats:** default credentials; database constraints silently off; error messages
leaking internals.

| Control | Where | CWE |
|---|---|---|
| No default password: the first technician gets a random `secrets` password shown once, and must change it before doing anything. Staff accounts created later work the same way. | `auth.py: bootstrap_if_empty, create_staff_account` | CWE-1392 |
| `PRAGMA foreign_keys = ON` on every connection (SQLite's default is off). | `storage.py: connect` | CWE-1188 |
| Any unexpected error becomes one fixed line ("Something went wrong"), never a traceback with paths or versions, in the terminal and in the browser. The web server's `Server` header carries no Python version. | `cli/menus.py: run_action, run`; `web/server.py: _run` | CWE-209 |
| Every web response carries a strict Content Security Policy (`default-src 'none'`, scripts, styles and fetches from this server only, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. The pages cannot be framed for clickjacking. | `web/server.py: CSP, SECURITY_HEADERS` | CWE-1021 |
| Responses are sent with `Cache-Control: no-store`, so balances and one-time staff passwords are not kept in the browser cache. | `web/server.py: SECURITY_HEADERS` | CWE-525 |
| Only a fixed list of files is served, read from inside the package; no part of a request path is joined onto a file name. | `web/server.py: STATIC` | — |
| No hard-coded credentials, no debug leftovers (`breakpoint`, `pdb`, stray `print`) (test-enforced). The database file is git-ignored. | `test_no_hardcoded_credentials`, `test_no_debug_leftovers`, `.gitignore` | CWE-798, CWE-489, CWE-540 |

## A06 Vulnerable and Outdated Components

**Threats:** a known-vulnerable or substituted dependency.

| Control | Where | CWE |
|---|---|---|
| One runtime dependency (`argon2-cffi`) plus its two sub-dependencies; everything else is the standard library. | `pyproject.toml` | CWE-1104 |
| Exact versions pinned **with SHA-256 hashes** for every published file; installed with `--require-hashes`, and the program itself with `--no-deps`. | `requirements.txt`, `docs/INSTALL.md` | CWE-494 |
| No dynamic imports, so no module can be loaded by name at run time (test-enforced). | `test_no_dynamic_imports` | CWE-829 |

## A07 Identification and Authentication Failures

**Threats:** password guessing; user enumeration; weak passwords; session confusion.

| Control | Where | CWE |
|---|---|---|
| 5 consecutive failures lock the account for 15 minutes; the lock is stored in the database, so restarting the program does not reset it. | `auth.py: login` | CWE-307 |
| One message for every login failure, and comparable work for unknown users. | `auth.py: LOGIN_FAILURE_MESSAGE, _DUMMY_HASH` | CWE-204 |
| Passwords 12–1024 characters, common passwords refused; changing a password requires the current one. Passwords are read with `getpass`, never echoed. | `auth.py: validate_password_strength, change_password`; `cli/prompts.py` | CWE-521, CWE-620, CWE-549 |
| Usernames are case-insensitive unique (`COLLATE NOCASE`), so `Alice` cannot impersonate `alice`. | `schema.sql` | CWE-178 |
| A `Session` is an in-process frozen object, re-validated against the database on every action. In the terminal there are no tokens at all; in the browser the cookie holds only a random id that maps to that object in server memory. | `auth.py: Session, _identity_row`; `web/sessions.py` | — |
| The browser cookie is `HttpOnly` (unreadable to page script) and `SameSite=Strict`, and holds nothing but the random id. A new id is issued at login and at every password change, so an id planted before login is useless after it. Logins end after 30 idle minutes or 8 hours, at logout, and when the server stops. | `web/server.py: _send`, `web/sessions.py: LoginStore` | CWE-1004, CWE-1275, CWE-384 |
| Cross-site request forgery: every change is a POST with a JSON body, the `X-Casino` header and, once signed in, the login's CSRF value; a foreign `Origin` or `Sec-Fetch-Site` is refused. The `Host` header must name this server, which also stops DNS rebinding. | `web/server.py: _check_host, _check_same_site, _run` | CWE-352 |

## A08 Software and Data Integrity Failures

**Threats:** silent edits to the logs or spin history; deserialisation of malicious
configs; forged spins.

| Control | Where | CWE |
|---|---|---|
| **Hash-chained audit log** (additional feature): each entry stores the SHA-256 of the previous one; *Verify audit log integrity* recomputes the chain and names the first broken entry. Triggers block `UPDATE`/`DELETE` on the log. | `audit.py`, `schema.sql`, `cli/technician.py: verify_audit_chain` | CWE-353 |
| **Seed replay** (additional feature): every spin stores its seed; a commissioner can re-derive the reel stops and payout and see that they match. Spins cannot be edited (trigger). | `cli/commissioner.py: view_spin_seeds`, `schema.sql` | CWE-353 |
| Game configs are validated JSON, never pickle; hash-pinned dependencies. | `game/spec.py`, `requirements.txt` | CWE-502, CWE-494 |

## A09 Security Logging and Monitoring Failures

**Threats:** attacks leaving no trace; logs that leak secrets; logs that can be rewritten.

| Control | Where | CWE |
|---|---|---|
| Logged: registration, login success and failure, lockout, password change, every access decision (granted or denied) from inside the role gate, card added/removed, deposit, withdrawal, buy-in, every spin, cash-out, crash recovery, game install/enable/disable, staff creation, test sessions, spin replays, audit verifications. | `audit.py: Action`, every module | CWE-778, CWE-223 |
| Each business event is written in the same transaction as the change it records, so a change cannot happen without its entry. | `accounts.py`, `payment.py`, `cli/technician.py` | CWE-778 |
| Never logged: passwords, hashes, card numbers, CVVs, tokens. The web access log records the method, the path without its query, and the status: never a body, a cookie or a header. | `payment.py`, `auth.py`, `web/server.py: log_request` | CWE-532 |
| Technicians can filter the log and verify it; the diagnostic shows chain status and locked accounts. Log details are shown through `clean()`, so a username cannot forge or hide log lines on screen. | `cli/technician.py`, `cli/prompts.py: clean` | CWE-778, CWE-117 |
| Known limit: removing the newest entries is not detectable from the chain alone (documented). | `audit.py` docstring | |

## A10 Server-Side Request Forgery

**Not applicable, by design.** SSRF needs a server that fetches a URL an attacker can
influence. This system never opens an outbound connection: it has no HTTP client code,
takes no URL as input, and its payment processor is a local mock. The optional web front
end (`slotmachine --web`) only *listens*, on `127.0.0.1`, and answers from a fixed list of
local files and the JSON API; it requests nothing itself. The pages are equally closed:
the CSP sets `default-src 'none'` and `connect-src 'self'`, and
`tests/test_web_static.py: test_nothing_is_loaded_from_elsewhere` fails if a remote URL
appears in the HTML, CSS or JavaScript.

The one place a path is read is the technician's *Install a new game* in the terminal,
which reads a local file only: it must end in `.json`, be a regular file under 256 KB, and
pass full validation. The browser's version takes the JSON text itself. Neither can fetch
from the network.

---

## Additional features (beyond spec §5.2)

1. **Tamper-evident audit log with verification** (A08/A09): a regulator or technician
   can prove the log has not been altered.
2. **Provably fair spins**: any spin, live or test, can be replayed from its seed by a
   commissioner. This mirrors real regulatory practice, where gaming labs certify the RNG
   and require that outcomes be reproducible and auditable.
3. **PAR sheet and exact RTP**: the Probability Accounting Report that commissions request
   from manufacturers, computed exactly, including perfect hold play.
4. **Responsible-gaming limits**: daily deposit and withdrawal caps, and buy-in/cash-out
   credits instead of betting straight from the account.
