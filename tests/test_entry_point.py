"""Identity CLI bootstrap, hidden password prompts, and error boundaries."""
from unittest.mock import patch
import pytest
from slotmachine import audit, auth, storage
from slotmachine.__main__ import main


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "slotmachine" in capsys.readouterr().out


def test_init_creates_database_once(tmp_path, capsys):
    db = tmp_path / "casino.db"
    assert main(["--db", str(db), "--init"]) == 0
    first = capsys.readouterr().out
    assert "username: tech-admin" in first and "password:" in first
    assert main(["--db", str(db), "--init"]) == 0
    assert "password:" not in capsys.readouterr().out


def test_runs_with_no_arguments(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "3")
    assert main([]) == 0
    assert "slotmachine" in capsys.readouterr().out


def test_forced_change_before_account_menu(tmp_path, capsys):
    db = tmp_path / "casino.db"
    conn = storage.connect(db)
    storage.init_db(conn)
    password = auth.bootstrap_if_empty(conn)
    conn.close()
    replacement = "A replacement technician phrase"
    with patch("builtins.input", side_effect=["1", "tech-admin", "2", "3"]), patch(
        "getpass.getpass", side_effect=[password, password, replacement, replacement]
    ):
        assert main(["--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert output.index("Password changed.") < output.index("Signed in as")
    assert password not in output and replacement not in output
    conn = storage.connect(db)
    try:
        session = auth.login(conn, "tech-admin", replacement)
        assert not auth.must_change_password(conn, session)
        assert audit.verify_chain(conn).ok
    finally:
        conn.close()


def test_cancel_forced_change_never_reaches_account_menu(tmp_path, capsys):
    db = tmp_path / "casino.db"
    conn = storage.connect(db)
    storage.init_db(conn)
    password = auth.bootstrap_if_empty(conn)
    conn.close()
    with patch("builtins.input", side_effect=["1", "tech-admin"]), patch(
        "getpass.getpass", side_effect=[password, EOFError]
    ):
        assert main(["--db", str(db)]) == 0
    assert "Signed in as" not in capsys.readouterr().out


def test_wrong_login_clean_message(tmp_path, capsys):
    with patch("builtins.input", side_effect=["1", "missing", "3"]), patch(
        "getpass.getpass", return_value="incorrect"
    ):
        assert main(["--db", str(tmp_path / "casino.db")]) == 0
    output = capsys.readouterr().out
    assert auth.LOGIN_FAILURE_MESSAGE in output and "Traceback" not in output


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_terminal_cancel_is_clean(tmp_path, capsys, error):
    with patch("builtins.input", side_effect=error):
        assert main(["--db", str(tmp_path / "casino.db")]) == 0
    assert "Goodbye" in capsys.readouterr().out


def test_database_error_is_clean(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "missing" / "casino.db")]) == 1
    assert "Traceback" not in capsys.readouterr().out


def test_access_denied_boundary(tmp_path, capsys):
    with patch("slotmachine.__main__._identity_menu", side_effect=auth.AccessDenied("Denied")):
        assert main(["--db", str(tmp_path / "casino.db")]) == 1
    output = capsys.readouterr().out
    assert "Denied" in output and "Traceback" not in output


def test_cli_register_and_login(tmp_path, capsys):
    password = "A newly registered player's phrase"
    with patch("builtins.input", side_effect=["2", "alice", "1", "ALICE", "2", "3"]), patch(
        "getpass.getpass", side_effect=[password, password, password]
    ):
        assert main(["--db", str(tmp_path / "casino.db")]) == 0
    output = capsys.readouterr().out
    assert "Player registered" in output and "Signed in as alice (player)" in output
    assert password not in output


def test_forced_change_retries_mismatch(tmp_path, capsys):
    db = tmp_path / "casino.db"
    conn = storage.connect(db)
    storage.init_db(conn)
    password = auth.bootstrap_if_empty(conn)
    conn.close()
    replacement = "A replacement technician phrase"
    with patch("builtins.input", side_effect=["1", "tech-admin", "2", "3"]), patch(
        "getpass.getpass", side_effect=[
            password, password, replacement, "mismatch", password, replacement, replacement
        ]
    ):
        assert main(["--db", str(db)]) == 0
    output = capsys.readouterr().out
    assert "Passwords do not match" in output
    assert output.index("Password changed.") < output.index("Signed in as")
