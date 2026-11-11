from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

import serverpilot.server_keepalive as keepalive_module
from serverpilot.adapters import (
    AdapterCommandError,
    AdapterRegistryError,
    ServerScriptKeepaliveAdapter,
    endpoint_keepalive_adapter,
    endpoint_ssh_argv,
)
from serverpilot.config import EndpointConfig
from serverpilot.keepalive_protocol import (
    KEEPALIVE_IMPLEMENTATION_VERSION,
    KEEPALIVE_INSPECT_COMMAND,
    KEEPALIVE_PROTOCOL_INFO_CAPABILITIES,
    KEEPALIVE_SCHEMA_VERSION,
    KeepaliveAttestationRequest,
    KeepaliveAttestationResponse,
    KeepaliveGPUResult,
    KeepaliveProtocolError,
    KeepaliveRequest,
    KeepaliveResponse,
    KeepaliveTargets,
    KeepaliveWorkerAttestation,
    keepalive_protocol_info,
)
from serverpilot.server_keepalive import (
    DUTY_PERIOD_SECONDS,
    WORKER_MEMORY_SLACK_BYTES,
    WORKER_PROCESS_MARKER,
    KeepaliveProcessIdentity,
    LocalKeepaliveController,
    TorchSubprocessProvider,
    default_state_directory,
    gpu_memory_hold_bytes,
    handle_attestation,
    handle_request,
    hold_host_memory,
    hold_utilization,
    next_host_memory_hold,
)

GPU_A = "GPU-00000000-0000-0000-0000-000000000001"
GPU_B = "GPU-00000000-0000-0000-0000-000000000002"
GPU_C = "GPU-00000000-0000-0000-0000-000000000003"
KNOWN_GPUS = {GPU_A, GPU_B, GPU_C}
DEFAULT_TARGETS = KeepaliveTargets()
GIB = 1024 * 1024 * 1024


def test_torch_provider_resolves_uuid_to_pci_ordered_cuda_ordinal_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_query(argument: str) -> str:
        calls.append(argument)
        return (
            f"7, {GPU_A}, 00000000:AF:00.0\n"
            f"3, {GPU_B}, 00000000:01:00.0\n"
        )

    monkeypatch.setattr("serverpilot.server_keepalive._run_nvidia_smi_query", fake_query)
    provider = TorchSubprocessProvider()

    assert provider._cuda_visible_device(GPU_A) == "1"
    assert provider._cuda_visible_device(GPU_B) == "0"
    assert calls == ["--query-gpu=index,uuid,pci.bus_id"]


def test_torch_provider_sets_pci_order_and_derived_ordinal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 9876

        @staticmethod
        def poll() -> None:
            return None

    def fake_popen(command: list[str], **options: Any) -> FakeProcess:
        captured["command"] = command
        captured["env"] = options["env"]
        os.write(options["pass_fds"][0], b"READY\n")
        return FakeProcess()

    monkeypatch.setattr("serverpilot.server_keepalive.subprocess.Popen", fake_popen)
    identity = KeepaliveProcessIdentity(
        pid=9876,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=1234,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._capture_worker_process_identity", lambda _pid: identity
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._worker_process_matches", lambda observed: observed == identity
    )
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "4"}

    targets = KeepaliveTargets(
        target_utilization_pct=90, target_gpu_memory_pct=95, target_memory_pct=0
    )
    assert provider.start(GPU_A, targets, 8) == identity
    assert captured["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert captured["env"]["CUDA_VISIBLE_DEVICES"] == "4"
    assert captured["command"][-10:] == [
        "--worker-marker", WORKER_PROCESS_MARKER,
        "--target-utilization-pct", "90",
        "--target-gpu-memory-pct", "95",
        "--target-memory-pct", "0",
        "--host-gpu-count", "8",
    ]


def test_torch_provider_reports_unknown_uuid_in_pci_mapping() -> None:
    provider = TorchSubprocessProvider()
    provider._gpu_ordinals = {GPU_A: "0"}

    with pytest.raises(RuntimeError, match="does not contain requested GPU UUID"):
        provider._cuda_visible_device(GPU_B)


def test_driver_pid_attestation_uses_only_the_fixed_compute_process_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_query(query_argument: str) -> str:
        calls.append(query_argument)
        return f"{GPU_A}, 50101\n{GPU_B}, 50102\n"

    monkeypatch.setattr(keepalive_module, "_run_nvidia_smi_query", fake_query)

    assert keepalive_module._resolve_keepalive_driver_pid(GPU_A) == 50101
    assert calls == ["--query-compute-apps=gpu_uuid,pid"]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("No running compute processes found\n", "exactly one process"),
        (f"{GPU_B}, 50102\n", "exactly one process"),
        (f"{GPU_A}, 50101\n{GPU_A}, 50103\n", "exactly one process"),
        (f"{GPU_A}, invalid\n", "query is invalid"),
    ],
)
def test_driver_pid_attestation_fails_closed_for_nonunique_or_malformed_compute_query(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
    message: str,
) -> None:
    monkeypatch.setattr(
        keepalive_module, "_run_nvidia_smi_query", lambda _query_argument: output
    )

    with pytest.raises(RuntimeError, match=message):
        keepalive_module._resolve_keepalive_driver_pid(GPU_A)


def _result(
    gpu_uuid: str,
    *,
    enabled: bool,
    outcome: str = "unchanged",
) -> KeepaliveGPUResult:
    return KeepaliveGPUResult(
        gpu_uuid=gpu_uuid,
        status="running" if enabled else "stopped",
        outcome=outcome,  # type: ignore[arg-type]
    )


def test_protocol_uses_only_the_per_gpu_request() -> None:
    targets = KeepaliveTargets(
        target_utilization_pct=90, target_gpu_memory_pct=95, target_memory_pct=0
    )
    request = KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B), targets=targets)
    decoded = KeepaliveRequest.decode(request.encode())

    assert decoded == request
    assert json.loads(request.encode()) == {
        "schema_version": 3,
        "enabled": True,
        "gpu_uuids": [GPU_A, GPU_B],
        "target_utilization_pct": 90,
        "target_gpu_memory_pct": 95,
        "target_memory_pct": 0,
    }
    assert KEEPALIVE_SCHEMA_VERSION == 3
    with pytest.raises(KeepaliveProtocolError, match="expected 3"):
        KeepaliveRequest.decode('{"schema_version":2,"enabled":true}')
    with pytest.raises(KeepaliveProtocolError, match="malformed"):
        KeepaliveRequest.decode('{"schema_version":3,"enabled":true,"gpu_uuids":["0;touch /tmp/x"]}')
    # A control plane from before a target still drives this helper: a target
    # its request leaves out means the default.
    assert KeepaliveRequest.decode(
        '{"schema_version":3,"enabled":true,"gpu_uuids":["' + GPU_A + '"],"note":"unused",'
        '"target_utilization_pct":90}'
    ) == KeepaliveRequest(
        enabled=True,
        gpu_uuids=(GPU_A,),
        targets=KeepaliveTargets(
            target_utilization_pct=90, target_gpu_memory_pct=85, target_memory_pct=85
        ),
    )
    for name, out_of_range in (
        *(("target_utilization_pct", value) for value in (9, 101, "90", 90.5, True)),
        *(("target_gpu_memory_pct", value) for value in (9, 101)),
        *(("target_memory_pct", value) for value in (-1, 101)),
    ):
        with pytest.raises(KeepaliveProtocolError, match=name):
            KeepaliveTargets(**{name: out_of_range})
        with pytest.raises(KeepaliveProtocolError, match=name):
            KeepaliveRequest.decode(
                json.dumps(
                    {"schema_version": 3, "enabled": True, "gpu_uuids": [GPU_A], name: out_of_range}
                )
            )
    # Holding no host memory is a choice; a held card always holds some VRAM.
    assert KeepaliveTargets(target_memory_pct=0).target_memory_pct == 0


def test_protocol_info_publishes_v3_helper_capabilities() -> None:
    assert keepalive_protocol_info() == {
        "kind": "serverpilot-keepalive",
        "schema_version": 3,
        # Pinned by tests/test_release_metadata.py; this test owns the
        # schema version and capability list, not the release string.
        "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
        "capabilities": [
            "per_gpu_keepalive",
            "pidfd_identity",
            "pci_bus_id",
            "worker_attestation",
            # Advertised, not required: an older helper still works, at its
            # old fixed load and VRAM and without holding host memory.
            "target_utilization",
            "target_gpu_memory",
            "target_memory",
        ],
    }


def test_attestation_protocol_is_typed_and_rejects_ambiguous_identity_evidence() -> None:
    request = KeepaliveAttestationRequest(gpu_uuids=(GPU_A,))
    assert KeepaliveAttestationRequest.decode(request.encode()) == request
    with pytest.raises(KeepaliveProtocolError, match="request fields are invalid"):
        KeepaliveAttestationRequest.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "gpu_uuids": [GPU_A],
                    "command": "arbitrary",
                }
            )
        )
    with pytest.raises(KeepaliveProtocolError, match="worker marker is invalid"):
        KeepaliveAttestationResponse.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "workers": [
                        {
                            "gpu_uuid": GPU_A,
                            "pid": 101,
                            "driver_pid": 50_101,
                            "boot_id": "fake-boot-id",
                            "start_time_ticks": 1010,
                            "worker_marker": "foreign-worker",
                        }
                    ],
                }
            )
        )
    with pytest.raises(KeepaliveProtocolError, match="driver PID is invalid"):
        KeepaliveAttestationResponse.decode(
            json.dumps(
                {
                    "schema_version": 3,
                    "workers": [
                        {
                            "gpu_uuid": GPU_A,
                            "pid": 101,
                            "driver_pid": 0,
                            "boot_id": "fake-boot-id",
                            "start_time_ticks": 1010,
                            "worker_marker": WORKER_PROCESS_MARKER,
                        }
                    ],
                }
            )
        )


def test_response_decodes_each_gpu_result() -> None:
    valid = KeepaliveResponse(enabled=True, results=(_result(GPU_A, enabled=True),)).encode()
    assert KeepaliveResponse.decode(valid).results[0].gpu_uuid == GPU_A
    decoded = KeepaliveResponse.decode(
        json.dumps(
            {
                "schema_version": 3,
                "enabled": False,
                "results": [
                    {"gpu_uuid": GPU_A, "status": "stopped", "outcome": "unchanged"},
                    {"gpu_uuid": GPU_B, "status": "stopped", "outcome": "stopped"},
                ],
                "note": "unused",
            }
        )
    )
    assert [result.gpu_uuid for result in decoded.results] == [GPU_A, GPU_B]


def test_keepalive_adapter_factory_is_sealed() -> None:
    assert endpoint_keepalive_adapter("server-script-v1").id == "server-script-v1"
    with pytest.raises(AdapterRegistryError, match="does not provide endpoint_keepalive"):
        endpoint_keepalive_adapter("raw-ssh")


def test_adapter_uses_fixed_ssh_command_and_exact_json_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any], bytes]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, payload: bytes = b"") -> tuple[bytes, bytes]:
            calls.append((command, kwargs, payload))
            if command[-1].endswith("--protocol-info"):
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode("utf-8"),
                    b"",
                )
            return KeepaliveResponse(
                enabled=False,
                results=(_result(GPU_A, enabled=False, outcome="stopped"),),
            ).encode(), b""

    command: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = {}

    async def fake_create_subprocess_exec(*args: Any, **options: Any) -> FakeProcess:
        nonlocal command, kwargs
        command = args
        kwargs = options
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=2202,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    control_dir = Path("/tmp/serverpilot-test-control")
    adapter = ServerScriptKeepaliveAdapter(control_dir=control_dir)
    response = asyncio.run(adapter.set_enabled(endpoint, False, [GPU_A]))

    assert response.results[0].outcome == "stopped"
    # The helper rides the same sealed, multiplexed SSH as observation.
    assert calls[0][0] == endpoint_ssh_argv(
        endpoint,
        control_dir=control_dir,
        remote_command="cd -- /srv/project-a && ./serverpilot-keepalive --protocol-info",
    )
    assert calls[0][2] == b""
    assert calls[1][0] == endpoint_ssh_argv(
        endpoint,
        control_dir=control_dir,
        remote_command="cd -- /srv/project-a && ./serverpilot-keepalive --schema-version 3",
    )
    assert json.loads(calls[1][2]) == {
        "schema_version": 3,
        "enabled": False,
        "gpu_uuids": [GPU_A],
        # The server's own targets, the defaults here; a stop ignores them.
        "target_utilization_pct": 85,
        "target_gpu_memory_pct": 85,
        "target_memory_pct": 85,
    }
    assert {"shell", "env", "pid", "path", "argv", "command", "cuda_visible_devices"}.isdisjoint(
        json.loads(calls[1][2])
    )


def test_adapter_rejects_a_v2_response_with_explicit_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocations: list[tuple[Any, ...]] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            # The preflight probe runs before the mutation and receives the
            # old v2 capability record, so the mutation must never be sent.
            return (
                json.dumps(
                    {
                        "kind": "serverpilot-keepalive",
                        "schema_version": 2,
                        "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                        "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                    }
                ).encode("utf-8"),
                b"",
            )

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FakeProcess:
        invocations.append(_args)
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="schema version mismatch.*expected 3"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, False, [GPU_A]))
    assert len(invocations) == 1
    assert invocations[0][-1].endswith("--protocol-info")


def test_adapter_rejects_unknown_or_mismapped_gpu_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_calls: list[str] = []

    class FakeProcess:
        returncode = 0

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            if not process_calls:
                process_calls.append("probe")
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode("utf-8"),
                    b"",
                )
            process_calls.append("mutation")
            return KeepaliveResponse(
                enabled=True,
                results=(_result(GPU_B, enabled=True, outcome="started"),),
            ).encode(), b""

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="exactly") as exc_info:
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A]))
    assert exc_info.value.uncertain is True
    with pytest.raises(ValueError, match="duplicates"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, [GPU_A, GPU_A]))
    with pytest.raises(ValueError, match="malformed"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(endpoint, True, ["GPU-A;$(id)"]))


def test_adapter_attests_workers_through_the_fixed_inspection_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], bytes]] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command: tuple[Any, ...]) -> None:
            self.command = command

        async def communicate(self, payload: bytes = b"") -> tuple[bytes, bytes]:
            calls.append((self.command, payload))
            if self.command[-1].endswith("--protocol-info"):
                return (
                    json.dumps(
                        {
                            "kind": "serverpilot-keepalive",
                            "schema_version": KEEPALIVE_SCHEMA_VERSION,
                            "implementation_version": KEEPALIVE_IMPLEMENTATION_VERSION,
                            "capabilities": list(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES),
                        }
                    ).encode(),
                    b"",
                )
            return (
                KeepaliveAttestationResponse(
                    workers=(
                        KeepaliveWorkerAttestation(
                            gpu_uuid=GPU_A,
                            pid=101,
                            driver_pid=50_101,
                            boot_id="fake-boot-id",
                            start_time_ticks=1010,
                            worker_marker=WORKER_PROCESS_MARKER,
                        ),
                    )
                ).encode(),
                b"",
            )

    async def fake_create_subprocess_exec(*args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=2202,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    response = asyncio.run(ServerScriptKeepaliveAdapter().attest_workers(endpoint, [GPU_A]))

    assert response.workers[0].pid == 101
    assert response.workers[0].driver_pid == 50_101
    assert calls[1][0][-1] == f"cd -- /srv/project-a && {KEEPALIVE_INSPECT_COMMAND}"
    assert json.loads(calls[1][1]) == {"schema_version": 3, "gpu_uuids": [GPU_A]}
    assert {"shell", "env", "command", "path", "argv", "pid"}.isdisjoint(
        json.loads(calls[1][1])
    )


def test_adapter_rejects_attestation_that_widens_the_requested_gpu_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        returncode = 0

        def __init__(self, command: tuple[Any, ...]) -> None:
            self.command = command

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            if self.command[-1].endswith("--protocol-info"):
                return json.dumps(keepalive_protocol_info()).encode(), b""
            workers = tuple(
                KeepaliveWorkerAttestation(
                    gpu_uuid=gpu_uuid,
                    pid=101 + position,
                    driver_pid=50_101 + position,
                    boot_id="fake-boot-id",
                    start_time_ticks=1010 + position,
                    worker_marker=WORKER_PROCESS_MARKER,
                )
                for position, gpu_uuid in enumerate((GPU_A, GPU_B))
            )
            return KeepaliveAttestationResponse(workers=workers).encode(), b""

    async def fake_create_subprocess_exec(*args: Any, **_options: Any) -> FakeProcess:
        return FakeProcess(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="does not map exactly"):
        asyncio.run(ServerScriptKeepaliveAdapter().attest_workers(endpoint, [GPU_A]))


def test_adapter_attestation_timeout_is_read_only_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class HungProcess:
        returncode: int | None = None
        killed = False

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            await asyncio.Future()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    process = HungProcess()

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> HungProcess:
        return process

    async def probe_without_ssh(_endpoint: EndpointConfig) -> None:
        return None

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = ServerScriptKeepaliveAdapter()
    adapter.timeout_seconds = 0.01
    monkeypatch.setattr(adapter, "_probe_helper", probe_without_ssh)
    endpoint = EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )

    with pytest.raises(AdapterCommandError, match="attestation timed out") as failure:
        asyncio.run(adapter.attest_workers(endpoint, [GPU_A]))

    assert failure.value.uncertain is False
    assert process.killed is True



def _probe_endpoint() -> EndpointConfig:
    return EndpointConfig(
        id="endpoint-a",
        host="gpu.example.test",
        port=22,
        ssh_user="gpu",
        workspace_path="/srv/project-a",
    )


def test_probe_timeout_is_unreachable_not_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A slow handshake used to surface as "upgrade the helper", which sent the
    # operator after a version problem that did not exist.
    invocations: list[tuple[Any, ...]] = []

    class HungProcess:
        returncode = None

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            return None

        async def wait(self) -> int:
            return -9

    async def fake_create_subprocess_exec(*args: Any, **_options: Any) -> HungProcess:
        invocations.append(args)
        return HungProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    adapter = ServerScriptKeepaliveAdapter()
    adapter.probe_timeout_seconds = 0.01

    with pytest.raises(AdapterCommandError, match="keepalive_helper_unreachable") as failure:
        asyncio.run(adapter.set_enabled(_probe_endpoint(), False, [GPU_A]))

    assert "keepalive_helper_incompatible" not in str(failure.value)
    assert failure.value.uncertain is False
    # Only the preflight ran; no mutation was sent.
    assert len(invocations) == 1
    assert invocations[0][-1].endswith("--protocol-info")


def test_probe_ssh_failure_is_unreachable_but_helper_error_is_incompatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    returncode = 255

    class FailedProcess:
        @property
        def returncode(self) -> int:
            return returncode

        async def communicate(self, _payload: bytes = b"") -> tuple[bytes, bytes]:
            return b"", b"ssh: connect to host gpu.example.test port 22: Operation timed out"

    async def fake_create_subprocess_exec(*_args: Any, **_options: Any) -> FailedProcess:
        return FailedProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(AdapterCommandError, match="keepalive_helper_unreachable"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(_probe_endpoint(), False, [GPU_A]))

    returncode = 127
    with pytest.raises(AdapterCommandError, match="keepalive_helper_incompatible"):
        asyncio.run(ServerScriptKeepaliveAdapter().set_enabled(_probe_endpoint(), False, [GPU_A]))


class FakeProvider:
    def __init__(self) -> None:
        self.running: set[int] = set()
        self.started: list[tuple[str, int]] = []
        self.targets: list[tuple[KeepaliveTargets, int]] = []
        self.stopped: list[int] = []

    @staticmethod
    def identity(pid: int) -> KeepaliveProcessIdentity:
        return KeepaliveProcessIdentity(
            pid=pid,
            boot_id="fake-boot-id",
            start_time_ticks=pid * 10,
            worker_marker=WORKER_PROCESS_MARKER,
        )

    def start(
        self, gpu_uuid: str, targets: KeepaliveTargets, host_gpu_count: int
    ) -> KeepaliveProcessIdentity:
        pid = 100 + len(self.started) + 1
        self.started.append((gpu_uuid, pid))
        self.targets.append((targets, host_gpu_count))
        self.running.add(pid)
        return self.identity(pid)

    def is_running(self, identity: KeepaliveProcessIdentity) -> bool:
        return identity.pid in self.running

    def stop(self, identity: KeepaliveProcessIdentity) -> None:
        self.stopped.append(identity.pid)
        self.running.discard(identity.pid)


class FailingSecondProvider(FakeProvider):
    def start(
        self, gpu_uuid: str, targets: KeepaliveTargets, host_gpu_count: int
    ) -> KeepaliveProcessIdentity:
        if self.started:
            raise RuntimeError("second worker failed")
        return super().start(gpu_uuid, targets, host_gpu_count)


def test_local_controller_manages_each_gpu_independently_and_only_stops_own_state(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
        driver_pid_resolver=lambda gpu_uuid: {GPU_A: 50_101, GPU_B: 50_102}[gpu_uuid],
    )

    started = controller.set_enabled(True, [GPU_A, GPU_B], DEFAULT_TARGETS)
    disabled_a = controller.set_enabled(False, [GPU_A], DEFAULT_TARGETS)
    repeated_b = controller.set_enabled(True, [GPU_B], DEFAULT_TARGETS)
    disabled_missing = controller.set_enabled(False, [GPU_C], DEFAULT_TARGETS)

    pid_a = provider.started[0][1]
    pid_b = provider.started[1][1]
    assert [result.outcome for result in started.results] == ["started", "started"]
    assert disabled_a.results == (_result(GPU_A, enabled=False, outcome="stopped"),)
    assert repeated_b.results[0].outcome == "unchanged"
    assert disabled_missing.results[0].outcome == "unchanged"
    assert provider.stopped == [pid_a]
    assert provider.is_running(provider.identity(pid_b))
    stored = json.loads((tmp_path / "keepalive" / "workers.v3.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 3
    assert stored["workers"] == [
        {
            "gpu_uuid": GPU_B,
            "pid": pid_b,
            "boot_id": "fake-boot-id",
            "start_time_ticks": pid_b * 10,
            "worker_marker": WORKER_PROCESS_MARKER,
        }
    ]


def test_local_controller_attests_only_its_live_v3_workers(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
        driver_pid_resolver=lambda gpu_uuid: {GPU_A: 50_101, GPU_B: 50_102}[gpu_uuid],
    )
    controller.set_enabled(True, [GPU_A, GPU_B], DEFAULT_TARGETS)

    response = handle_attestation(
        KeepaliveAttestationRequest(gpu_uuids=(GPU_B, GPU_A)).encode(), controller=controller
    )

    assert [
        (worker.gpu_uuid, worker.pid, worker.driver_pid, worker.start_time_ticks)
        for worker in response.workers
    ] == [
        (GPU_B, 102, 50_102, 1020),
        (GPU_A, 101, 50_101, 1010),
    ]
    assert all(worker.worker_marker == WORKER_PROCESS_MARKER for worker in response.workers)
    provider.running.remove(102)
    with pytest.raises(RuntimeError, match="recorded worker identity is not running"):
        controller.attest_workers([GPU_B])
    with pytest.raises(RuntimeError, match="does not contain requested GPU UUID"):
        controller.attest_workers([GPU_C])


def test_attestation_rejects_a_state_worker_with_a_foreign_marker(tmp_path: Path) -> None:
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "workers": [
                    {
                        "gpu_uuid": GPU_A,
                        "pid": 101,
                        "boot_id": "fake-boot-id",
                        "start_time_ticks": 1010,
                        "worker_marker": "foreign-worker",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="invalid worker identity"):
        handle_attestation(
            KeepaliveAttestationRequest(gpu_uuids=(GPU_A,)).encode(), controller=controller
        )


def test_local_controller_starts_each_gpu_directly_without_batch_rollback(tmp_path: Path) -> None:
    provider = FailingSecondProvider()
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="second worker failed"):
        controller.set_enabled(True, [GPU_A, GPU_B], DEFAULT_TARGETS)

    pid_a = provider.started[0][1]
    assert provider.running == {pid_a}
    stored = json.loads((state_directory / "workers.v3.json").read_text(encoding="utf-8"))
    assert stored["schema_version"] == 3
    assert stored["workers"] == [
        {
            "gpu_uuid": GPU_A,
            "pid": pid_a,
            "boot_id": "fake-boot-id",
            "start_time_ticks": pid_a * 10,
            "worker_marker": WORKER_PROCESS_MARKER,
        }
    ]


def test_local_controller_starts_batch_workers_concurrently(tmp_path: Path) -> None:
    barrier = threading.Barrier(2)

    class ConcurrentProvider(FakeProvider):
        def start(
            self, gpu_uuid: str, targets: KeepaliveTargets, host_gpu_count: int
        ) -> KeepaliveProcessIdentity:
            barrier.wait(timeout=1)
            return super().start(gpu_uuid, targets, host_gpu_count)

    provider = ConcurrentProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    started = controller.set_enabled(True, [GPU_A, GPU_B], DEFAULT_TARGETS)

    assert [result.gpu_uuid for result in started.results] == [GPU_A, GPU_B]
    assert {gpu_uuid for gpu_uuid, _pid in provider.started} == {GPU_A, GPU_B}


def test_local_controller_rejects_a_valid_but_unknown_gpu_before_mutation(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: {GPU_A},
    )

    with pytest.raises(ValueError, match="unknown"):
        controller.set_enabled(True, [GPU_B], DEFAULT_TARGETS)

    assert provider.started == []


def test_local_controller_rejects_legacy_v2_worker_state(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v2.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "workers": [{"gpu_uuid": GPU_A, "pid": 777}],
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "workers.v2.json").chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="legacy keepalive worker state"):
        controller.set_enabled(False, [GPU_A], DEFAULT_TARGETS)

    assert provider.stopped == []
    assert (state_directory / "workers.v2.json").exists()
    assert not (state_directory / "workers.v3.json").exists()


def test_local_controller_rejects_a_pid_only_worker_state_in_v3_container(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "workers": [{"gpu_uuid": GPU_A, "pid": 777}],
            }
        ),
        encoding="utf-8",
    )
    (state_directory / "workers.v3.json").chmod(0o600)
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="invalid worker identity"):
        controller.set_enabled(False, [GPU_A], DEFAULT_TARGETS)

    assert provider.stopped == []
    assert (state_directory / "workers.v3.json").exists()


def test_local_controller_rejects_v2_payload_in_v3_state_path(tmp_path: Path) -> None:
    provider = FakeProvider()
    state_directory = tmp_path / "keepalive"
    state_directory.mkdir(mode=0o700)
    (state_directory / "workers.v3.json").write_text(
        json.dumps({"schema_version": 2, "workers": []}),
        encoding="utf-8",
    )
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    with pytest.raises(RuntimeError, match="schema version mismatch.*expected 3"):
        controller.set_enabled(False, [GPU_A], DEFAULT_TARGETS)

    assert provider.stopped == []
    assert (state_directory / "workers.v3.json").exists()


def test_torch_provider_does_not_signal_a_reused_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_linux_boot_id", lambda: identity.boot_id
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_start_time_ticks", lambda _pid: 101
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_command",
        lambda _pid: (
            b"python",
            b"-m",
            b"serverpilot.server_keepalive",
            b"--internal-worker",
            b"--worker-marker",
            WORKER_PROCESS_MARKER.encode("ascii"),
        ),
    )
    signal_attempts: list[int] = []

    def forbidden_signal(pid: int, _signal: int) -> None:
        signal_attempts.append(pid)

    monkeypatch.setattr("serverpilot.server_keepalive.os.kill", forbidden_signal)

    provider = TorchSubprocessProvider()
    assert provider.is_running(identity) is False
    provider.stop(identity)

    assert signal_attempts == []


def test_pidfd_syscalls_are_used_when_python_omits_native_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int | None, ...]] = []

    class FakeSyscall:
        restype: Any = None

        def __call__(self, *arguments: Any) -> int:
            values = tuple(getattr(argument, "value", argument) for argument in arguments)
            calls.append(values)
            return 17 if values[0] == keepalive_module.LINUX_PIDFD_OPEN_SYSCALL else 0

    class FakeLibc:
        syscall = FakeSyscall()

    monkeypatch.delattr(keepalive_module.os, "pidfd_open", raising=False)
    monkeypatch.delattr(keepalive_module.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(keepalive_module.sys, "platform", "linux")
    monkeypatch.setattr(keepalive_module.ctypes, "CDLL", lambda *_args, **_kwargs: FakeLibc())

    pidfd = keepalive_module._pidfd_open(4321)
    keepalive_module._pidfd_send_signal(pidfd, signal.SIGTERM)

    assert pidfd == 17
    assert calls == [
        (keepalive_module.LINUX_PIDFD_OPEN_SYSCALL, 4321, 0),
        (keepalive_module.LINUX_PIDFD_SEND_SIGNAL_SYSCALL, 17, signal.SIGTERM, None, 0),
    ]


def test_torch_provider_stops_through_the_pinned_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    opened: list[int] = []
    signaled: list[tuple[int, int]] = []
    monkeypatch.setattr("serverpilot.server_keepalive._worker_process_matches", lambda _item: True)

    def fake_open(pid: int) -> int:
        opened.append(pid)
        return read_fd

    monkeypatch.setattr("serverpilot.server_keepalive._pidfd_open", fake_open)
    monkeypatch.setattr(
        "serverpilot.server_keepalive._pidfd_send_signal",
        lambda pidfd, signal_number: signaled.append((pidfd, signal_number)),
    )

    TorchSubprocessProvider().stop(identity)

    assert opened == [identity.pid]
    assert signaled == [(read_fd, signal.SIGTERM)]


def test_torch_provider_requires_the_fixed_worker_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = KeepaliveProcessIdentity(
        pid=4321,
        boot_id="11111111-1111-1111-1111-111111111111",
        start_time_ticks=100,
        worker_marker=WORKER_PROCESS_MARKER,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_linux_boot_id", lambda: identity.boot_id
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_start_time_ticks",
        lambda _pid: identity.start_time_ticks,
    )
    monkeypatch.setattr(
        "serverpilot.server_keepalive._read_process_command",
        lambda _pid: (
            b"python",
            b"-m",
            b"serverpilot.server_keepalive",
            b"--internal-worker",
            b"--worker-marker",
            b"foreign-worker",
        ),
    )

    assert TorchSubprocessProvider().is_running(identity) is False


def test_worker_state_write_is_atomic_and_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )
    controller._ensure_state_directory()
    real_fsync = os.fsync
    fsynced_modes: list[int] = []

    def recording_fsync(descriptor: int) -> None:
        fsynced_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr("serverpilot.server_keepalive.os.fsync", recording_fsync)
    controller._write_identities({GPU_A: FakeProvider.identity(101)})

    assert any(stat.S_ISREG(mode) for mode in fsynced_modes)
    assert any(stat.S_ISDIR(mode) for mode in fsynced_modes)
    directory_syncs_before_delete = sum(stat.S_ISDIR(mode) for mode in fsynced_modes)

    controller._write_identities({})

    assert not controller._state_path.exists()
    assert sum(stat.S_ISDIR(mode) for mode in fsynced_modes) == directory_syncs_before_delete + 1


def test_worker_state_replace_failure_preserves_previous_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_directory = tmp_path / "keepalive"
    controller = LocalKeepaliveController(
        provider=FakeProvider(),
        state_directory=state_directory,
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )
    controller._ensure_state_directory()
    controller._write_identities({GPU_A: FakeProvider.identity(101)})
    previous_payload = controller._state_path.read_bytes()
    replacement_sources: list[Path] = []

    def fail_replace(source: str | os.PathLike[str], _destination: str | os.PathLike[str]) -> None:
        replacement_sources.append(Path(source))
        raise OSError("simulated replace failure")

    monkeypatch.setattr("serverpilot.server_keepalive.os.replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        controller._write_identities(
            {
                GPU_A: FakeProvider.identity(101),
                GPU_B: FakeProvider.identity(102),
            }
        )

    assert replacement_sources[0].parent == state_directory
    assert controller._state_path.read_bytes() == previous_payload
    assert list(state_directory.glob(".workers.v3.*.tmp")) == []


def test_handle_request_returns_each_requested_gpu(tmp_path: Path) -> None:
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
    )

    response = handle_request(
        KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B), targets=DEFAULT_TARGETS).encode(),
        controller=controller,
    )

    assert [(result.gpu_uuid, result.outcome) for result in response.results] == [
        (GPU_A, "started"),
        (GPU_B, "started"),
    ]
    with pytest.raises(KeepaliveProtocolError, match="expected 3"):
        handle_request(b'{"schema_version":2,"enabled":true}', controller=controller)


def test_a_held_card_fills_its_vram_to_the_target_counting_what_the_worker_already_uses() -> None:
    assert DUTY_PERIOD_SECONDS == 0.1
    total = 80 * GIB
    # The worker's context and buffers already use 1 GiB of the card.
    free = total - GIB
    assert gpu_memory_hold_bytes(total, free, 85) == math.ceil(total * 0.85) - GIB
    assert gpu_memory_hold_bytes(total, free, 50) == math.ceil(total * 0.50) - GIB
    # 100% leaves only the driver's slack free.
    assert gpu_memory_hold_bytes(total, free, 100) == free - WORKER_MEMORY_SLACK_BYTES
    # A card already past the target gets nothing more.
    assert gpu_memory_hold_bytes(total, total // 10, 85) == 0
    with pytest.raises(RuntimeError, match="memory is invalid"):
        gpu_memory_hold_bytes(total, total + 1, 85)


def _settle_host_memory(
    *,
    host_gpu_count: int,
    held_cards: int,
    others: int,
    target_pct: int,
    views: list[int],
    steps: int = 40,
) -> tuple[list[int], list[float]]:
    """Run every held card's host memory step together, as their workers would.

    ``views`` gives each view's total; every view sees the same usage, which is
    ``others`` plus what the cards hold. Returns the holds and each step's peak
    use as a fraction of the tightest view.
    """

    holds = [0] * held_cards
    peaks: list[float] = []
    for _ in range(steps):
        used = others + sum(holds)
        observed = [(total, used) for total in views]
        holds = [
            next_host_memory_hold(held, observed, target_pct, host_gpu_count) for held in holds
        ]
        peaks.append((others + sum(holds)) / min(views))
    return holds, peaks


def test_every_held_card_together_brings_host_memory_to_the_target_without_overshooting() -> None:
    total = 512 * GIB
    holds, peaks = _settle_host_memory(
        host_gpu_count=8, held_cards=8, others=40 * GIB, target_pct=85, views=[total]
    )
    assert max(peaks) <= 0.85 + 1e-9
    assert (40 * GIB + sum(holds)) / total == pytest.approx(0.85, abs=0.005)


def test_each_held_card_holds_only_its_share_of_host_memory() -> None:
    total = 512 * GIB
    holds, _peaks = _settle_host_memory(
        host_gpu_count=8, held_cards=2, others=40 * GIB, target_pct=85, views=[total]
    )
    share = total * 0.85 / 8
    assert holds == [int(share), int(share)]


def test_host_memory_comes_back_when_something_else_grows_into_it() -> None:
    total = 512 * GIB
    holds, _peaks = _settle_host_memory(
        host_gpu_count=8, held_cards=8, others=40 * GIB, target_pct=85, views=[total]
    )
    # A workload on the server grows by 200 GiB after the cards were filled.
    grown = 240 * GIB
    for _ in range(40):
        used = grown + sum(holds)
        holds = [next_host_memory_hold(held, [(total, used)], 85, 8) for held in holds]
    assert (grown + sum(holds)) / total == pytest.approx(0.85, abs=0.005)
    # Something else alone past the target leaves the cards holding nothing.
    holds = [10 * GIB] * 8
    others = int(total * 0.95)
    for _ in range(40):
        used = others + sum(holds)
        holds = [next_host_memory_hold(held, [(total, used)], 85, 8) for held in holds]
    assert holds == [0] * 8


def test_host_memory_respects_the_tightest_of_container_and_host() -> None:
    # A container limited to 128 GiB on a 512 GiB host: the App shows the
    # container's figure, and the cards fill it to the target.
    holds, peaks = _settle_host_memory(
        host_gpu_count=4, held_cards=4, others=8 * GIB, target_pct=85, views=[512 * GIB, 128 * GIB]
    )
    assert max(peaks) <= 0.85 + 1e-9
    assert (8 * GIB + sum(holds)) / (128 * GIB) == pytest.approx(0.85, abs=0.005)
    # A host already at 90% from other tenants holds back the container's cards.
    crowded = [(512 * GIB, int(512 * GIB * 0.90)), (128 * GIB, 8 * GIB)]
    assert next_host_memory_hold(0, crowded, 85, 4) == 0


def test_host_memory_target_zero_holds_nothing() -> None:
    assert next_host_memory_hold(0, [(512 * GIB, 0)], 0, 8) == 0


def test_host_memory_hold_keeps_its_pieces_and_lets_go_when_memory_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunk = 1024 * 1024
    readings: list[list[tuple[int, int]] | None] = [
        [(64 * chunk, 0)],
        [(64 * chunk, 16 * chunk)],
        None,
        [(64 * chunk, 0)],
    ]
    held_before_each_step: list[int] = []
    original = keepalive_module.next_host_memory_hold

    def recording_next(held: int, views: list[tuple[int, int]], target_pct: int, count: int) -> int:
        held_before_each_step.append(held // chunk)
        return original(held, views, target_pct, count)

    def read_views() -> list[tuple[int, int]]:
        reading = readings.pop(0)
        if reading is None:
            raise OSError("meminfo is unreadable")
        return reading

    class StopAfterFourPauses(threading.Event):
        pauses: list[float] = []

        def wait(self, timeout: float | None = None) -> bool:
            self.pauses.append(timeout or 0.0)
            if len(self.pauses) == 4:
                self.set()
            return self.is_set()

    monkeypatch.setattr(keepalive_module, "next_host_memory_hold", recording_next)
    stop = StopAfterFourPauses()
    hold_host_memory(
        50, 2, read_views=read_views, stop_event=stop, chunk_bytes=chunk, adjust_seconds=0.25
    )
    # 50% of 64 pieces over two cards is 16 for this one; it keeps them while
    # the reading includes them, lets all go when memory cannot be read, and
    # starts again from nothing.
    assert held_before_each_step == [0, 16, 0]
    assert stop.pauses == [0.25] * 4


def test_default_state_directory_is_persistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker state must outlive a reboot, so the fallback cannot be a temp dir.

    The temp-directory check has to run against the real fallback. Asserting it
    against an ``XDG_STATE_HOME`` pointed at ``tmp_path`` only described the
    test's own scratch directory, which sits under ``/tmp`` on Linux.
    """

    xdg_state = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    assert default_state_directory() == xdg_state / "serverpilot" / "keepalive"

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    fallback = default_state_directory()
    assert fallback == Path.home() / ".local" / "state" / "serverpilot" / "keepalive"
    assert not fallback.is_relative_to(Path(tempfile.gettempdir()))

    monkeypatch.setenv("XDG_STATE_HOME", "relative-state")
    assert default_state_directory() == Path("relative-state/serverpilot/keepalive")


class _SimulatedCard:
    """One GPU queue and a host clock: kernels run in order, the host waits or sleeps.

    ``launch_cost`` is the host time to queue a batch; the card starts a
    batch when both it and the batch are ready.
    """

    def __init__(self, *, batch_seconds: float, launch_cost: float = 0.0002) -> None:
        self.now = 0.0
        self.free_at = 0.0
        self.batch_seconds = batch_seconds
        self.launch_cost = launch_cost
        self.busy: list[tuple[float, float]] = []

    def clock(self) -> float:
        return self.now

    def launch(self):  # type: ignore[no-untyped-def]
        self.now += self.launch_cost
        start = max(self.now, self.free_at)
        self.free_at = start + self.batch_seconds
        self.busy.append((start, self.free_at))
        finished = self.free_at

        def wait() -> None:
            self.now = max(self.now, finished)

        return wait

    def pause(self, seconds: float) -> None:
        self.now += seconds

    def utilization(self, since: float, until: float) -> float:
        covered = sum(max(0.0, min(end, until) - max(start, since)) for start, end in self.busy)
        return covered / (until - since)


@pytest.mark.parametrize("target", [0.30, 0.50, 0.85, 0.90])
@pytest.mark.parametrize("batch_seconds", [0.0005, 0.004, 0.011])
def test_held_card_settles_on_its_target_load(target: float, batch_seconds: float) -> None:
    """The old loop ran one kernel and synchronized each time, so a fast card sat idle
    between launches and a card meant to hold 80% held 43%. Measured over the
    later seconds, the busy share sits at the target whatever a batch costs."""

    card = _SimulatedCard(batch_seconds=batch_seconds)
    hold_utilization(
        target,
        launch_batch=card.launch,
        batch_seconds=batch_seconds,
        stopped=lambda: card.now >= 20.0,
        pause=card.pause,
        clock=card.clock,
    )

    assert card.utilization(5.0, 20.0) == pytest.approx(target, abs=0.02)


def test_a_full_target_never_lets_the_card_idle() -> None:
    card = _SimulatedCard(batch_seconds=0.004)
    hold_utilization(
        1.0,
        launch_batch=card.launch,
        batch_seconds=0.004,
        stopped=lambda: card.now >= 5.0,
        pause=card.pause,
        clock=card.clock,
    )

    assert card.utilization(1.0, 5.0) > 0.99


def test_a_started_worker_is_given_the_requested_targets(tmp_path: Path) -> None:
    targets = KeepaliveTargets(
        target_utilization_pct=90, target_gpu_memory_pct=95, target_memory_pct=60
    )
    provider = FakeProvider()
    controller = LocalKeepaliveController(
        provider=provider,
        state_directory=tmp_path / "keepalive",
        known_gpu_uuids_resolver=lambda: KNOWN_GPUS,
        driver_pid_resolver=lambda gpu_uuid: 50_101,
    )

    handle_request(
        KeepaliveRequest(enabled=True, gpu_uuids=(GPU_A, GPU_B), targets=targets).encode(),
        controller=controller,
    )

    # Each worker learns the server's GPU count, which its host memory share divides by.
    assert provider.targets == [(targets, len(KNOWN_GPUS)), (targets, len(KNOWN_GPUS))]


def test_memory_views_read_the_host_and_a_limited_container_like_the_probe(tmp_path: Path) -> None:
    (tmp_path / "proc").mkdir()
    (tmp_path / "proc" / "meminfo").write_text(
        "MemTotal:       536870912 kB\nMemFree:  1 kB\nMemAvailable:   402653184 kB\n",
        encoding="ascii",
    )
    host = (512 * GIB, 128 * GIB)
    assert keepalive_module.read_memory_views(tmp_path) == [host]

    cgroup = tmp_path / "sys" / "fs" / "cgroup"
    cgroup.mkdir(parents=True)
    (cgroup / "memory.max").write_text("max\n", encoding="ascii")
    (cgroup / "memory.current").write_text(f"{8 * GIB}\n", encoding="ascii")
    assert keepalive_module.read_memory_views(tmp_path) == [host]

    (cgroup / "memory.max").write_text(f"{64 * GIB}\n", encoding="ascii")
    assert keepalive_module.read_memory_views(tmp_path) == [host, (64 * GIB, 8 * GIB)]
