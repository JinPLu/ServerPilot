"""The keepalive recovery entries run when the control plane is unhealthy.

A recovery tool that raises a traceback in exactly the states it exists for is
not a recovery tool, so every unmet precondition must exit cleanly and say what
is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import typer

from serverpilot import cli, daemon


def _daemon_config(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = root / "Application Support" / "ServerPilot"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "state").mkdir(exist_ok=True)
    config = daemon.DaemonConfig(
        base_url="http://127.0.0.1:8787",
        host="127.0.0.1",
        port=8787,
        data_dir=data_dir,
        database_path=data_dir / "state" / "serverpilot.sqlite3",
        inventory_path=data_dir / "inventory.yaml",
        plist_path=root / "LaunchAgents" / "local.serverpilot.daemon.plist",
        log_dir=root / "Logs" / "ServerPilot",
        lock_path=data_dir / "daemon.ensure.lock",
        executable=root / "bin" / "serverpilot",
    )
    monkeypatch.setattr(daemon, "resolve_daemon_config", lambda **_kwargs: config)


def test_a_missing_inventory_reports_the_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _daemon_config(tmp_path, monkeypatch)

    with pytest.raises(typer.Exit) as exit_info:
        cli._keepalive_target("any-endpoint")

    assert exit_info.value.exit_code == 1
    assert "cannot read the control plane" in capsys.readouterr().err


def test_an_unmigrated_database_reports_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _daemon_config(tmp_path, monkeypatch)
    data_dir = tmp_path / "Application Support" / "ServerPilot"
    (data_dir / "inventory.yaml").write_text(
        "schema_version: 1\nprojects: []\nendpoints: []\n", encoding="utf-8"
    )
    (data_dir / "state" / "serverpilot.sqlite3").touch()

    with pytest.raises(typer.Exit) as exit_info:
        cli._keepalive_target("any-endpoint")

    assert exit_info.value.exit_code == 1
    assert "cannot read the control plane" in capsys.readouterr().err


def test_a_paused_endpoint_stays_reachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping occupancy on a paused endpoint is a main reason this exists.

    The regular collector listing filters endpoints by lifecycle state, so it
    would hide exactly the endpoint an operator is trying to clean up.
    """

    import inspect

    source = inspect.getsource(cli._keepalive_target)

    assert "collector_endpoint(" in source
    assert "collector_endpoints(" not in source
