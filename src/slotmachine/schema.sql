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
