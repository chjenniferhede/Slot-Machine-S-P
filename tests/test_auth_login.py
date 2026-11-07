"""Login state, bootstrap, and transactional failure coverage."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import pytest
from slotmachine import audit, auth, storage

PASSWORD = "My original long passphrase"
NEW_PASSWORD = "My replacement long passphrase"


@pytest.fixture
def registered(conn):
    return auth.register_player(conn, "alice", PASSWORD)


def test_login_success(conn, registered):
    assert auth.login(conn, "ALICE", PASSWORD) == auth.Session(registered, "alice", auth.Role.PLAYER)
    assert audit.read(conn)[0]["action"] == audit.Action.LOGIN_SUCCESS


def test_unknown_user_same_message(conn, registered):
    messages = []
    for username in ["alice", "missing"]:
        with pytest.raises(auth.AuthError) as exc:
            auth.login(conn, username, "incorrect")
        messages.append(str(exc.value))
    assert messages == [auth.LOGIN_FAILURE_MESSAGE] * 2


def test_unknown_user_similar_work(conn, registered):
    with patch.object(auth, "verify_password", wraps=auth.verify_password) as verify:
        for username in ["alice", "missing"]:
            with pytest.raises(auth.AuthError):
                auth.login(conn, username, "incorrect")
        assert verify.call_count == 2
        assert verify.call_args_list[0].args[0].startswith("$argon2id$")
        assert verify.call_args_list[1].args[0] == auth._DUMMY_HASH


def test_lockout_after_five(conn, registered):
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login(conn, "alice", "incorrect")
    row = storage.get_user(conn, registered)
    assert row["failed_count"] == 5
    remaining = datetime.fromisoformat(row["locked_until"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < remaining <= timedelta(minutes=15)
    with pytest.raises(auth.AuthError, match=auth.LOGIN_FAILURE_MESSAGE):
        auth.login(conn, "alice", PASSWORD)
    assert len(audit.read(conn, action=audit.Action.LOCKOUT)) == 1
    assert len(audit.read(conn, action=audit.Action.LOGIN_FAILURE)) == 6
    assert audit.verify_chain(conn).ok


def expire_lockout(conn, uid):
    until = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn.execute("UPDATE users SET failed_count = 5, locked_until = ? WHERE id = ?", (until, uid))


def test_lockout_expires(conn, registered):
    expire_lockout(conn, registered)
    assert auth.login(conn, "alice", PASSWORD).user_id == registered
    row = storage.get_user(conn, registered)
    assert row["failed_count"] == 0 and row["locked_until"] is None


def test_expired_lockout_starts_fresh_attempts(conn, registered):
    expire_lockout(conn, registered)
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", "incorrect")
    row = storage.get_user(conn, registered)
    assert row["failed_count"] == 1 and row["locked_until"] is None


def test_success_resets_counter(conn, registered):
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", "incorrect")
    auth.login(conn, "alice", PASSWORD)
    assert storage.get_user(conn, registered)["failed_count"] == 0


def test_failure_persists_after_reconnect(conn, registered):
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", "incorrect")
    other = storage.connect(conn.execute("PRAGMA database_list").fetchone()["file"])
    try:
        assert storage.get_user(other, registered)["failed_count"] == 1
        assert audit.read(other)[0]["action"] == audit.Action.LOGIN_FAILURE
    finally:
        other.close()


def test_register_creates_only_player_wallet(conn, registered):
    assert storage.get_user(conn, registered)["role"] == "player"
    assert storage.get_balance(conn, registered).cents == 0
    with pytest.raises(TypeError):
        auth.register_player(conn, "mallory", PASSWORD, role="technician")


def test_duplicate_username_is_clean_error(conn, registered):
    with pytest.raises(auth.AuthError, match="unavailable"):
        auth.register_player(conn, "ALICE", PASSWORD)
    assert storage.count_users(conn) == 1


@pytest.mark.parametrize("username", ["", "ab", "x" * 33, "a\nb", "' OR '1'='1"])
def test_bad_username_rejected(conn, username):
    with pytest.raises(auth.AuthError):
        auth.register_player(conn, username, PASSWORD)
    assert storage.count_users(conn) == 0


def test_bootstrap_creates_one_technician(conn):
    password = auth.bootstrap_if_empty(conn)
    assert password and len(password) >= 24
    row = storage.get_user_by_username(conn, "tech-admin")
    assert row["role"] == "technician" and row["must_change_pw"] == 1
    assert storage.get_account(conn, row["id"]) is None
    assert auth.verify_password(row["pw_hash"], password)
    assert auth.bootstrap_if_empty(conn) is None
    assert storage.count_users(conn) == 1
    assert audit.read(conn)[0]["action"] == audit.Action.BOOTSTRAP_TECHNICIAN_CREATED


def test_bootstrap_noop_when_any_user_exists(conn, registered):
    assert auth.bootstrap_if_empty(conn) is None


def test_bootstrap_random_passwords(tmp_path):
    passwords = []
    for name in ["one.db", "two.db"]:
        conn = storage.connect(tmp_path / name)
        try:
            storage.init_db(conn)
            passwords.append(auth.bootstrap_if_empty(conn))
        finally:
            conn.close()
    assert passwords[0] != passwords[1]


def test_concurrent_bootstrap_only_once(conn):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    def bootstrap():
        other = storage.connect(path)
        try:
            return auth.bootstrap_if_empty(other)
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: bootstrap(), range(2)))
    assert sum(pw is not None for pw in results) == 1
    assert storage.count_users(conn) == 1
    assert audit.verify_chain(conn).ok


def test_concurrent_failures_counted(conn, registered):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    def attempt():
        other = storage.connect(path)
        try:
            with pytest.raises(auth.AuthError):
                auth.login(other, "alice", "incorrect")
        finally:
            other.close()
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(lambda _: attempt(), range(5)))
    assert storage.get_user(conn, registered)["failed_count"] == 5
    assert storage.get_user(conn, registered)["locked_until"] is not None
    assert audit.verify_chain(conn).ok


def test_change_password_requires_current(conn, registered):
    session = auth.login(conn, "alice", PASSWORD)
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, session, "incorrect", NEW_PASSWORD)
    assert auth.verify_password(storage.get_user(conn, registered)["pw_hash"], PASSWORD)
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, session, PASSWORD, "short")
    with pytest.raises(auth.AuthError):
        auth.change_password(conn, session, PASSWORD, PASSWORD)
    auth.change_password(conn, session, PASSWORD, NEW_PASSWORD)
    assert auth.login(conn, "alice", NEW_PASSWORD) == session
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", PASSWORD)
    assert audit.read(conn, action=audit.Action.PASSWORD_CHANGE)


def test_password_never_logged(conn, registered):
    auth.login(conn, "alice", PASSWORD)
    with pytest.raises(auth.AuthError):
        auth.login(conn, "alice", "hunter2correct")
    session = auth.login(conn, "alice", PASSWORD)
    auth.change_password(conn, session, PASSWORD, NEW_PASSWORD)
    logs = repr([tuple(row) for row in audit.read(conn)])
    for secret in [PASSWORD, NEW_PASSWORD, "hunter2correct", "$argon2"]:
        assert secret not in logs


@pytest.mark.parametrize("operation", ["register", "bootstrap", "login", "failure", "change"])
def test_audit_failure_rolls_back_state(conn, operation):
    if operation in {"login", "failure", "change"}:
        uid = auth.register_player(conn, "alice", PASSWORD)
        session = auth.login(conn, "alice", PASSWORD)
    before = [tuple(row) for row in conn.execute("SELECT * FROM users")]
    logs_before = len(audit.read(conn))
    with patch.object(audit, "write", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            if operation == "register":
                auth.register_player(conn, "alice", PASSWORD)
            elif operation == "bootstrap":
                auth.bootstrap_if_empty(conn)
            elif operation in {"login", "failure"}:
                auth.login(conn, "alice", PASSWORD if operation == "login" else "incorrect")
            else:
                auth.change_password(conn, session, PASSWORD, NEW_PASSWORD)
    assert [tuple(row) for row in conn.execute("SELECT * FROM users")] == before
    assert len(audit.read(conn)) == logs_before
    assert audit.verify_chain(conn).ok
    assert not conn.in_transaction


def test_rehash_preserves_forced_change(conn):
    password = auth.bootstrap_if_empty(conn)
    with patch.object(auth.PasswordHasher, "check_needs_rehash", return_value=True):
        session = auth.login(conn, "tech-admin", password)
    assert auth.must_change_password(conn, session)


def test_corrupt_hash_is_generic_login_failure(conn, registered):
    conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", ("corrupt", registered))
    with pytest.raises(auth.AuthError, match=auth.LOGIN_FAILURE_MESSAGE):
        auth.login(conn, "alice", PASSWORD)
    assert storage.get_user(conn, registered)["failed_count"] == 1
