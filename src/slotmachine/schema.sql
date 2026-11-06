-- Tables for the Big Casino slot machine.
--
-- Applied by storage.init_db(). Everything uses IF NOT EXISTS, so running it
-- again on an existing database does nothing.
--
-- Foreign keys are switched on in storage.connect(), not here -- it is a
-- per-connection setting, and SQLite leaves it off by default.

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- COLLATE NOCASE stops 'Alice' and 'alice' being two different accounts.
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
    -- The code refuses overdrafts and so does this. If someone removes the
    -- check in the code, this one still stops it.
    balance_cents         INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    daily_deposit_cents   INTEGER NOT NULL DEFAULT 0 CHECK (daily_deposit_cents >= 0),
    daily_withdraw_cents  INTEGER NOT NULL DEFAULT 0 CHECK (daily_withdraw_cents >= 0),
    day_bucket            TEXT    NOT NULL
);

-- Card numbers are never stored. Only a meaningless token from the payment
-- processor, plus the last four digits and the brand so we can show the user
-- which card is which.
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

-- A game is data, not code. config_json holds its reels and paytable, so a
-- new game can be added without changing any Python.
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
    -- The raw 32 bytes, not text. This is what lets a spin be replayed.
    seed            BLOB    NOT NULL,
    stops_json      TEXT    NOT NULL,
    payout_cents    INTEGER NOT NULL CHECK (payout_cents >= 0),
    is_hold_respin  INTEGER NOT NULL DEFAULT 0 CHECK (is_hold_respin IN (0, 1)),
    -- Commissioner test spins live in this table too, so the regulator tests
    -- the real game rather than a copy. Use the live_spins view below instead
    -- of filtering on this by hand.
    is_test         INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
    created_at      TEXT    NOT NULL
);

-- The audit log. Each row stores the hash of the row before it, so edits can
-- be detected. See audit.py.
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
-- Spins and audit entries can only be added, never changed or removed
-- ---------------------------------------------------------------------------
--
-- A spin is a record of something that happened, so it should never change.
--
-- Without these, hiding a spin would not even need a DELETE. Setting
-- is_test = 1 would do it: the money still moved, but the spin disappears
-- from every report, because reports leave test spins out.

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

-- The triggers above stop is_test being changed later, but not being set to 1
-- when the row is first written, which hides the spin just as well.
--
-- So the code does not get to decide. A spin is a test spin only if a
-- commissioner made it, and that is checked here against the users table.

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
-- Views, so the safe query is also the easy one
-- ---------------------------------------------------------------------------
--
-- Without these, every report would have to remember WHERE is_test = 0, and
-- forgetting once quietly gives a wrong number. Read live_spins or test_spins
-- by name instead; only record_spin() should touch the spins table.

CREATE VIEW IF NOT EXISTS live_spins AS
    SELECT * FROM spins WHERE is_test = 0;

CREATE VIEW IF NOT EXISTS test_spins AS
    SELECT * FROM spins WHERE is_test = 1;
