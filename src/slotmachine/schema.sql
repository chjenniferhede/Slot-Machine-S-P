-- Tables for the slot machine, applied by storage.init_db().
-- Foreign keys are switched on per connection in storage.connect(), not here.


-- Everyone who can log in.
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    -- NOCASE: 'Alice' and 'alice' are the same account.
    username        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash         TEXT    NOT NULL,
    role            TEXT    NOT NULL CHECK (role IN ('player', 'technician', 'commissioner')),
    must_change_pw  INTEGER NOT NULL DEFAULT 0 CHECK (must_change_pw IN (0, 1)),
    -- Consecutive failed logins, and the time until which login is refused.
    failed_count    INTEGER NOT NULL DEFAULT 0 CHECK (failed_count >= 0),
    locked_until    TEXT,
    created_at      TEXT    NOT NULL
);

-- A player's money, and how much they have moved today.
CREATE TABLE IF NOT EXISTS accounts (
    user_id               INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance_cents         INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    daily_deposit_cents   INTEGER NOT NULL DEFAULT 0 CHECK (daily_deposit_cents >= 0),
    daily_withdraw_cents  INTEGER NOT NULL DEFAULT 0 CHECK (daily_withdraw_cents >= 0),
    -- The UTC date the two daily totals above belong to.
    day_bucket            TEXT    NOT NULL
);

-- Cards a player has linked. The card number itself is never stored.
CREATE TABLE IF NOT EXISTS payment_methods (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    -- Meaningless id from the payment processor, used in place of the number.
    token      TEXT    NOT NULL UNIQUE,
    last4      TEXT    NOT NULL CHECK (length(last4) = 4),
    brand      TEXT    NOT NULL,
    exp_month  INTEGER NOT NULL CHECK (exp_month BETWEEN 1 AND 12),
    exp_year   INTEGER NOT NULL CHECK (exp_year >= 2000),
    added_at   TEXT    NOT NULL
);

-- The playable games.
CREATE TABLE IF NOT EXISTS games (
    id           TEXT    PRIMARY KEY,
    name         TEXT    NOT NULL,
    -- The game's reels and paytable, so a new game needs no code change.
    config_json  TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at   TEXT    NOT NULL
);

-- One visit to a machine, from buying in to cashing out.
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    game_id        TEXT    NOT NULL REFERENCES games(id),
    -- Money moved into the machine and not yet cashed out.
    credits_cents  INTEGER NOT NULL DEFAULT 0 CHECK (credits_cents >= 0),
    started_at     TEXT    NOT NULL,
    ended_at       TEXT
);

-- Every spin ever played.
CREATE TABLE IF NOT EXISTS spins (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES sessions(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    game_id         TEXT    NOT NULL REFERENCES games(id),
    bet_cents       INTEGER NOT NULL CHECK (bet_cents > 0),
    -- The random seed; replaying it reproduces this spin exactly.
    seed            BLOB    NOT NULL,
    -- Where each reel stopped.
    stops_json      TEXT    NOT NULL,
    payout_cents    INTEGER NOT NULL CHECK (payout_cents >= 0),
    -- This spin followed a hold rather than being a fresh one.
    is_hold_respin  INTEGER NOT NULL DEFAULT 0 CHECK (is_hold_respin IN (0, 1)),
    -- A commissioner test spin, left out of real statistics.
    is_test         INTEGER NOT NULL DEFAULT 0 CHECK (is_test IN (0, 1)),
    created_at      TEXT    NOT NULL
);

-- Security events. Each row stores the hash of the row before it, so edits
-- can be detected. See audit.py.
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


-- Spins and audit entries can only be added, never changed or deleted.
-- Otherwise a spin could be hidden from every report by setting is_test = 1.

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

-- is_test is not the caller's to choose: only a commissioner makes test spins.
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


-- Read these instead of filtering spins on is_test by hand.
CREATE VIEW IF NOT EXISTS live_spins AS
    SELECT * FROM spins WHERE is_test = 0;

CREATE VIEW IF NOT EXISTS test_spins AS
    SELECT * FROM spins WHERE is_test = 1;
