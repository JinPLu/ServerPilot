from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from serverpilot.adapters import (
    ADAPTER_REGISTRY,
    MAX_RAW_SSH_STDERR_BYTES,
    MAX_RAW_SSH_STDOUT_BYTES,
    RAW_SSH_COMBINED_QUERY,
    AdapterRegistryError,
    RawSSHObservationAdapter,
    observation_ssh_argv,
)
from serverpilot.config import EndpointConfig


def test_registry_is_sealed_to_known_adapters() -> None:
    assert ADAPTER_REGISTRY.ids() == ("raw-ssh", "server-script-v1")
    assert ADAPTER_REGISTRY.require_capability("raw-ssh", "observation").id == "raw-ssh"
    assert (
        ADAPTER_REGISTRY.require_capability("server-script-v1", "endpoint_keepalive").id
        == "server-script-v1"
    )
    with pytest.raises(AdapterRegistryError, match="unknown adapter"):
        ADAPTER_REGISTRY.get("unknown")
    with pytest.raises(AdapterRegistryError, match="does not provide endpoint_keepalive"):
        ADAPTER_REGISTRY.require_capability("raw-ssh", "endpoint_keepalive")


def test_raw_ssh_adapter_runs_fixed_ssh_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok\n", b""

    async def fake_create_subprocess_exec(*command: Any, **_kwargs: Any) -> FakeProcess:
        calls.append(command)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a", host="gpu.example.test", port=2202, ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    result = asyncio.run(
        RawSSHObservationAdapter().run_probe(
            endpoint,
            probe="endpoint-telemetry",
            control_dir=tmp_path,
        )
    )

    assert result.stdout == "ok\n"
    assert calls == [
        observation_ssh_argv(
            endpoint, control_dir=tmp_path, remote_command=RAW_SSH_COMBINED_QUERY
        )
    ]


def test_raw_ssh_adapter_rejects_arbitrary_probe(tmp_path: Path) -> None:
    adapter = RawSSHObservationAdapter()
    endpoint = EndpointConfig(
        id="endpoint-a", host="gpu.example.test", port=2202, ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(ValueError, match="unknown raw SSH probe"):
        asyncio.run(
            adapter.run_probe(endpoint, probe="arbitrary", control_dir=tmp_path)  # type: ignore[arg-type]
        )


def test_raw_ssh_adapter_bounds_and_drains_noisy_remote_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FakeProcess:
        returncode = 0

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(b"o" * (MAX_RAW_SSH_STDOUT_BYTES + 1))
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(b"e" * (MAX_RAW_SSH_STDERR_BYTES + 1))
            self.stderr.feed_eof()

        async def wait(self) -> None:
            return None

    async def fake_create_subprocess_exec(*_command: Any, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a", host="gpu.example.test", port=22, ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    result = asyncio.run(
        RawSSHObservationAdapter().run_probe(
            endpoint,
            probe="endpoint-telemetry",
            control_dir=tmp_path,
        )
    )

    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert len(result.stdout) == MAX_RAW_SSH_STDOUT_BYTES
    assert len(result.stderr) == MAX_RAW_SSH_STDERR_BYTES


def test_raw_ssh_adapter_times_out_and_reaps_a_hung_connected_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from serverpilot import adapters as adapters_module
    from serverpilot.config import SSHBudgets

    class FakeProcess:
        returncode: int | None = None
        killed = False

        def __init__(self) -> None:
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self) -> int:
            return self.returncode or 0

    process: FakeProcess | None = None

    async def fake_create_subprocess_exec(*_command: Any, **_kwargs: Any) -> FakeProcess:
        nonlocal process
        process = FakeProcess()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    # The probe deadline is a fixed sum of the two SSH budgets; shrink it here
    # so the timeout path can be exercised without a real 30-second wait.
    monkeypatch.setattr(
        adapters_module,
        "SSH_BUDGETS",
        SSHBudgets(connect_seconds=0, command_seconds=0.01),  # type: ignore[arg-type]
    )
    endpoint = EndpointConfig(
        id="endpoint-a", host="gpu.example.test", port=22, ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(
        TimeoutError,
        match=r"SSH observation timed out after 0\.01 seconds for endpoint-a",
    ):
        asyncio.run(
            RawSSHObservationAdapter().run_probe(
                endpoint,
                probe="endpoint-telemetry",
                control_dir=tmp_path,
            )
        )

    assert process is not None
    assert process.killed is True
