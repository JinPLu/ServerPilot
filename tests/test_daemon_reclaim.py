"""Reclaim hands the port back to launchd; it must never stop the owned daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverpilot import daemon
from serverpilot.daemon import DaemonConfig, DaemonError, MacOSDaemonManager
from tests.test_macos_daemon import _config


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MacOSDaemonManager:
    monkeypatch.setattr(daemon.sys, "platform", "darwin")
    return MacOSDaemonManager(_config(tmp_path))


def test_reclaim_leaves_an_owned_daemon_running(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "probe_ready", lambda _config: {"process_id": 4242})
    monkeypatch.setattr(manager, "_launchd_pid", lambda: 4242)
    monkeypatch.setattr(manager, "status", lambda: {"ready": True})
    killed: list[int] = []
    monkeypatch.setattr(manager, "_terminate_holder", lambda pid, **_: killed.append(pid))
    started: list[bool] = []
    monkeypatch.setattr(manager, "start", lambda **_: started.append(True))

    result = manager.reclaim()

    assert result["reclaimed"] is False
    assert result["reason"] == "already_owned"
    assert killed == []
    assert started == []


def test_reclaim_stops_a_foreign_holder_and_restarts_the_launch_agent(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "probe_ready", lambda _config: {"process_id": 6402})
    monkeypatch.setattr(manager, "_launchd_pid", lambda: None)
    monkeypatch.setattr(manager, "_process_command", staticmethod(lambda _pid: "uv run serverpilot serve"))
    monkeypatch.setattr(manager, "status", lambda: {"ready": True})
    killed: list[int] = []
    monkeypatch.setattr(manager, "_terminate_holder", lambda pid, **_: killed.append(pid))
    started: list[bool] = []
    monkeypatch.setattr(manager, "start", lambda **_: started.append(True))

    result = manager.reclaim()

    assert result["reclaimed"] is True
    assert killed == [6402]
    assert started == [True]
    assert result["stopped"] == {"pid": 6402, "command": "uv run serverpilot serve"}


def test_reclaim_does_not_touch_a_service_that_is_not_this_installation(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # probe_ready rejects a foreign instance id, so an unrelated program
    # answering on the port never reaches the terminate path.
    def refuse(_config: DaemonConfig) -> dict[str, object]:
        raise DaemonError("http://127.0.0.1:8787 is not the installed ServerPilot macOS daemon")

    monkeypatch.setattr(daemon, "probe_ready", refuse)
    killed: list[int] = []
    monkeypatch.setattr(manager, "_terminate_holder", lambda pid, **_: killed.append(pid))

    with pytest.raises(DaemonError):
        manager.reclaim()

    assert killed == []


def test_reclaim_reports_when_nothing_is_serving(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon, "probe_ready", lambda _config: None)
    monkeypatch.setattr(manager, "status", lambda: {"ready": False})
    killed: list[int] = []
    monkeypatch.setattr(manager, "_terminate_holder", lambda pid, **_: killed.append(pid))
    started: list[bool] = []
    monkeypatch.setattr(manager, "start", lambda **_: started.append(True))

    result = manager.reclaim()

    assert result["reclaimed"] is True
    assert result["stopped"] is None
    assert killed == []
    assert started == [True]


def test_a_holder_that_ignores_sigterm_is_left_for_a_human(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Escalating to SIGKILL on a process we only identified by a self-reported
    # pid is not something this command decides on its own.
    monkeypatch.setattr(daemon.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(daemon.time, "sleep", lambda _seconds: None)

    with pytest.raises(DaemonError, match="still holding"):
        manager._terminate_holder(6402, timeout_seconds=-1)


def test_a_holder_that_already_exited_is_not_an_error(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gone(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(daemon.os, "kill", gone)

    manager._terminate_holder(6402)


def test_the_port_conflict_message_names_the_holder(
    manager: MacOSDaemonManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    # "not served by <label>" alone gives an operator nothing to act on.
    monkeypatch.setattr(
        manager, "_process_command", staticmethod(lambda _pid: "uv run serverpilot serve --port 8787")
    )

    message = manager._foreign_holder_message(6402)

    assert "6402" in message
    assert "uv run serverpilot serve --port 8787" in message
    assert "serverpilot daemon reclaim" in message
