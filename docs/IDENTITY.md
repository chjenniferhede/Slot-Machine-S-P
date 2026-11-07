# Identity layer

Layer 2 implements issue #3 on top of the existing storage and audit layers.

## Try it

Install with the commands in `AGENTS.md`, then run:

```sh
slotmachine --db casino.db
```

The first run creates `tech-admin` with a random password. Save the password
immediately: it appears once, including when using `--init`. Login requires a
password change before the account menu opens. Canceling with Ctrl-C or EOF leaves
that requirement in place. No staff wallet is created.

The initial menu offers login, player registration, and quit. Registration creates
only a player with a zero-balance wallet. Usernames accept 3–32 ASCII letters,
digits, dots, hyphens, or underscores; uniqueness is case insensitive. Passwords
accept 12–1024 characters, without composition rules, and reject a small common
password list. Password entry uses hidden terminal prompts.

After login, users can change their password or log out. Payment and account
APIs and the shared CLI framework are integrated, but game, payment, technician,
and commissioner menus remain later-layer work, not available commands.

Five consecutive incorrect passwords lock an account for 15 minutes. Unknown,
incorrect-password, and locked-account logins all display `Invalid username or
password`. Wait for the lockout to expire; there is no password-reset bypass.
An incorrect attempt after expiry starts a new five-attempt window. Successful
login resets failures. Unknown accounts perform a dummy Argon2 verification.

## Integrate later layers

Use `auth.register_player(conn, username, password)` for self-registration. It
returns the user ID and cannot accept a role. Use `auth.login` to obtain the
frozen `Session(user_id, username, role)`; do not construct sessions from user
input. Sessions are trusted local Python objects, not bearer tokens or a security
boundary against code already executing in the same process.

Decorate privileged actions with `auth.require_role(*auth.ROLE_MATRIX[action])`.
The function's first two arguments must be `session` and `conn`. The decorator
checks the current database username, role, and password-change flag on every
call, records the result, and attaches `__required_roles__` for introspection.
There is no role hierarchy: technicians and commissioners cannot play or move
player money. The tests carry an independent expected role matrix.

Authentication mutations own a `storage.transaction` and must be called outside
an existing transaction. Login failure counters and audit events commit before
`AuthError` is raised. Registration, password changes, and bootstrap roll back
if their audit write fails, including on Ctrl-C. Bootstrap's empty-database check
and creation share the same write lock so concurrent starts create one account.

The role decorator owns a short authorization/audit transaction when none is
active, then invokes the action. The action can open its own money transaction.
This preserves the access-attempt evidence even if that action fails. If called
inside an existing transaction, the decorator joins it and the caller owns
commit/rollback, including the access event. Never nest `BEGIN IMMEDIATE`.
Business events (deposits, spins, etc.) must still be audited inside the same
transaction as the business mutation. Do not confuse `access.granted` with proof
that an action completed.

Use `auth.change_password(conn, session, current_password, new_password)` even
for a bootstrap session. It verifies the current password, rejects reuse, applies
policy, and clears `must_change_pw`. The decorator continues to block all
privileged actions until this succeeds. Catch `AuthError` and `AccessDenied` at
CLI boundaries; never display a traceback for those expected errors.

## Verification and remaining integration

```sh
python -m pytest tests/test_auth_hashing.py tests/test_auth_roles.py tests/test_auth_login.py tests/test_entry_point.py
python -m pytest
python -m mypy
```

The suite tests lockout persistence across reconnects, concurrent bootstrap and
failed logins, anonymous and stale-role denials, forced password changes through
the API and CLI, and transaction rollback when auditing is interrupted. Passwords
and hashes are excluded from audit events.

Layer 5 must apply the role decorator to every real technician and commissioner
entry point and add introspection tests for those modules when they exist. Layer
6 owns the full application-wide invariant scans and installation/manual work.
Staff account provisioning beyond the first technician is not exposed by this
identity CLI. The audit chain retains its documented tail-truncation limitation.

The payment and account entry points now carry the player role decorator. They
accept real authenticated sessions and retain their ownership checks. Additional
integration tests cover anonymous callers, staff, stale/deleted identities and
the forced password-change gate against every real money API. Their business
transactions remain separate from the decorator's committed access evidence.
The shared menu boundary recognizes `AuthError` and `AccessDenied`.

Startup calls `accounts.recover_open_sessions` after applying the schema and
before showing the identity menu, including with `--init`. It atomically returns
abandoned credits to their owners and logs recovery. This deployment assumes
one running application per database; starting another copy would recover an
active copy's sessions too.
