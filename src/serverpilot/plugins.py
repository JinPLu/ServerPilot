"""Local server plugins: fixed verbs, discovered executables, fail-closed output.

A plugin is an executable in a known directory. ServerPilot calls it with
fixed verbs and typed flags only. It never interpolates caller strings into a
shell, and it never gives a plugin BrokerService, a database session, or claim
state. Output is validated or rejected; there is no downgrade path.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from serverpilot.adapters import MAX_RAW_SSH_STDOUT_BYTES

PLUGIN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
PLUGIN_CAPABILITIES = frozenset({"observe", "apply", "release"})
TASK_REF_PATTERN = re.compile(r"^[A-Za-z0-9._@+/-]{1,255}$")
ALLOCATION_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
PLUGIN_INFO_TIMEOUT_SECONDS = 8
PLUGIN_OBSERVE_TIMEOUT_SECONDS = 45
PLUGIN_MUTATION_TIMEOUT_SECONDS = 60
MAX_PLUGIN_OUTPUT_BYTES = MAX_RAW_SSH_STDOUT_BYTES
PLUGIN_SCHEMA_VERSION = 3
# An `apply` that found nothing free right now exits with this instead of the
# generic failure code, so the outcome is machine-readable rather than inferred
# from whatever the scheduler happened to print.
PLUGIN_NO_CAPACITY_EXIT_CODE = 3

# How a lease on this profile ends.  A cluster that kills the job at a time
# limit and a cluster that holds it until release are not interchangeable for
# the same experiment, so the difference is declared rather than inferred.
PROFILE_LEASE_ENDS = frozenset({"on_release", "hard_kill_at_time_limit"})
PROFILE_LIMIT_KEYS = frozenset(
    {"lease_ends", "max_lease_seconds", "apply_max_seconds", "queues"}
)
MAX_PROFILE_LEASE_SECONDS = 30 * 24 * 3600
# A plugin may not declare an apply that outlasts what a caller will wait for.
# `client.CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS` is the caller's side of this
# bound, and `tests/test_client.py` holds the two together.
MAX_PROFILE_APPLY_SECONDS = 180

# ServerPilot's own per-card allocator: it holds the cards until the caller
# releases them, and it never queues. It declares no `apply_max_seconds`:
# unlike a plugin, its cost is not published by the profile but derived from
# the adapter and collector timeouts a direct claim actually spends, which the
# server group projects.
DIRECT_PROFILE_LIMITS: dict[str, Any] = {
    "lease_ends": "on_release",
    "max_lease_seconds": None,
    "queues": False,
}

BUILTIN_OBSERVATION_PROFILES: tuple[str, ...] = (
    "linux-nvidia",
    "linux-host",
    "server-script-v1",
)

BUILTIN_PROFILE_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "linux-nvidia",
        "display_name": "标准 NVIDIA 采集",
        "description": "使用内置、只读的 Linux NVIDIA 观测配置。",
        "source": "builtin",
        "capabilities": ["observe"],
        "limits": dict(DIRECT_PROFILE_LIMITS),
    },
    {
        "id": "linux-host",
        "display_name": "主机容量采集",
        "description": "使用内置、只读的 Linux 主机容量观测配置。",
        "source": "builtin",
        "capabilities": ["observe"],
        "limits": dict(DIRECT_PROFILE_LIMITS),
    },
    {
        "id": "server-script-v1",
        "display_name": "服务器采集脚本",
        "description": "使用远端密封只读采集脚本；不能输入命令或容器参数。",
        "source": "builtin",
        "capabilities": ["observe"],
        "limits": dict(DIRECT_PROFILE_LIMITS),
    },
)

PluginSource = Literal["builtin", "local"]


class PluginError(RuntimeError):
    """Raised when a plugin cannot be discovered, invoked, or validated.

    ``no_capacity`` separates "the cluster had nothing free right now" from
    every other failure. A quota refusal, an unreachable scheduler, or a broken
    plugin must not read to an agent as an empty cluster.
    """

    def __init__(self, message: str, *, no_capacity: bool = False) -> None:
        super().__init__(message)
        self.no_capacity = no_capacity


@dataclass(frozen=True, slots=True)
class PluginDiscoveryFailure:
    path: Path
    source: PluginSource
    error: str
    plugin_id: str | None = None


@dataclass(frozen=True, slots=True)
class PluginInfo:
    plugin_id: str
    display_name: str
    schema_version: int
    capabilities: tuple[str, ...]
    path: Path
    source: PluginSource
    description: str = ""
    limits: dict[str, Any] = field(default_factory=lambda: dict(DIRECT_PROFILE_LIMITS))


def bundled_plugin_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "serverpilot" / "bundled_plugins"
    return Path(__file__).resolve().parent / "bundled_plugins"


def user_plugin_dir(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    if sys.platform == "win32":
        local = env.get("LOCALAPPDATA")
        if local:
            return Path(local) / "ServerPilot" / "plugins"
        return (home or Path.home()) / "AppData" / "Local" / "ServerPilot" / "plugins"
    return (home or Path.home()) / "Library/Application Support/ServerPilot/plugins"


def checkout_plugin_dir() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "plugins"
    return candidate if candidate.is_dir() else None


def plugin_search_dirs(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[tuple[Path, PluginSource]]:
    dirs: list[tuple[Path, PluginSource]] = [(bundled_plugin_dir(), "builtin")]
    checkout = checkout_plugin_dir()
    if checkout is not None and checkout.resolve() != bundled_plugin_dir().resolve():
        dirs.append((checkout, "builtin"))
    dirs.append((user_plugin_dir(home=home, environment=environment), "local"))
    return dirs


def is_valid_plugin_id(value: str) -> bool:
    return bool(PLUGIN_ID_PATTERN.fullmatch(value))


def is_plugin_profile(value: str) -> bool:
    return get_plugin(value) is not None


def is_known_observation_profile(value: str) -> bool:
    return value in BUILTIN_OBSERVATION_PROFILES or is_plugin_profile(value)


def list_observation_profiles(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    profiles = [dict(item) for item in BUILTIN_PROFILE_CATALOG]
    seen = {item["id"] for item in profiles}
    for plugin in discover_plugins(home=home, environment=environment):
        if plugin.plugin_id in seen:
            continue
        seen.add(plugin.plugin_id)
        profiles.append(
            {
                "id": plugin.plugin_id,
                "display_name": plugin.display_name,
                "description": plugin.description
                or f"本地插件 {plugin.plugin_id}，来源 {plugin.source}。",
                "source": plugin.source,
                "capabilities": list(plugin.capabilities),
                "limits": dict(plugin.limits),
            }
        )
    return profiles


def discover_plugins(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[PluginInfo]:
    plugins, _failures = discover_plugins_with_failures(home=home, environment=environment)
    return plugins


# One remembered verdict per plugin file, keyed on what a stat already tells us
# changed. ``info`` is a subprocess, and discovery runs on the collector loop
# and on every claim, so re-forking an interpreter per candidate per call is
# what stalled the control plane. The directory listing stays live so a plugin
# dropped in while the daemon runs is still found; only the fork is remembered.
_PROBE_CACHE: dict[str, tuple[tuple[int, int, PluginSource], PluginInfo | str]] = {}


def _probe_plugin_cached(path: Path, *, source: PluginSource) -> PluginInfo | str:
    """Return the plugin's ``info``, or the failure text, without re-forking."""

    resolved = path.resolve()
    try:
        stat = resolved.stat()
    except OSError as exc:
        return f"plugin could not be read: {type(exc).__name__}"
    fingerprint = (stat.st_mtime_ns, stat.st_size, source)
    cached = _PROBE_CACHE.get(str(resolved))
    if cached is not None and cached[0] == fingerprint:
        return cached[1]
    try:
        verdict: PluginInfo | str = probe_plugin(path, source=source)
    except PluginError as exc:
        verdict = str(exc)
    _PROBE_CACHE[str(resolved)] = (fingerprint, verdict)
    return verdict


def discover_plugins_with_failures(
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[list[PluginInfo], list[PluginDiscoveryFailure]]:
    found: dict[str, PluginInfo] = {}
    failures: list[PluginDiscoveryFailure] = []
    for directory, source in plugin_search_dirs(home=home, environment=environment):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not _is_plugin_candidate(path):
                continue
            verdict = _probe_plugin_cached(path, source=source)
            if isinstance(verdict, str):
                failures.append(
                    PluginDiscoveryFailure(
                        path=path.resolve(),
                        source=source,
                        error=verdict,
                        plugin_id=path.name if is_valid_plugin_id(path.name) else None,
                    )
                )
                continue
            found[verdict.plugin_id] = verdict
    return sorted(found.values(), key=lambda item: item.plugin_id), failures


def get_plugin(
    plugin_id: str,
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> PluginInfo | None:
    if not is_valid_plugin_id(plugin_id):
        return None
    for plugin in discover_plugins(home=home, environment=environment):
        if plugin.plugin_id == plugin_id:
            return plugin
    return None


def require_plugin(plugin_id: str) -> PluginInfo:
    plugin = get_plugin(plugin_id)
    if plugin is None:
        raise PluginError(f"unknown observation profile: {plugin_id}")
    return plugin


def parse_plugin_limits(value: Any) -> dict[str, Any]:
    """Validate schema v3 ``info.limits``. Unknown keys and inconsistent pairs fail."""

    if not isinstance(value, dict):
        raise PluginError("plugin info limits must be an object")
    extra = sorted(set(value) - PROFILE_LIMIT_KEYS)
    if extra:
        raise PluginError(f"plugin info limits has unknown keys: {extra}")
    missing = sorted(PROFILE_LIMIT_KEYS - set(value))
    if missing:
        raise PluginError(f"plugin info limits is missing keys: {missing}")
    lease_ends = value["lease_ends"]
    if lease_ends not in PROFILE_LEASE_ENDS:
        raise PluginError("plugin info limits.lease_ends is invalid")
    max_lease_seconds = value["max_lease_seconds"]
    apply_max_seconds = value["apply_max_seconds"]
    queues = value["queues"]
    if queues is not False:
        raise PluginError("plugin info limits.queues must be false")
    if lease_ends == "hard_kill_at_time_limit":
        if (
            type(max_lease_seconds) is not int
            or isinstance(max_lease_seconds, bool)
            or max_lease_seconds < 1
            or max_lease_seconds > MAX_PROFILE_LEASE_SECONDS
        ):
            raise PluginError("plugin info limits.max_lease_seconds must be a positive integer")
    elif max_lease_seconds is not None:
        raise PluginError("plugin info limits.max_lease_seconds must be null")
    if apply_max_seconds is not None and (
        type(apply_max_seconds) is not int
        or isinstance(apply_max_seconds, bool)
        or apply_max_seconds < 1
        or apply_max_seconds > MAX_PROFILE_APPLY_SECONDS
    ):
        raise PluginError("plugin info limits.apply_max_seconds must be a positive integer")
    return {
        "lease_ends": lease_ends,
        "max_lease_seconds": max_lease_seconds,
        "apply_max_seconds": apply_max_seconds,
        "queues": False,
    }


def probe_plugin(path: Path, *, source: PluginSource) -> PluginInfo:
    raw = invoke_plugin(path, ["info"], timeout_seconds=PLUGIN_INFO_TIMEOUT_SECONDS)
    payload = _strict_object(raw, label="plugin info")
    plugin_id = payload.get("plugin_id")
    display_name = payload.get("display_name")
    schema_version = payload.get("schema_version")
    capabilities = payload.get("capabilities")
    description = payload.get("description", "")
    if not isinstance(plugin_id, str) or not is_valid_plugin_id(plugin_id):
        raise PluginError("plugin info plugin_id is invalid")
    if path.name != plugin_id:
        raise PluginError("plugin filename must match plugin_id")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120:
        raise PluginError("plugin info display_name is invalid")
    if schema_version != PLUGIN_SCHEMA_VERSION:
        raise PluginError(f"plugin info schema_version must be {PLUGIN_SCHEMA_VERSION}")
    if not isinstance(capabilities, list) or not capabilities:
        raise PluginError("plugin info capabilities must be a non-empty list")
    if any(item not in PLUGIN_CAPABILITIES or not isinstance(item, str) for item in capabilities):
        raise PluginError("plugin info capabilities are invalid")
    if len(set(capabilities)) != len(capabilities):
        raise PluginError("plugin info capabilities must be unique")
    if description is None:
        description = ""
    if not isinstance(description, str) or len(description) > 500:
        raise PluginError("plugin info description is invalid")
    limits = parse_plugin_limits(payload.get("limits"))
    return PluginInfo(
        plugin_id=plugin_id,
        display_name=display_name.strip(),
        schema_version=schema_version,
        capabilities=tuple(capabilities),
        path=path.resolve(),
        source=source,
        description=description.strip(),
        limits=limits,
    )


def invoke_plugin(
    path: Path,
    argv: list[str],
    *,
    timeout_seconds: float,
) -> str:
    resolved = path.resolve()
    if any(character in str(resolved) for character in ("\x00", "\n", "\r")):
        raise PluginError("plugin path is invalid")
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise PluginError("plugin is not an executable file")
    try:
        completed = subprocess.run(
            [str(resolved), *argv],
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginError("plugin timed out") from exc
    except OSError as exc:
        raise PluginError(f"plugin could not start: {type(exc).__name__}") from exc
    if len(completed.stdout) > MAX_PLUGIN_OUTPUT_BYTES:
        raise PluginError("plugin stdout exceeded the output limit")
    try:
        stdout = completed.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginError("plugin stdout is not valid UTF-8") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip().replace("\n", " ")
        raise PluginError(
            detail[:1500] or f"plugin exited {completed.returncode}",
            no_capacity=completed.returncode == PLUGIN_NO_CAPACITY_EXIT_CODE,
        )
    return stdout


def observe_plugin(plugin_id: str) -> str:
    plugin = require_plugin(plugin_id)
    if "observe" not in plugin.capabilities:
        raise PluginError(f"plugin {plugin_id} does not declare observe")
    return invoke_plugin(
        plugin.path,
        ["observe"],
        timeout_seconds=PLUGIN_OBSERVE_TIMEOUT_SECONDS,
    )


def apply_plugin(plugin_id: str, *, gpu_count: int, task_ref: str) -> dict[str, Any]:
    plugin = require_plugin(plugin_id)
    if "apply" not in plugin.capabilities:
        raise PluginError(f"plugin {plugin_id} does not declare apply")
    if type(gpu_count) is not int or isinstance(gpu_count, bool) or gpu_count < 1:
        raise PluginError("plugin apply gpu_count must be a positive integer")
    if not isinstance(task_ref, str) or not TASK_REF_PATTERN.fullmatch(task_ref):
        raise PluginError("plugin apply task_ref is invalid")
    # The plugin declares how long its own apply may take. Enforcing the
    # declaration instead of a generic ceiling is what lets the caller's budget
    # be derived from it: a plugin that overruns what it published is a failure,
    # not something to keep waiting on.
    declared = plugin.limits.get("apply_max_seconds")
    raw = invoke_plugin(
        plugin.path,
        ["apply", "--gpu-count", str(gpu_count), "--task-ref", task_ref],
        timeout_seconds=(
            float(declared) if isinstance(declared, int) else PLUGIN_MUTATION_TIMEOUT_SECONDS
        ),
    )
    return parse_apply_payload(raw, plugin_id=plugin_id)


def release_plugin(plugin_id: str, *, allocation_ref: str) -> dict[str, Any]:
    plugin = require_plugin(plugin_id)
    if "release" not in plugin.capabilities:
        raise PluginError(f"plugin {plugin_id} does not declare release")
    if not isinstance(allocation_ref, str) or not ALLOCATION_REF_PATTERN.fullmatch(allocation_ref):
        raise PluginError("plugin release allocation_ref is invalid")
    raw = invoke_plugin(
        plugin.path,
        ["release", "--allocation-ref", allocation_ref],
        timeout_seconds=PLUGIN_MUTATION_TIMEOUT_SECONDS,
    )
    payload = _strict_object(raw, label="plugin release")
    if payload.get("state") != "released":
        raise PluginError("plugin release state must be released")
    return {"state": "released"}


def parse_apply_payload(raw: str, *, plugin_id: str) -> dict[str, Any]:
    payload = _strict_object(raw, label="plugin apply")
    allocation_ref = payload.get("allocation_ref")
    if not isinstance(allocation_ref, str) or not ALLOCATION_REF_PATTERN.fullmatch(allocation_ref):
        raise PluginError("plugin apply allocation_ref is invalid")
    ssh = payload.get("ssh")
    if not isinstance(ssh, dict):
        raise PluginError("plugin apply ssh must be an object")
    host = ssh.get("host")
    port = ssh.get("port")
    user = ssh.get("user")
    if not isinstance(host, str) or not host.strip() or len(host) > 253:
        raise PluginError("plugin apply ssh.host is invalid")
    if type(port) is not int or isinstance(port, bool) or not 1 <= port <= 65535:
        raise PluginError("plugin apply ssh.port is invalid")
    if not isinstance(user, str) or not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$", user):
        raise PluginError("plugin apply ssh.user is invalid")
    workspace_path = payload.get("workspace_path")
    if (
        not isinstance(workspace_path, str)
        or not workspace_path.startswith("/")
        or any(character in workspace_path for character in ("\x00", "\n", "\r"))
        or len(workspace_path) > 2000
    ):
        raise PluginError("plugin apply workspace_path is invalid")
    cuda_visible_devices = payload.get("cuda_visible_devices")
    if not isinstance(cuda_visible_devices, str) or not re.fullmatch(
        r"^[0-9]+(?:,[0-9]+)*$", cuda_visible_devices
    ):
        raise PluginError("plugin apply cuda_visible_devices is invalid")
    gpus = payload.get("gpus", [])
    if not isinstance(gpus, list):
        raise PluginError("plugin apply gpus must be a list")
    parsed_gpus: list[dict[str, Any]] = []
    seen_uuids: set[str] = set()
    for item in gpus:
        if not isinstance(item, dict):
            raise PluginError("plugin apply gpus items must be objects")
        uuid = item.get("gpu_uuid")
        if not isinstance(uuid, str) or not uuid or len(uuid) > 160:
            raise PluginError("plugin apply gpus gpu_uuid is invalid")
        if uuid in seen_uuids:
            raise PluginError("plugin apply gpus contain duplicate gpu_uuid")
        seen_uuids.add(uuid)
        parsed: dict[str, Any] = {"gpu_uuid": uuid}
        name = item.get("name")
        if isinstance(name, str) and name and len(name) <= 255:
            parsed["name"] = name
        total = item.get("total_vram_mib")
        if type(total) is int and not isinstance(total, bool) and total >= 1:
            parsed["total_vram_mib"] = total
        parsed_gpus.append(parsed)
    return {
        "plugin_id": plugin_id,
        "allocation_ref": allocation_ref,
        "ssh": {"host": host.strip(), "port": port, "user": user},
        "workspace_path": workspace_path,
        "cuda_visible_devices": cuda_visible_devices,
        "gpus": parsed_gpus,
    }


def add_plugin(
    source: Path,
    *,
    home: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> PluginInfo:
    if not source.is_file():
        raise PluginError("plugin add source must be a file")
    info = probe_plugin(source, source="local")
    destination_dir = user_plugin_dir(home=home, environment=environment)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / info.plugin_id
    shutil.copy2(source, destination)
    destination.chmod(destination.stat().st_mode | 0o111)
    return probe_plugin(destination, source="local")


def _is_plugin_candidate(path: Path) -> bool:
    if not path.is_file() or path.name.startswith("."):
        return False
    if not is_valid_plugin_id(path.name):
        return False
    return os.access(path, os.X_OK)


def _strict_object(raw: str, *, label: str) -> dict[str, Any]:
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PluginError(f"{label} is not valid Unicode") from exc
    if len(encoded) > MAX_PLUGIN_OUTPUT_BYTES:
        raise PluginError(f"{label} exceeded the output limit")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PluginError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise PluginError(f"{label} must be a JSON object")
    return decoded
