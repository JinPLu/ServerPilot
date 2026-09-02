"""Internal typed adapter registry for external GPU backends.

Adapters are intentionally sealed to the built-in identifiers in this module.
They do not receive BrokerService, database sessions, actors, or claim state.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from serverpilot.config import SSH_BUDGETS, EndpointConfig
from serverpilot.keepalive_protocol import (
    KEEPALIVE_INSPECT_COMMAND,
    KEEPALIVE_PROTOCOL_INFO_CAPABILITIES,
    KEEPALIVE_PROTOCOL_INFO_COMMAND,
    KEEPALIVE_REMOTE_COMMAND,
    KEEPALIVE_SCHEMA_VERSION,
    KeepaliveAttestationRequest,
    KeepaliveAttestationResponse,
    KeepaliveGPUResult,
    KeepaliveRequest,
    KeepaliveResponse,
    validate_gpu_uuid,
)

AdapterId = Literal["raw-ssh", "server-script-v1"]
Capability = Literal[
    "observation",
    "endpoint_keepalive",
]
RawSSHProbe = Literal["endpoint-telemetry"]
ObservationProfile = str


GPU_QUERY = (
    "nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
    "utilization.gpu,utilization.memory,temperature.gpu,power.draw,pstate,pci.bus_id "
    "--format=csv,noheader,nounits"
)
PROCESS_QUERY = (
    "nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory,process_name "
    "--format=csv,noheader,nounits"
)
IDENTITY_QUERY = "hostname; cat /proc/sys/kernel/random/boot_id"
HOST_RESOURCES_QUERY = (
    "getconf _NPROCESSORS_ONLN; "
    "awk '/MemTotal:/{total=$2} /MemAvailable:/{available=$2} "
    "END {printf \"%d %d\\n\", total/1024, available/1024}' /proc/meminfo; "
    "cut -d ' ' -f1 /proc/loadavg; "
    "awk 'NR == 1 && $1 == \"cpu\" {idle=$5+$6; total=0; "
    "for (i=2; i<=NF; i++) total+=$i; printf \"%.0f %.0f\\n\", total, idle}' /proc/stat; "
    "if [ -r /sys/fs/cgroup/cpu.max ] && [ -r /sys/fs/cgroup/cpu.stat ]; then "
    "awk 'FNR==NR {quota=$1; period=$2; next} "
    "$1==\"usage_usec\" && quota != \"\" && period != \"\" "
    "{printf \"%s %s %s\\n\", quota, period, $2}' "
    "/sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat; fi; "
    "if [ -r /sys/fs/cgroup/memory.max ] && [ -r /sys/fs/cgroup/memory.current ]; then "
    "printf \"mem %s %s\\n\" "
    "\"$(cat /sys/fs/cgroup/memory.max)\" "
    "\"$(cat /sys/fs/cgroup/memory.current)\"; fi"
)
GPU_SECTION = "__SERVERPILOT_GPU__"
PROCESS_SECTION = "__SERVERPILOT_PROCESSES__"
PROCESS_DETAILS_SECTION = "__SERVERPILOT_PROCESS_DETAILS__"
IDENTITY_SECTION = "__SERVERPILOT_IDENTITY__"
HOST_RESOURCES_SECTION = "__SERVERPILOT_HOST_RESOURCES__"
GPU_UNAVAILABLE = "__SERVERPILOT_GPU_UNAVAILABLE__"
GPU_CPU_ONLY = "__SERVERPILOT_GPU_CPU_ONLY__"
RAW_SSH_COMBINED_QUERY = (
    f"set -e; printf '{GPU_SECTION}\\n'; "
    f"if command -v nvidia-smi >/dev/null 2>&1; then "
    f"if serverpilot_gpu_output=$({GPU_QUERY} 2>/dev/null); then serverpilot_gpu_rc=0; "
    "else serverpilot_gpu_rc=$?; fi; "
    "if [ \"$serverpilot_gpu_rc\" = 0 ] && [ -n \"$serverpilot_gpu_output\" ]; then "
    "serverpilot_nvidia=1; printf '%s\\n' \"$serverpilot_gpu_output\"; "
    "elif [ \"$serverpilot_gpu_rc\" = 0 ]; then "
    f"serverpilot_nvidia=0; printf '{GPU_CPU_ONLY}\\n'; "
    f"else serverpilot_nvidia=0; printf '{GPU_UNAVAILABLE}\\n'; fi; "
    f"else serverpilot_nvidia=0; printf '{GPU_CPU_ONLY}\\n'; fi; "
    f"printf '{PROCESS_SECTION}\\n'; "
    "if [ \"$serverpilot_nvidia\" = 1 ]; then "
    f"serverpilot_processes=$({PROCESS_QUERY} 2>/dev/null || true); "
    "printf '%s\\n' \"$serverpilot_processes\"; "
    "else serverpilot_processes=''; fi; "
    f"printf '{PROCESS_DETAILS_SECTION}\\n'; "
    "serverpilot_pids=$(printf '%s\\n' \"$serverpilot_processes\" | "
    "awk -F',' '{pid=$2; gsub(/^[ \\t]+|[ \\t]+$/, \"\", pid); "
    "if (pid ~ /^[0-9]+$/ && pid > 0 && !seen[pid]++) "
    "{printf \"%s%s\", separator, pid; separator=\",\"}}'); "
    "if [ -n \"$serverpilot_pids\" ]; then "
    "serverpilot_ps_output=$(ps -o pid=,user=,etimes=,comm= "
    "-p \"$serverpilot_pids\" 2>&1) && serverpilot_ps_rc=0 || serverpilot_ps_rc=$?; "
    "if [ \"$serverpilot_ps_rc\" = 0 ]; then printf '%s\\n' \"$serverpilot_ps_output\"; "
    "elif [ \"$serverpilot_ps_rc\" != 1 ] || [ -n \"$serverpilot_ps_output\" ]; then "
    "printf '%s\\n' \"$serverpilot_ps_output\" >&2; exit \"$serverpilot_ps_rc\"; fi; fi; "
    f"printf '{IDENTITY_SECTION}\\n'; {IDENTITY_QUERY}; "
    f"printf '{HOST_RESOURCES_SECTION}\\n'; {HOST_RESOURCES_QUERY}"
)

# The one sealed observation profile.  There is no table: a second entry that
# differed from this one by a shell string ("linux-host") was a distinction the
# probe already makes for itself, because the combined query reports
# GPU_CPU_ONLY when nvidia-smi is absent.  A plugin's `observe` is the only
# extension point.
BUILTIN_OBSERVATION_PROFILE: ObservationProfile = "linux"


class AdapterRegistryError(KeyError):
    """Raised when a requested sealed adapter id is not registered."""


@dataclass(frozen=True, slots=True)
class AdapterDefinition:
    id: AdapterId
    capabilities: frozenset[Capability]


class AdapterRegistry:
    def __init__(self, definitions: tuple[AdapterDefinition, ...]) -> None:
        by_id = {definition.id: definition for definition in definitions}
        if len(by_id) != len(definitions):
            raise ValueError("adapter definitions must use unique ids")
        self._definitions = MappingProxyType(by_id)

    def get(self, adapter_id: str) -> AdapterDefinition:
        try:
            return self._definitions[adapter_id]  # type: ignore[index]
        except KeyError as exc:
            raise AdapterRegistryError(f"unknown adapter: {adapter_id}") from exc

    def require_capability(self, adapter_id: str, capability: Capability) -> AdapterDefinition:
        definition = self.get(adapter_id)
        if capability not in definition.capabilities:
            raise AdapterRegistryError(f"adapter {adapter_id} does not provide {capability}")
        return definition

    def ids(self) -> tuple[AdapterId, ...]:
        return tuple(self._definitions)


@dataclass(frozen=True, slots=True)
class RawSSHResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False


MAX_RAW_SSH_STDOUT_BYTES = 1_048_576
MAX_RAW_SSH_STDERR_BYTES = 16_384


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    *,
    maximum_bytes: int,
) -> tuple[bytes, bool]:
    """Drain a process stream while retaining only a bounded prefix.

    Continuing to drain after the limit prevents a noisy remote process from
    deadlocking on a full SSH pipe, while retaining no unbounded output in the
    broker process.
    """

    chunks: list[bytes] = []
    retained = 0
    truncated = False
    while chunk := await stream.read(65_536):
        remaining = maximum_bytes - retained
        if remaining > 0:
            chunks.append(chunk[:remaining])
            retained += min(len(chunk), remaining)
        if len(chunk) > remaining:
            truncated = True
    return b"".join(chunks), truncated


def _decode_remote_output(value: bytes, *, stream_name: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"SSH {stream_name} is not valid UTF-8") from exc


def control_socket_path(endpoint: EndpointConfig, control_dir: Path) -> Path:
    """Where this endpoint's multiplexed channel lives.

    Not OpenSSH's own `%C`: that expands to 64 hex characters, and under a macOS
    Application Support directory the result exceeds the 104-byte `sun_path`
    limit, at which point multiplexing silently does not happen.  A 16-character
    digest of the connection triple keeps the whole path short while still
    giving one socket per (user, host, port).
    """

    digest = hashlib.sha256(
        f"{endpoint.ssh_user}@{endpoint.host}:{endpoint.port}".encode()
    ).hexdigest()[:16]
    return control_dir / digest


def observation_ssh_argv(
    endpoint: EndpointConfig, *, control_dir: Path, remote_command: str
) -> tuple[str, ...]:
    """The sealed option list for one observation. One definition, one caller.

    Multiplexing is the point: without it every cycle paid a full TCP handshake,
    key exchange and authentication per host, which over a VPN cost more than
    half of a probe's entire budget and made a slow moment look like six dead
    servers.  `ControlMaster=auto` means the first probe opens the channel and
    every later one rides it; nothing in ServerPilot supervises, restarts or
    health-checks a master, because OpenSSH already does.

    The user's own `~/.ssh/config` is deliberately still read (a host may need
    ProxyJump or a non-default identity), while every option that decides how a
    probe fails is pinned here so an endpoint cannot configure it.
    """

    return (
        "ssh",
        "-T",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"ConnectTimeout={SSH_BUDGETS.connect_seconds}",
        # A channel whose peer vanished must die on its own before the next
        # probe reaches for it; otherwise the probe inherits a dead connection
        # and spends the full command budget discovering that.
        "-o", f"ServerAliveInterval={SSH_BUDGETS.server_alive_interval_seconds}",
        "-o", f"ServerAliveCountMax={SSH_BUDGETS.server_alive_count_max}",
        "-o", "ControlMaster=auto",
        # Quoted: ssh parses an option value by whitespace, and the macOS data
        # directory is under "Application Support". An unquoted path with a
        # space is rejected as "extra arguments at end of line", which fails
        # every probe on the machine while looking like an SSH problem.
        "-o", f'ControlPath="{control_socket_path(endpoint, control_dir)}"',
        "-o", f"ControlPersist={SSH_BUDGETS.control_persist_seconds}",
        # Offer one identity rather than every key an agent holds, so a
        # misconfigured host fails as auth instead of exhausting MaxAuthTries.
        "-o", "IdentitiesOnly=yes",
        "-o", "PreferredAuthentications=publickey",
        "-o", "NumberOfPasswordPrompts=0",
        "-o", "LogLevel=ERROR",
        "-p", str(endpoint.port),
        f"{endpoint.ssh_user}@{endpoint.host}",
        remote_command,
    )


def clear_control_sockets(control_dir: Path) -> None:
    """Drop sockets left by a previous daemon generation.

    A socket file whose master died with the process makes `ControlMaster=auto`
    refuse to multiplex for as long as the file exists, which would silently
    return every probe to the un-multiplexed cost that this design exists to
    remove.  Any master that genuinely survived exits on its own ControlPersist
    timer.
    """

    control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for socket_path in control_dir.iterdir():
        with contextlib.suppress(OSError):
            socket_path.unlink()


async def close_control_socket(endpoint: EndpointConfig, control_dir: Path) -> None:
    """Ask a multiplexed master to exit, for an endpoint we stop observing."""

    path = control_socket_path(endpoint, control_dir)
    if not path.exists():
        return
    with contextlib.suppress(OSError):
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-O", "exit",
            "-o", f'ControlPath="{path}"',
            f"{endpoint.ssh_user}@{endpoint.host}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)


class RawSSHObservationAdapter:
    id: AdapterId = "raw-ssh"

    async def run_probe(
        self,
        endpoint: EndpointConfig,
        *,
        probe: RawSSHProbe,
        control_dir: Path,
    ) -> RawSSHResult:
        if probe != "endpoint-telemetry":
            raise ValueError(f"unknown raw SSH probe: {probe}")
        process = await asyncio.create_subprocess_exec(
            *observation_ssh_argv(
                endpoint,
                control_dir=control_dir,
                remote_command=RAW_SSH_COMBINED_QUERY,
            ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def read_result() -> tuple[bytes, bytes, bool, bool]:
            if getattr(process, "stdout", None) is None or getattr(process, "stderr", None) is None:
                # Compatibility path for a deliberately minimal fake process. The
                # production subprocess always supplies pipes, which takes the
                # bounded streaming path below.
                stdout, stderr = await process.communicate()
                stdout_truncated = len(stdout) > MAX_RAW_SSH_STDOUT_BYTES
                stderr_truncated = len(stderr) > MAX_RAW_SSH_STDERR_BYTES
                return (
                    stdout[:MAX_RAW_SSH_STDOUT_BYTES],
                    stderr[:MAX_RAW_SSH_STDERR_BYTES],
                    stdout_truncated,
                    stderr_truncated,
                )
            (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
                _read_bounded_stream(process.stdout, maximum_bytes=MAX_RAW_SSH_STDOUT_BYTES),
                _read_bounded_stream(process.stderr, maximum_bytes=MAX_RAW_SSH_STDERR_BYTES),
            )
            await process.wait()
            return stdout, stderr, stdout_truncated, stderr_truncated

        try:
            stdout, stderr, stdout_truncated, stderr_truncated = await asyncio.wait_for(
                read_result(), timeout=SSH_BUDGETS.probe_deadline_seconds
            )
        except TimeoutError as exc:
            # OpenSSH enforces ConnectTimeout on connection setup alone, so a
            # session that connected can still hang on the remote work.  This is
            # the whole observation's wall clock, and because it is strictly
            # longer than the connect budget, reaching it always means the
            # remote command is what did not finish.
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise TimeoutError(
                "SSH observation timed out after "
                f"{SSH_BUDGETS.probe_deadline_seconds} seconds for {endpoint.id}"
            ) from exc
        return RawSSHResult(
            returncode=process.returncode or 0,
            stdout=_decode_remote_output(stdout, stream_name="stdout"),
            stderr=_decode_remote_output(stderr, stream_name="stderr"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )


class AdapterCommandError(RuntimeError):
    def __init__(self, message: str, *, access_required: bool = False, uncertain: bool = False):
        super().__init__(message)
        self.access_required = access_required
        self.uncertain = uncertain


def _clean_output(value: str) -> str:
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    return value.replace("\r", "").strip()


class ServerScriptKeepaliveAdapter:
    """Mutating adapter with one sealed, exact-GPU reconciliation operation."""

    id: AdapterId = "server-script-v1"
    connect_timeout_seconds = 8
    timeout_seconds = 45

    @staticmethod
    def _incompatible_helper(message: str) -> AdapterCommandError:
        return AdapterCommandError(
            f"keepalive_helper_incompatible: {message}",
            uncertain=False,
        )

    async def _probe_helper(self, endpoint: EndpointConfig) -> None:
        """Verify the remote helper's v3 protocol before sending a mutation."""

        remote_command = (
            f"cd -- {shlex.quote(endpoint.workspace_path or '')} && "
            f"{KEEPALIVE_PROTOCOL_INFO_COMMAND}"
        )
        process: Any | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"ConnectTimeout={self.connect_timeout_seconds}",
                "-p",
                str(endpoint.port),
                f"{endpoint.ssh_user}@{endpoint.host}",
                remote_command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.connect_timeout_seconds
            )
        except TimeoutError as exc:
            if process is not None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
            raise self._incompatible_helper("protocol-info probe timed out") from exc
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise self._incompatible_helper(
                f"protocol-info probe could not start: {type(exc).__name__}"
            ) from exc
        if process.returncode != 0:
            detail = _clean_output(_decode_remote_output(stderr[:16_384], stream_name="stderr"))
            raise self._incompatible_helper(
                detail[-500:] if detail else f"protocol-info exited with code {process.returncode}"
            )
        try:
            decoded = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise self._incompatible_helper("protocol-info is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise self._incompatible_helper("protocol-info must be a JSON object")
        if decoded.get("kind") != "serverpilot-keepalive":
            raise self._incompatible_helper("protocol-info kind is incompatible")
        if decoded.get("schema_version") != KEEPALIVE_SCHEMA_VERSION:
            raise self._incompatible_helper(
                "protocol-info schema version mismatch: "
                f"expected {KEEPALIVE_SCHEMA_VERSION}, got {decoded.get('schema_version')!r}"
            )
        capabilities = decoded.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not all(isinstance(capability, str) for capability in capabilities)
            or not set(KEEPALIVE_PROTOCOL_INFO_CAPABILITIES) <= set(capabilities)
        ):
            raise self._incompatible_helper(
                "protocol-info capabilities are missing required v3 features"
            )

    async def set_enabled(
        self,
        endpoint: EndpointConfig,
        enabled: bool,
        gpu_uuids: list[str],
    ) -> KeepaliveResponse:
        """Set only ``gpu_uuids`` using the fixed v3 helper command.

        The UUID set is derived by BrokerService from its current endpoint
        lease, never supplied by REST/MCP callers.  The adapter validates the
        request before starting SSH and requires the remote response to map
        exactly back to that set, preventing a helper from widening a request
        to another GPU on the same host.
        """

        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        if not isinstance(gpu_uuids, list):
            raise ValueError("gpu_uuids must be a list")
        try:
            requested = tuple(validate_gpu_uuid(gpu_uuid) for gpu_uuid in gpu_uuids)
        except ValueError as exc:
            raise ValueError("gpu_uuids contains malformed UUIDs") from exc
        if not requested:
            raise ValueError("gpu_uuids cannot be empty")
        if len(set(requested)) != len(requested):
            raise ValueError("gpu_uuids contains duplicates")
        if endpoint.workspace_path is None:
            raise AdapterCommandError("endpoint workspace_path is required for keepalive")
        await self._probe_helper(endpoint)
        payload = KeepaliveRequest(enabled=enabled, gpu_uuids=requested).encode()
        remote_command = (
            f"cd -- {shlex.quote(endpoint.workspace_path)} && {KEEPALIVE_REMOTE_COMMAND}"
        )
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
            "-p",
            str(endpoint.port),
            f"{endpoint.ssh_user}@{endpoint.host}",
            remote_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AdapterCommandError(
                "endpoint keepalive operation timed out; its remote outcome is unknown",
                uncertain=True,
            ) from exc
        cleaned_stderr = _clean_output(_decode_remote_output(stderr[:16_384], stream_name="stderr"))
        if process.returncode != 0:
            message = cleaned_stderr or f"helper exited with code {process.returncode}"
            raise AdapterCommandError(message[-1500:], uncertain=True)
        try:
            response = KeepaliveResponse.decode(stdout)
        except ValueError as exc:
            raise AdapterCommandError(
                f"endpoint keepalive returned an invalid response: {exc}", uncertain=True
            ) from exc
        if response.enabled is not enabled:
            raise AdapterCommandError(
                "endpoint keepalive returned the wrong desired state", uncertain=True
            )
        self._validate_exact_result_mapping(response.results, requested, enabled)
        return response

    async def attest_workers(
        self,
        endpoint: EndpointConfig,
        gpu_uuids: list[str],
    ) -> KeepaliveAttestationResponse:
        """Return sealed helper evidence for exactly ``gpu_uuids``.

        This is read-only: it never starts, stops, or discovers processes.  It
        can only ask the endpoint helper to validate UUIDs supplied by the
        Broker against that helper's own v3 state and fixed worker marker.
        """

        if not isinstance(gpu_uuids, list):
            raise ValueError("gpu_uuids must be a list")
        try:
            requested = tuple(validate_gpu_uuid(gpu_uuid) for gpu_uuid in gpu_uuids)
        except ValueError as exc:
            raise ValueError("gpu_uuids contains malformed UUIDs") from exc
        if not requested:
            raise ValueError("gpu_uuids cannot be empty")
        if len(set(requested)) != len(requested):
            raise ValueError("gpu_uuids contains duplicates")
        if endpoint.workspace_path is None:
            raise AdapterCommandError("endpoint workspace_path is required for keepalive")
        await self._probe_helper(endpoint)
        payload = KeepaliveAttestationRequest(gpu_uuids=requested).encode()
        remote_command = (
            f"cd -- {shlex.quote(endpoint.workspace_path)} && {KEEPALIVE_INSPECT_COMMAND}"
        )
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
            "-p",
            str(endpoint.port),
            f"{endpoint.ssh_user}@{endpoint.host}",
            remote_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AdapterCommandError("endpoint keepalive attestation timed out") from exc
        cleaned_stderr = _clean_output(_decode_remote_output(stderr[:16_384], stream_name="stderr"))
        if process.returncode != 0:
            message = cleaned_stderr or f"helper exited with code {process.returncode}"
            raise AdapterCommandError(f"endpoint keepalive attestation failed: {message[-1500:]}")
        try:
            response = KeepaliveAttestationResponse.decode(stdout)
        except ValueError as exc:
            raise AdapterCommandError(
                f"endpoint keepalive attestation returned an invalid response: {exc}"
            ) from exc
        self._validate_exact_attestation_mapping(response, requested)
        return response

    @staticmethod
    def _validate_exact_result_mapping(
        results: tuple[KeepaliveGPUResult, ...],
        requested: tuple[str, ...],
        enabled: bool,
    ) -> None:
        """Reject reordered-safe but widened, partial, or ambiguous helper output."""

        result_uuids = tuple(result.gpu_uuid for result in results)
        if len(result_uuids) != len(set(result_uuids)) or set(result_uuids) != set(requested):
            raise AdapterCommandError(
                "endpoint keepalive response does not map exactly to requested GPUs",
                uncertain=True,
            )
        expected_status = "running" if enabled else "stopped"
        if any(result.status != expected_status for result in results):
            raise AdapterCommandError(
                "endpoint keepalive response contains inconsistent GPU state",
                uncertain=True,
            )

    @staticmethod
    def _validate_exact_attestation_mapping(
        response: KeepaliveAttestationResponse,
        requested: tuple[str, ...],
    ) -> None:
        observed = tuple(worker.gpu_uuid for worker in response.workers)
        if len(observed) != len(set(observed)) or set(observed) != set(requested):
            raise AdapterCommandError(
                "endpoint keepalive attestation does not map exactly to requested GPUs"
            )


_ADAPTER_DEFINITIONS = (
    AdapterDefinition(id="raw-ssh", capabilities=frozenset({"observation"})),
    AdapterDefinition(
        id="server-script-v1",
        capabilities=frozenset({"endpoint_keepalive"}),
    ),
)

ADAPTER_REGISTRY = AdapterRegistry(_ADAPTER_DEFINITIONS)
RAW_SSH_OBSERVATION_ADAPTER = RawSSHObservationAdapter()
SERVER_SCRIPT_KEEPALIVE_ADAPTER = ServerScriptKeepaliveAdapter()


def endpoint_keepalive_adapter(adapter_id: str) -> ServerScriptKeepaliveAdapter:
    ADAPTER_REGISTRY.require_capability(adapter_id, "endpoint_keepalive")
    if adapter_id != SERVER_SCRIPT_KEEPALIVE_ADAPTER.id:  # sealed exhaustiveness check
        raise AdapterRegistryError(f"unknown endpoint keepalive adapter: {adapter_id}")
    return SERVER_SCRIPT_KEEPALIVE_ADAPTER


def direct_claim_budget_seconds() -> int:
    """Worst-case seconds for one direct claim, derived from the timeouts it uses.

    A claim holds one host, so the cost is one helper probe, one stop, and the
    one fresh observation that proves the cards are empty. Publishing the
    derived number is what lets a caller size its own wait from the group it is
    claiming from, instead of guessing a per-GPU constant that no server-side
    step actually scales with.
    """

    return (
        ServerScriptKeepaliveAdapter.connect_timeout_seconds
        + ServerScriptKeepaliveAdapter.timeout_seconds
        + SSH_BUDGETS.probe_deadline_seconds
    )
