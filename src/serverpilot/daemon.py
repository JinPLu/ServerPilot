"""macOS user-daemon lifecycle for the loopback ServerPilot control plane."""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from . import API_CAPABILITIES, __version__
from .client import control_plane_http_request
from .config import (
    ConfigurationError,
    autostart_enabled,
    control_plane_url,
    load_inventory,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - the first implementation is macOS-only
    fcntl = None  # type: ignore[assignment]


DAEMON_LABEL = "local.serverpilot.daemon"
LEGACY_DAEMON_LABEL = "local.gpu-broker.daemon"
# A daemon can keep the same semantic version while still running an older
# in-memory module after ``uv tool install --force``. Capabilities (rather
# than a timestamp) are the stable floor: they cannot be spoofed by an
# unchanged semver. The running ``/health/live`` version is the second check,
# so ``ensure`` also restarts an owned LaunchAgent that still advertises every
# current capability but has not been replaced into this release.
EXPECTED_CAPABILITIES = frozenset(API_CAPABILITIES)
DAEMON_PROTOCOL = "macos-launchagent-v1"


class DaemonError(RuntimeError):
    """Raised when the local daemon cannot be installed or made ready."""


@dataclass(frozen=True, slots=True)
class DaemonConfig:
    base_url: str
    host: str
    port: int
    data_dir: Path
    database_path: Path
    inventory_path: Path
    plist_path: Path
    log_dir: Path
    lock_path: Path
    executable: Path
    label: str = DAEMON_LABEL


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home()).expanduser().resolve()


def _loopback_url(value: str) -> tuple[str, int, str]:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise DaemonError("macOS daemon auto-start requires a loopback http SERVERPILOT_URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DaemonError("SERVERPILOT_URL must not include credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise DaemonError("SERVERPILOT_URL must not include an API path")
    port = parsed.port or 80
    host = parsed.hostname
    assert host is not None
    return host, port, f"http://{host}:{port}"


def _daemon_executable(environment: Mapping[str, str], home: Path) -> Path:
    """The one install the daemon may run from.

    This used to try four candidates, including whatever was first on PATH and
    a sibling of the current interpreter. Two builds on one machine therefore
    took turns writing the same launch agent, under the same label, at the same
    port, into the same log file, and each restarted the other -- which is what
    filled the log with address-already-in-use and unresolvable-revision errors
    that belonged to neither build alone.
    """

    executable = home / ".local/share/uv/tools/serverpilot/bin/serverpilot"
    if executable.is_file() and os.access(executable, os.X_OK):
        return executable.resolve()
    raise DaemonError(
        f"no serverpilot executable at {executable}; install it with "
        "`uv tool install --force /path/to/serverpilot`"
    )


def resolve_daemon_config(
    environment: Mapping[str, str] | None = None,
) -> DaemonConfig:
    environment = os.environ if environment is None else environment
    home = _home(environment)
    host, port, base_url = _loopback_url(control_plane_url(environment=environment))
    data_dir = (home / "Library/Application Support/ServerPilot").resolve()
    database_path = data_dir / "state/serverpilot.sqlite3"
    inventory_path = data_dir / "inventory.yaml"
    return DaemonConfig(
        base_url=base_url,
        host=host,
        port=port,
        data_dir=data_dir,
        database_path=database_path,
        inventory_path=inventory_path,
        plist_path=home / "Library/LaunchAgents" / f"{DAEMON_LABEL}.plist",
        log_dir=home / "Library/Logs/ServerPilot",
        lock_path=data_dir / "daemon.ensure.lock",
        executable=_daemon_executable(environment, home),
    )


def daemon_instance_id_for_paths(
    database_path: Path,
    inventory_path: Path,
    *,
    label: str = DAEMON_LABEL,
) -> str:
    return "|".join(
        (
            DAEMON_PROTOCOL,
            label,
            str(database_path.expanduser().resolve()),
            str(inventory_path.expanduser().resolve()),
        )
    )


def daemon_instance_id(config: DaemonConfig) -> str:
    return daemon_instance_id_for_paths(
        config.database_path,
        config.inventory_path,
        label=config.label,
    )


def _probe_json(url: str, path: str, timeout_seconds: float = 0.8) -> dict[str, Any] | None:
    try:
        response = control_plane_http_request(
            "GET",
            f"{url}{path}",
            timeout=timeout_seconds,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        raise DaemonError(
            f"{url} responded to {path} with incompatible HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise DaemonError(f"{url} is occupied by a non-JSON service") from exc
    if not isinstance(payload, dict):
        raise DaemonError(f"{url} returned an invalid health response")
    return payload


def probe_live(config: DaemonConfig) -> dict[str, Any] | None:
    payload = _probe_json(config.base_url, "/health/live")
    if payload is None:
        return None
    capabilities = payload.get("capabilities")
    if (
        payload.get("status") != "live"
        or payload.get("schema_version") != "v1"
        or not isinstance(capabilities, list)
        or not EXPECTED_CAPABILITIES.issubset(capabilities)
    ):
        raise DaemonError(
            f"{config.base_url} is occupied by an incompatible ServerPilot service"
        )
    if payload.get("version") != __version__:
        raise DaemonError(
            f"{config.base_url} is running ServerPilot {payload.get('version')}, "
            f"expected {__version__}"
        )
    return payload


def probe_ready(config: DaemonConfig) -> dict[str, Any] | None:
    payload = _probe_json(config.base_url, "/health/ready")
    if payload is None:
        return None
    if (
        payload.get("status") != "ready"
        or payload.get("database_ready") is not True
        or payload.get("inventory_readable") is not True
        or payload.get("single_writer") is not True
        or payload.get("daemon_instance_id") != daemon_instance_id(config)
    ):
        raise DaemonError(
            f"{config.base_url} is not the installed ServerPilot macOS daemon"
        )
    return payload


def render_launch_agent(config: DaemonConfig) -> bytes:
    stdout_path = config.log_dir / "daemon.stdout.log"
    stderr_path = config.log_dir / "daemon.stderr.log"
    payload = {
        "Label": config.label,
        "ProgramArguments": [
            str(config.executable),
            "serve",
            "--db",
            str(config.database_path),
            "--inventory",
            str(config.inventory_path),
            "--host",
            config.host,
            "--port",
            str(config.port),
            "--daemon-instance-id",
            daemon_instance_id(config),
        ],
        "WorkingDirectory": str(config.data_dir),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(stdout_path),
        "StandardErrorPath": str(stderr_path),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def _publish_inventory(source: Path, destination: Path) -> bool:
    if destination.exists():
        if not destination.is_file():
            raise DaemonError(f"inventory target is not a regular file: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".inventory.",
            suffix=".yaml",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
        shutil.copy2(source, temporary)
        try:
            load_inventory(temporary)
        except ConfigurationError as exc:
            raise DaemonError(f"source ServerPilot inventory is invalid: {exc}") from exc
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _copy_sqlite(source: Path, destination: Path) -> bool:
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if destination.exists():
        if not destination.is_file():
            raise DaemonError(f"database target is not a regular file: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as source_db:
        integrity = source_db.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise DaemonError("source ServerPilot database failed integrity_check")
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=".serverpilot.",
                suffix=".sqlite3",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
            with sqlite3.connect(temporary) as destination_db:
                source_db.backup(destination_db)
            with sqlite3.connect(f"file:{temporary}?mode=ro", uri=True) as copied_db:
                copied_integrity = copied_db.execute("PRAGMA integrity_check").fetchone()
            if copied_integrity is None or copied_integrity[0] != "ok":
                raise DaemonError("migrated ServerPilot database failed integrity_check")
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return False
            return True
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class MacOSDaemonManager:
    def __init__(self, config: DaemonConfig | None = None) -> None:
        if sys.platform != "darwin":
            raise DaemonError("headless daemon management is currently implemented for macOS only")
        self.config = config or resolve_daemon_config()

    @property
    def launch_domain(self) -> str:
        return f"gui/{os.getuid()}"

    @property
    def service_target(self) -> str:
        return f"{self.launch_domain}/{self.config.label}"

    @property
    def legacy_service_target(self) -> str:
        return f"{self.launch_domain}/{LEGACY_DAEMON_LABEL}"

    def _launchctl(
        self,
        *arguments: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/launchctl", *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def _loaded(self) -> bool:
        result = self._launchctl("print", self.service_target, check=False)
        return result.returncode == 0

    def _legacy_loaded(self) -> bool:
        try:
            result = self._launchctl("print", self.legacy_service_target, check=False)
        except OSError:
            return False
        return result.returncode == 0

    def _launchd_pid(self) -> int | None:
        result = self._launchctl("print", self.service_target, check=False)
        if result.returncode != 0:
            return None
        match = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout)
        return int(match.group(1)) if match is not None else None

    @staticmethod
    def _parent_process_id(process_id: int) -> int | None:
        """Return one process's parent without invoking a shell.

        PyInstaller's onefile bootloader remains the launchd-owned process and
        starts the Python service as its direct child. A direct parent match is
        therefore an owned service; arbitrary descendants are intentionally not
        accepted.
        """

        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(process_id)],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.8,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        try:
            parent_id = int(result.stdout.strip())
        except ValueError:
            return None
        return parent_id if parent_id > 0 else None

    @staticmethod
    def _process_command(process_id: int) -> str | None:
        try:
            result = subprocess.run(
                ["/bin/ps", "-o", "command=", "-p", str(process_id)],
                check=False,
                capture_output=True,
                text=True,
                timeout=0.8,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def _foreign_holder_message(self, process_id: object) -> str:
        detail = f"{self.config.base_url} is not served by {self.service_target}"
        if isinstance(process_id, int) and not isinstance(process_id, bool) and process_id > 0:
            detail += f"; pid {process_id} holds it"
            command = self._process_command(process_id)
            if command is not None:
                detail += f" ({command})"
        return (
            f"{detail}. Run `serverpilot daemon reclaim` to stop that process and hand "
            f"the port back to the LaunchAgent."
        )

    def _ready_process_is_owned_by_launchd(
        self, process_id: object, launchd_pid: int | None
    ) -> bool:
        if (
            launchd_pid is None
            or isinstance(process_id, bool)
            or not isinstance(process_id, int)
            or process_id < 1
        ):
            return False
        return process_id == launchd_pid or self._parent_process_id(process_id) == launchd_pid

    def _probe_owned_ready(self) -> dict[str, Any] | None:
        live = probe_live(self.config)
        if live is None:
            return None
        ready = probe_ready(self.config)
        if ready is None:
            return None
        launchd_pid = self._launchd_pid()
        if not self._ready_process_is_owned_by_launchd(ready.get("process_id"), launchd_pid):
            raise DaemonError(self._foreign_holder_message(ready.get("process_id")))
        return ready

    def _write_plist(self) -> bool:
        expected = render_launch_agent(self.config)
        if self.config.plist_path.is_file() and self.config.plist_path.read_bytes() == expected:
            return False
        self.config.plist_path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.config.plist_path.parent,
                prefix=f".{self.config.label}.",
                suffix=".plist",
                delete=False,
            ) as handle:
                handle.write(expected)
                temporary = Path(handle.name)
            os.replace(temporary, self.config.plist_path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True

    @contextlib.contextmanager
    def _install_lock(self) -> Any:
        if fcntl is None:  # pragma: no cover - guarded by the macOS constructor
            raise DaemonError("file locking is unavailable on this platform")
        self.config.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield

    def _bootout_if_loaded(self) -> bool:
        if not self._loaded():
            return False
        result = self._launchctl("bootout", self.service_target, check=False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._loaded():
                return True
            time.sleep(0.05)
        details = (result.stderr or result.stdout).strip()
        raise DaemonError(
            f"launchctl bootout did not unload {self.service_target}"
            + (f": {details}" if details else "")
        )

    def _remove_legacy_plist(self) -> bool:
        """Delete the retired agent's plist, not just unload it.

        Unloading a plist that stays on disk with RunAtLoad only postpones it to
        the next login, where it binds the same port this daemon wants and the
        loser logs an address conflict nobody can trace back to it.
        """

        legacy_plist = self.config.plist_path.with_name(f"{LEGACY_DAEMON_LABEL}.plist")
        if not legacy_plist.exists():
            return False
        legacy_plist.unlink()
        return True

    def _bootout_legacy_if_loaded(self) -> bool:
        if not self._legacy_loaded():
            return self._remove_legacy_plist()
        result = self._launchctl("bootout", self.legacy_service_target, check=False)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if not self._legacy_loaded():
                self._remove_legacy_plist()
                return True
            time.sleep(0.05)
        details = (result.stderr or result.stdout).strip()
        raise DaemonError(
            f"launchctl bootout did not unload {self.legacy_service_target}"
            + (f": {details}" if details else "")
        )

    def _prepare_data(self, source_root: Path | None) -> dict[str, bool]:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        legacy_data_dir = self.config.data_dir.parent / "GPU Broker"
        migrated_inventory = False
        migrated_database = False
        if not self.config.inventory_path.is_file():
            legacy_inventory = legacy_data_dir / "inventory.yaml"
            source_inventory = (
                legacy_inventory
                if legacy_inventory.is_file()
                else (
                    source_root.expanduser().resolve() / "configs/inventory.yaml"
                    if source_root is not None
                    else None
                )
            )
            if source_inventory is None:
                raise DaemonError(
                    f"missing {self.config.inventory_path}; run `serverpilot daemon install "
                    "--source-root /path/to/serverpilot` once"
                )
            if not source_inventory.is_file():
                raise DaemonError(f"source inventory does not exist: {source_inventory}")
            migrated_inventory = _publish_inventory(
                source_inventory,
                self.config.inventory_path,
            )
        if not self.config.database_path.exists():
            legacy_database = legacy_data_dir / "state/gpu-broker.sqlite3"
            source_database = legacy_database
            if not source_database.is_file() and source_root is not None:
                source_database = (
                    source_root.expanduser().resolve() / "state/serverpilot.sqlite3"
                )
            if source_database.is_file():
                migrated_database = _copy_sqlite(
                    source_database,
                    self.config.database_path,
                )
        return {
            "migrated_inventory": migrated_inventory,
            "migrated_database": migrated_database,
        }

    def _install_locked(
        self,
        source_root: Path | None = None,
        *,
        start: bool = True,
    ) -> dict[str, Any]:
        self._bootout_legacy_if_loaded()
        migration = self._prepare_data(source_root)
        changed = self._write_plist()
        if self._loaded() and changed:
            self._bootout_if_loaded()
        if start:
            self.start()
        return {
            "installed": True,
            "plist_changed": changed,
            "data_dir": str(self.config.data_dir),
            "database_path": str(self.config.database_path),
            "inventory_path": str(self.config.inventory_path),
            **migration,
        }

    def install(self, source_root: Path | None = None, *, start: bool = True) -> dict[str, Any]:
        with self._install_lock():
            return self._install_locked(source_root, start=start)

    def start(self, *, timeout_seconds: float = 15.0) -> None:
        if not self.config.plist_path.is_file():
            raise DaemonError("daemon is not installed")
        if not self._loaded():
            result = self._launchctl(
                "bootstrap",
                self.launch_domain,
                str(self.config.plist_path),
                check=False,
            )
            if result.returncode != 0 and not self._loaded():
                raise DaemonError(
                    f"launchctl bootstrap failed: {(result.stderr or result.stdout).strip()}"
                )
            # RunAtLoad has already spawned the job. Kicking it here would kill
            # a process that is still starting, and launchd then withholds the
            # respawn for its throttle interval: ten seconds in which the app
            # has nothing to talk to.
        else:
            try:
                if self._probe_owned_ready() is not None:
                    return
            except DaemonError:
                pass
            # Loaded but not answering is the case a forced restart is for.
            self._launchctl("kickstart", "-k", self.service_target)
        deadline = time.monotonic() + timeout_seconds
        last_error: DaemonError | None = None
        while time.monotonic() < deadline:
            try:
                if self._probe_owned_ready() is not None:
                    return
            except DaemonError as exc:
                last_error = exc
            time.sleep(0.15)
        if last_error is not None:
            raise last_error
        raise DaemonError(
            f"ServerPilot daemon did not become ready at {self.config.base_url}"
        )

    def stop(self) -> None:
        self._bootout_if_loaded()

    def _terminate_holder(self, process_id: int, *, timeout_seconds: float = 5.0) -> None:
        try:
            os.kill(process_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            raise DaemonError(f"could not stop pid {process_id}: {exc}") from exc
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.kill(process_id, 0)
            except OSError:
                return
            time.sleep(0.1)
        raise DaemonError(
            f"pid {process_id} is still holding {self.config.base_url} after SIGTERM; "
            f"stop it manually and run this command again"
        )

    def reclaim(self, *, timeout_seconds: float = 15.0) -> dict[str, Any]:
        """Hand the daemon port back to the owned LaunchAgent.

        This only resolves a ServerPilot service that answers ``/health/ready``
        without being owned by launchd. A port held by an unrelated program
        never reaches the ownership check and is not stopped here.
        """

        ready = probe_ready(self.config)
        holder = ready.get("process_id") if ready is not None else None
        if ready is not None and self._ready_process_is_owned_by_launchd(
            holder, self._launchd_pid()
        ):
            return {"reclaimed": False, "reason": "already_owned", **self.status()}
        stopped: dict[str, Any] | None = None
        if isinstance(holder, int) and not isinstance(holder, bool) and holder > 0:
            stopped = {"pid": holder, "command": self._process_command(holder)}
            self._terminate_holder(holder)
        self.start(timeout_seconds=timeout_seconds)
        return {"reclaimed": True, "stopped": stopped, **self.status()}

    def uninstall(self) -> dict[str, Any]:
        with self._install_lock():
            self.stop()
            removed = self.config.plist_path.exists()
            self.config.plist_path.unlink(missing_ok=True)
        return {
            "uninstalled": True,
            "plist_removed": removed,
            "data_preserved": str(self.config.data_dir),
        }

    def status(self) -> dict[str, Any]:
        live = probe_live(self.config)
        ready = self._probe_owned_ready() if live is not None else None
        return {
            "installed": self.config.plist_path.is_file(),
            "loaded": self._loaded(),
            "live": live is not None,
            "ready": ready is not None,
            # Which release is installed and which one is answering are
            # different facts, and the status that could not tell them apart is
            # exactly the status a restart-after-upgrade needs to be checked by.
            "installed_version": __version__,
            "running_version": live.get("version") if live else None,
            "base_url": self.config.base_url,
            "data_dir": str(self.config.data_dir),
            "database_path": str(self.config.database_path),
            "inventory_path": str(self.config.inventory_path),
        }

    def ensure(
        self,
        source_root: Path | None = None,
        *,
        timeout_seconds: float = 15.0,
    ) -> dict[str, Any]:
        self._bootout_legacy_if_loaded()
        try:
            # Stale means the owned process is missing a declared capability or
            # is not this release's ``version``. ``probe_live`` raises in both
            # cases; if we still own the LaunchAgent, fall through and replace it.
            if self._probe_owned_ready() is not None:
                return self.status()
        except DaemonError:
            if not (self._loaded() and self.config.plist_path.is_file()):
                raise
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        with self._install_lock():
            try:
                if self._probe_owned_ready() is not None:
                    return self.status()
            except DaemonError:
                if not (self._loaded() and self.config.plist_path.is_file()):
                    raise
            self._install_locked(source_root, start=False)
            self.start(timeout_seconds=timeout_seconds)
            return self.status()


def assert_control_plane_matches_release(*, base_url: str | None = None) -> None:
    """Refuse an MCP session whose loopback service is not this release."""

    url = control_plane_url(base_url).rstrip("/")
    payload = _probe_json(url, "/health/live")
    if payload is None:
        raise DaemonError(
            f"{url} is not reachable. Start the ServerPilot control plane and retry."
        )
    capabilities = payload.get("capabilities")
    cap_list = capabilities if isinstance(capabilities, list) else []
    missing = sorted(EXPECTED_CAPABILITIES.difference(cap_list))
    running = payload.get("version")
    if running != __version__ or missing:
        missing_text = f" and is missing capabilities {missing}" if missing else ""
        raise DaemonError(
            f"this MCP is ServerPilot {__version__}, but the control plane at {url} "
            f"is {running}{missing_text}. Restart the ServerPilot control plane so both match."
        )


def ensure_broker_ready_for_mcp() -> None:
    if sys.platform == "darwin" and autostart_enabled():
        MacOSDaemonManager().ensure()
    assert_control_plane_matches_release()


def format_status(value: dict[str, Any], *, as_json: bool) -> str:
    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2)
    return "\n".join(f"{key}: {item}" for key, item in value.items())
