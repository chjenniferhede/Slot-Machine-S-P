-- Schema for the Big Casino slot machine.
--
-- Applied idempotently by storage.init_db(). Every table uses IF NOT EXISTS so
-- that running it against an existing database is a no-op.
--
-- Foreign keys are enabled per-connection in storage.connect(), NOT here --
-- PRAGMA foreign_keys is a connection-level setting and SQLite defaults it OFF
-- for backwards compatibility. Without that pragma the REFERENCES clauses below
-- are decorative.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COLLATE NOCASE stops 'Alice' and 'alice' becoming two accounts, which is
    -- a classic source of account-confusion attacks.
    username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash         TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('player', 'technician', 'commissioner')),
    must_change_pw  INTEGER NOT NULL DEFAULT 0 CHECK (must_change_pw IN (0, 1)),
    failed_count    INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    locked_until    TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    -- Defence in depth: the application forbids overdrafts, and so does the
    -- database. If a Phase II backdoor removes the application check, this
    -- constraint still fires.
    balance_cents         INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    daily_deposit_cents   INTEGER NOT NULL DEFAULT 0 CHECK (daily_deposit_cents >= 0),
    daily_withdraw_cents  INTEGER NOT NULL DEFAULT 0 CHECK (daily_withdraw_cents >= 0),
    day_bucket            TEXT    NOT NULL
);

-- Card data is NEVER stored. Only an opaque processor token, the last four
-- digits for display, and the brand. See ProvidedResources/SecureRNG.md,
-- "Minimise the Storage of Sensitive Information".
CREATE TABLE IF NOT EXISTS payment_methods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token      TEXT    NOT NULL UNIQUE,
    last4      TEXT    NOT NULL CHECK (length(last4) = 4),
    brand      TEXT    NOT NULL,
    exp_month  INTEGER NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year   INTEGER NOT NULL CHECK (exp_year >= 2000),
    added_at   TEXT    NOT NULL
);

-- Games are data, not code. config_json holds a validated GameSpec, which is
-- what makes "add a game" possible without a code change.
CREATE TABLE IF NOT EXISTS games (
    id           TEXT    PRIMARY KEY,
    name         TEXT    NOT NULL,
    config_json  TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    game_id        TEXT    NOT NULL REFERENCES games(id),
    credits_cents  INTEGER NOT NULL DEFAULT 0 CHECK (credits_cents >= 0),
    started_at     TEXT    NOT NULL,
    ended_at       TEXT
);

CREATE TABLE IF NOT EXISTS spins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    game_id         TEXT    NOT NULL REFERENCES games(id),
    bet_cents       INTEGER NOT NULL CHECK (bet_cents > 0),
    -- Raw 32 bytes, not hex text: half the storage and no encoding ambiguity.
    seed            BLOB    NOT NULL,
    stops_json      TEXT    NOT NULL,
    payout_cents    INTEGER NOT NULL CHECK (payout_cents >= 0),
    is_hold_respin  INTEGER NOT NULL DEFAULT 0 CHECK (is_hold_respin IN (0, 1)),
    -- Commissioner test spins live in this same table so that the regulator is
    -- exercising the real engine rather than a copy that could drift from it.
    -- The cost is that EVERY player-facing and accounting query must filter on
    -- this column. Forgetting to is the most likely bug in the stats layer.
    is_test         INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
    created_at      TEXT    NOT NULL
);

-- Append-only, hash-chained. Layer 1 (#2) owns the writer and the verifier;
-- the table lives here because the schema is applied in one place.
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_spins_game    ON spins(game_id, is_test);
CREATE INDEX IF NOT EXISTS idx_spins_user    ON spins(user_id, is_test);
CREATE INDEX IF NOT EXISTS idx_spins_created ON spins(created_at);
CREATE INDEX IF NOT EXISTS idx_spins_session ON spins(session_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts      ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor   ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_sessions_open ON sessions(user_id, ended_at);


-- ---------------------------------------------------------------------------
-- Append-only enforcement for spins and the audit log
-- ---------------------------------------------------------------------------
--
-- A spin is a historical fact. Once written it must never change, and it must
-- never disappear. Enforcing that in the database rather than by convention
-- closes a specific attack:
--
--   Without these triggers, hiding a fraudulent spin does not require deleting
--   it. Setting is_test = 1 is enough -- the spin keeps its money movement but
--   vanishes from every diagnostic, every RTP figure and every player
--   statistic, because all of those filter test spins out. That is a one-word
--   backdoor. These triggers make it fail loudly instead.
--
-- The same reasoning applies to audit_log, where it matters even more: the
-- hash chain detects tampering after the fact, and these triggers prevent a
-- whole class of it happening at all. Defence in depth -- the chain is the
-- detector, the triggers are the lock.
--
-- (OWASP A08 Software and Data Integrity Failures; CWE-mapping notes in
-- docs/OWASP.md.)

CREATE TRIGGER IF NOT EXISTS spins_are_immutable
BEFORE UPDATE ON spins
BEGIN
    SELECT RAISE(ABORT, 'spins are append-only: a recorded spin cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS spins_cannot_be_deleted
BEFORE DELETE ON spins
BEGIN
    SELECT RAISE(ABORT, 'spins are append-only: a recorded spin cannot be deleted');
END;

-- Immutability is only half the problem. These triggers stop is_test being
-- CHANGED after the fact; they do nothing about it being set to 1 when the row
-- is first written, which hides a spin just as effectively and is a one-line
-- change at any call site. Worse, it can be made conditional
-- (is_test = bet > $1000) so only the profitable spins disappear.
--
-- So the flag is not something the application is trusted to assert. A spin is
-- a test spin if and only if a commissioner made it, and that is checkable
-- here against the users table. Application code can now pass whatever it
-- likes; a lie is rejected at the point of insert.

CREATE TRIGGER IF NOT EXISTS spins_test_flag_must_match_role
BEFORE INSERT ON spins
WHEN NEW.is_test <> (SELECT role = 'commissioner' FROM users WHERE id = NEW.user_id)
BEGIN
    SELECT RAISE(
        ABORT,
        'is_test must match the actor: commissioners record test spins, everyone else records live spins'
    );
END;

CREATE TRIGGER IF NOT EXISTS audit_log_is_immutable
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: an entry cannot be modified');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_cannot_be_deleted
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: an entry cannot be deleted');
END;


-- ---------------------------------------------------------------------------
-- Views: make the safe query the easy one
-- ---------------------------------------------------------------------------
--
-- Commissioner test spins share the spins table so that the regulator is
-- exercising the real engine rather than a copy that could drift from it. The
-- cost is that every accounting and player-facing query must filter them out,
-- and forgetting once silently corrupts a number nobody is checking.
--
-- These views remove the need to remember. Application code reads live_spins
-- or test_spins by name; nothing outside storage.record_spin() should mention
-- the spins table directly, and a test in Layer 6 (#7) enforces that.

CREATE VIEW IF NOT EXISTS live_spins AS
    SELECT * FROM spins WHERE is_test = 0;

CREATE VIEW IF NOT EXISTS test_spins AS
    SELECT * FROM spins WHERE is_test = 1;
