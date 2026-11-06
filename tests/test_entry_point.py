"""The console script must work from day one."""

from __future__ import annotations

import pytest

from slotmachine.__main__ import main


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "slotmachine" in capsys.readouterr().out


def test_init_creates_database(tmp_path) -> None:
    db = tmp_path / "casino.db"
    assert main(["--db", str(db), "--init"]) == 0
    assert db.exists()


def test_runs_with_no_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "slotmachine" in capsys.readouterr().out
