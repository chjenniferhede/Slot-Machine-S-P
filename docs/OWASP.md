# OWASP Top 10 (2021): how the casino answers each category

Every control below is applied in one place, so it can be pointed at during the
demo. Line numbers are for the commit that adds this document; the function or
constant name is the stable reference if lines move.

The system is a local web application: one Python process serves a JSON API
and static pages on `127.0.0.1` only, backed by one SQLite file. That shapes
several answers below, most of all A10.

## A01 Broken Access Control

**Risk.** A player reaches technician or commissioner actions, a technician
moves player money, or anyone reads another player's data.

**Controls.**

- One gate: `auth.require_role` (`src/slotmachine/auth.py:135`). Every
  privileged function carries it. On each call it rereads the user from the
  database, so a stale role, a deleted user or a pending forced password
  change is refused. It records `access.granted` / `access.denied` in the audit
  log before the action runs (CWE-285, CWE-862).
- Role modules: every public function in `technician.py` and `commissioner.py`
  must carry `@require_role` for exactly that role. `tests/test_source_invariants.py`
  (`test_every_privileged_function_is_decorated`) fails the build otherwise.
  The player entry points in `accounts.py` and `payment.py` are enumerated
  independently in `tests/test_identity_money_integration.py`.
- The web layer makes no access decisions. Each route in
  `src/slotmachine/web/api.py:564-600` calls one decorated function, so a
  player who posts to a technician route reaches `technician.*` and is refused
  there. `tests/test_web.py::test_roles_cannot_use_each_others_routes` checks
  the matrix over HTTP.
- Object ownership: every card and game-session lookup includes the caller's
  `user_id` (`storage.get_payment_method`, `storage.get_session`). A player's
  open game is looked up server-side (`accounts.current_game`) and is never
  named by the browser (CWE-639).
- No role hierarchy: technicians and commissioners have no wallet and cannot play.
- Test spins: a schema trigger refuses `is_test` unless the spin's user is a
  commissioner (`src/slotmachine/schema.sql:122`).

## A02 Cryptographic Failures

**Risk.** Predictable spins, recoverable passwords, stored card numbers.

**Controls.**

- Spin randomness: a fresh 32-byte `secrets.token_bytes` seed per spin
  (`src/slotmachine/rng.py:22`), expanded with HMAC-SHA256 and rejection
  sampling so every reel position is equally likely (`rng.py:34`, `rng.py:60`).
  `random` is never imported; a source scan enforces it (CWE-338).
- Passwords: Argon2id at m=19456 KiB, t=2, p=1 with per-password salts
  (`auth.py:28-35`). Staff one-time passwords come from `secrets.token_urlsafe`
  (`auth.py:235`) (CWE-916).
- Cards: the mock processor keeps only a random token, the last four digits,
  the brand and the expiry (`payment.py:168`). The number and CVV are excluded
  from `repr()` (`payment.py:71-74`) and are never logged (CWE-311, CWE-359).
- Session ids and CSRF values: 32 random bytes each from `rng.random_token`
  (`web/sessions.py:63-64`). CSRF values are compared with `hmac.compare_digest`
  (`web/sessions.py:49`).
- Transport: the server binds to `127.0.0.1` only (`web/server.py:47`). Traffic
  between the browser and the server never leaves the machine, so plain HTTP
  exposes nothing a local administrator could not already read. TLS would add
  a certificate to manage and no protection in this deployment. A networked
  deployment would need TLS and the `Secure` cookie flag first.

## A03 Injection

**Risk.** SQL injection through usernames, game ids or filters; HTML or script
injection into the pages; terminal escape injection in the CLI.

**Controls.**

- SQL: every value is bound with `?`. `tests/test_source_invariants.py`
  (`find_sql_string_building`) traces every `execute()` argument back to fixed
  text and fails on any f-string, `%` or `.format` (CWE-89). The audit filter
  query (`audit.read`) joins only fixed clauses.
- HTML: the pages build every element with `textContent` through `el()` in
  `web/static/common.js`. `tests/test_web_static.py` fails on `innerHTML`,
  `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval` or `new Function`
  (CWE-79). The CSP (A05) blocks inline script as a second layer.
- Usernames: allowlist `[A-Za-z0-9_.-]{3,32}` (`auth.validate_username`).
- Terminal: `cli.prompts.clean` (`cli/prompts.py:61`) replaces control
  characters before printing (CWE-150).

## A04 Insecure Design

**Risk.** A game that pays back more than it takes, money lost or created mid-crash,
or two requests spending the same balance.

**Controls.**

- Exact RTP, including perfect hold play: `tools/rtpcalc.py:106` and
  `rtpcalc.py:174`. Shipped games return 91.37% (95.62% with perfect holds)
  and 92.32%. A technician-installed game is refused if it would return 100%
  or more (`technician.py:148`), and it starts disabled until someone has read
  its PAR sheet.
- Bet, spin, payout and audit entry in one `BEGIN IMMEDIATE` transaction
  (`storage.py:52-62`, `accounts._play` at `accounts.py:371`). The check and
  the debit share the transaction, so two requests cannot both pass (CWE-367).
- Bets must be one of the game's tiers (`accounts._check_bet`), so no
  arbitrary or negative stake can mint money. Re-spins cost 200% of the bet,
  which keeps perfect hold play below 100%.
- Money is integer cents in `Money` (`money.py:35`), bounded to SQLite's
  64-bit range. The browser sends amounts as text; the server refuses JSON
  numbers with a fraction while parsing (`web/server.py:204`) (CWE-681).
- Crash recovery: `accounts.recover_open_sessions` (`accounts.py:338`) returns
  abandoned credits at startup. `tests/test_integration.py` kills the process
  mid-spin and checks conservation.
- Commissioner test rounds use the real engine and RNG but a virtual bankroll
  that exists only in memory (`commissioner.run_test_session`, `commissioner.py:135`);
  every round is replayed from its seed afterwards (`commissioner._replay`).

## A05 Security Misconfiguration

**Risk.** Default credentials, verbose errors, permissive browser policy.

**Controls.**

- No default password: the first technician gets a random one-time password,
  printed once in the terminal and never sent to a browser, and must change it
  at first login (`auth.bootstrap_if_empty`, `__main__.startup`) (CWE-1392).
- Errors: expected refusals are shown as their message; anything else becomes
  one fixed line, both in the CLI (`cli/menus.run_action`) and on the web
  (`web/server.py:255`). No traceback, path or version reaches a user (CWE-209).
  The `Server` header carries no Python version.
- Browser policy on every response (`web/server.py:54-69`): CSP
  `default-src 'none'; script-src 'self'; style-src 'self'; … frame-ancestors 'none'`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`.
- Only listed files are served, from inside the package (`web/server.py:72`);
  no request path is joined onto a file name (CWE-22).
- SQLite foreign keys are switched on per connection (`storage.py:35`).

## A06 Vulnerable and Outdated Components

**Risk.** A compromised or vulnerable dependency.

**Controls.**

- One runtime dependency, `argon2-cffi`, pinned by version and by hash in
  `requirements.txt` and installed with `--require-hashes` (CWE-1104, CWE-494).
- The web server, sessions, JSON and SQLite all come from the standard
  library. No front-end framework, no CDN, no build step: the pages are
  hand-written HTML, CSS and JavaScript served from the package.

## A07 Identification and Authentication Failures

**Risk.** Password guessing, user enumeration, session theft or fixation.

**Controls.**

- Five failures lock an account for 15 minutes (`auth.py:36-37`); counters
  persist across restarts. Unknown users, wrong passwords and locked accounts
  get the same message, and unknown users still pay for an Argon2 verification
  (`auth.py:103`) (CWE-307, CWE-204).
- Passwords: 12–1024 characters, a common-password list, no composition rules.
- Browser sessions (`web/sessions.py`): a random id in an `HttpOnly`,
  `SameSite=Strict` cookie, nothing else. Logins end after 30 idle minutes or
  8 hours, on logout, and when the server stops. Logging in and changing a
  password issue a new id (`LoginStore.renew`, `sessions.py:87`) (CWE-384,
  CWE-613).
- Cross-site requests: every POST needs JSON, the `X-Casino` header, and once
  signed in the login's CSRF value; `Origin` and `Sec-Fetch-Site` from another
  site are refused (`web/server.py:179`). The Host header must be this server,
  which stops DNS rebinding (`web/server.py:175`) (CWE-352, CWE-350).

## A08 Software and Data Integrity Failures

**Risk.** Code execution through a game config; silent edits to history.

**Controls.**

- Game configs are JSON only, checked field by field (`game/spec.py:88`);
  `pickle`, `marshal`, `shelve`, `eval` and `exec` are banned by a source scan
  (CWE-502).
- The audit log is a hash chain: each entry hashes the previous one
  (`audit.py:57`). `verify_chain` (`audit.py:100`) reports the first entry that
  does not fit, and technicians run it from the back office (CWE-354).
- Spins and audit entries are append-only by trigger (`schema.sql:109-142`).
- Every spin stores its seed, so any result can be recomputed.
- Documented limit: deleting entries from the *end* of the chain leaves a
  shorter chain that still verifies. Detecting that needs the last hash kept
  somewhere the attacker cannot write.

## A09 Security Logging and Monitoring Failures

**Risk.** An attack leaves no trace.

**Controls.**

- Logged in the same transaction as the change they describe: registration,
  login success and failure, lockout, password change, staff creation, card
  added and removed, deposit, withdrawal, buy-in, every spin, cash-out,
  recovery, game installed, enabled and disabled, test session start and end.
- Every privileged call logs `access.granted` or `access.denied` inside the
  role gate (CWE-778).
- The audit viewer filters by actor, action and date, and pages through history.
- Never logged: passwords, hashes, card numbers, CVVs, session ids. The web
  access log records method, path without query, and status only
  (`web/server.py:288`) (CWE-532).

## A10 Server-Side Request Forgery

**Not applicable, and why.** SSRF needs the server to fetch a URL that an
attacker influences. This system makes no outbound requests at all: it has no
HTTP client code, imports no networking library besides the standard
library's server, and loads no remote resources. The payment processor is a
local mock. The pages are equally closed: the CSP sets
`connect-src 'self'` and `default-src 'none'`, and
`tests/test_web_static.py::test_nothing_is_loaded_from_elsewhere` fails if a
remote URL appears in the HTML, CSS or JavaScript. If a real payment gateway
were added, its URL would have to be fixed in configuration, never taken from
a request.
