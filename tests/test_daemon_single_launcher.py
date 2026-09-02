"""One install may run the daemon, and the retired agent cannot come back.

Two builds on one machine used to take turns writing the same launch agent, at
the same label and port, into the same log file, each restarting the other. The
log that resulted was 12 MB of address conflicts and unresolvable migration
revisions that belonged to neither build alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverpilot.daemon import LEGACY_DAEMON_LABEL, DaemonError, _daemon_executable


def _install_fake_tool(home: Path) -> Path:
    executable = home / ".local/share/uv/tools/serverpilot/bin/serverpilot"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_the_daemon_executable_has_exactly_one_resolution(tmp_path: Path) -> None:
    """Whatever is first on PATH is not a candidate, and neither is an env override."""

    expected = _install_fake_tool(tmp_path)
    decoy = tmp_path / "decoy/serverpilot"
    decoy.parent.mkdir(parents=True, exist_ok=True)
    decoy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    decoy.chmod(0o755)

    environment = {
        "PATH": str(decoy.parent),
        "SERVERPILOT_DAEMON_EXECUTABLE": str(decoy),
    }
    assert _daemon_executable(environment, tmp_path) == expected.resolve()


def test_a_missing_install_is_an_error_not_a_fallback(tmp_path: Path) -> None:
    """Silently running some other build is worse than refusing to start."""

    with pytest.raises(DaemonError) as excinfo:
        _daemon_executable({}, tmp_path)
    assert "uv tool install" in str(excinfo.value)


def test_install_removes_the_retired_agents_plist(tmp_path: Path) -> None:
    """Unloading a plist that stays on disk only postpones it to the next login.

    The retired agent binds the same port this daemon wants, and the loser logs
    an address conflict that nothing traces back to a plist nobody remembers.
    """

    from serverpilot.daemon import DaemonConfig, MacOSDaemonManager

    launch_agents = tmp_path / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    legacy_plist = launch_agents / f"{LEGACY_DAEMON_LABEL}.plist"
    legacy_plist.write_text("<plist/>", encoding="utf-8")

    config = DaemonConfig(
        base_url="http://127.0.0.1:8787",
        host="127.0.0.1",
        port=8787,
        data_dir=tmp_path / "data",
        database_path=tmp_path / "data/state/serverpilot.sqlite3",
        inventory_path=tmp_path / "data/inventory.yaml",
        plist_path=launch_agents / "local.serverpilot.daemon.plist",
        log_dir=tmp_path / "logs",
        lock_path=tmp_path / "data/daemon.ensure.lock",
        executable=_install_fake_tool(tmp_path),
    )
    manager = MacOSDaemonManager(config)
    manager._legacy_loaded = lambda: False  # type: ignore[method-assign]

    assert manager._bootout_legacy_if_loaded() is True
    assert not legacy_plist.exists()
    # Idempotent: with nothing left to remove there is nothing to report.
    assert manager._bootout_legacy_if_loaded() is False
