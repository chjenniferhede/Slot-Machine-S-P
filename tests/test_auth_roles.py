"""Independent permission oracle: do not derive expected values from production."""
from dataclasses import FrozenInstanceError
import pytest
from slotmachine import audit, auth, storage

EXPECTED = {
    "play_game": {"player"},
    "deposit": {"player"},
    "withdraw": {"player"},
    "view_own_statistics": {"player"},
    "enable_game": {"technician"},
    "disable_game": {"technician"},
    "run_system_diagnostic": {"technician"},
    "view_audit_log": {"technician"},
    "verify_audit_chain": {"technician"},
    "run_test_session": {"commissioner"},
    "view_game_statistics": {"technician", "commissioner"},
    "view_spin_seeds": {"commissioner"},
}


def identity(conn, role):
    with storage.transaction(conn):
        uid = storage.create_user(conn, "user-" + role.value, "unused", role.value)
    return auth.Session(uid, "user-" + role.value, role)


def test_matrix_has_exact_actions():
    assert set(auth.ROLE_MATRIX) == set(EXPECTED)


@pytest.mark.parametrize("role", list(auth.Role))
@pytest.mark.parametrize("action", list(EXPECTED))
def test_role_matrix(conn, role, action):
    session = identity(conn, role)
    called = []

    @auth.require_role(*auth.ROLE_MATRIX[action])
    def operation(session, conn, argument):
        called.append(argument)
        return argument

    assert operation.__required_roles__ == frozenset(EXPECTED[action])
    if role.value in EXPECTED[action]:
        assert operation(session, conn, "result") == "result"
        assert called == ["result"]
        expected_event = audit.Action.ACCESS_GRANTED
    else:
        with pytest.raises(auth.AccessDenied):
            operation(session, conn, "result")
        assert not called
        expected_event = audit.Action.ACCESS_DENIED
    assert audit.read(conn)[0]["action"] == expected_event
    assert audit.verify_chain(conn).ok
    assert not conn.in_transaction


def test_session_is_frozen():
    session = auth.Session(1, "alice", auth.Role.PLAYER)
    with pytest.raises(FrozenInstanceError):
        session.role = auth.Role.TECHNICIAN


@pytest.mark.parametrize("session", [None, auth.Session(999, "missing", auth.Role.TECHNICIAN)])
def test_no_session_denied(conn, session):
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pytest.fail("denied action ran")
    with pytest.raises(auth.AccessDenied):
        operation(session, conn)
    assert audit.read(conn)[0]["action"] == audit.Action.ACCESS_DENIED


def test_cannot_forge_or_reuse_changed_role(conn):
    session = identity(conn, auth.Role.PLAYER)
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pytest.fail("forged role accepted")
    with pytest.raises(auth.AccessDenied):
        operation(auth.Session(session.user_id, session.username, auth.Role.TECHNICIAN), conn)


def test_deleted_identity_denied(conn):
    session = identity(conn, auth.Role.TECHNICIAN)
    conn.execute("DELETE FROM users WHERE id = ?", (session.user_id,))
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pytest.fail("deleted user accepted")
    with pytest.raises(auth.AccessDenied):
        operation(session, conn)


def test_must_change_pw_blocks_actions(conn):
    password = auth.bootstrap_if_empty(conn)
    session = auth.login(conn, "tech-admin", password)
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        return "allowed"
    with pytest.raises(auth.AccessDenied):
        operation(session, conn)
    auth.change_password(conn, session, password, "A new technician passphrase!")
    assert operation(session, conn) == "allowed"


def test_gate_rechecks_database_flag(conn):
    session = identity(conn, auth.Role.TECHNICIAN)
    conn.execute("UPDATE users SET must_change_pw = 1 WHERE id = ?", (session.user_id,))
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pytest.fail("stale session bypassed flag")
    with pytest.raises(auth.AccessDenied):
        operation(session, conn)


def test_denial_persists_across_reconnect(conn):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pass
    with pytest.raises(auth.AccessDenied):
        operation(None, conn)
    other = storage.connect(path)
    try:
        assert audit.read(other)[0]["action"] == audit.Action.ACCESS_DENIED
    finally:
        other.close()


def test_decorator_joins_caller_transaction(conn):
    session = identity(conn, auth.Role.TECHNICIAN)
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        return conn.in_transaction
    with storage.transaction(conn):
        assert operation(session, conn)
    assert audit.verify_chain(conn).ok


def test_decorator_allows_business_transaction(conn):
    session = identity(conn, auth.Role.TECHNICIAN)
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        with storage.transaction(conn):
            audit.write(conn, session.username, "test.business")
    operation(session, conn)
    assert len(audit.read(conn)) == 2
    assert audit.verify_chain(conn).ok


def test_audit_failure_prevents_action(conn, monkeypatch):
    session = identity(conn, auth.Role.TECHNICIAN)
    def fail(*args, **kwargs):
        raise RuntimeError("audit unavailable")
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        pytest.fail("unlogged action ran")
    monkeypatch.setattr(audit, "write", fail)
    with pytest.raises(RuntimeError):
        operation(session, conn)
    assert not conn.in_transaction


def test_failed_action_retains_access_evidence(conn):
    session = identity(conn, auth.Role.TECHNICIAN)
    @auth.require_role(auth.Role.TECHNICIAN)
    def operation(session, conn):
        with storage.transaction(conn):
            audit.write(conn, session.username, "test.rolled_back")
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        operation(session, conn)
    assert [r["action"] for r in audit.read(conn)] == [audit.Action.ACCESS_GRANTED]


def test_gate_requires_explicit_roles():
    with pytest.raises(ValueError):
        auth.require_role()
    with pytest.raises(ValueError):
        auth.require_role("technician")
