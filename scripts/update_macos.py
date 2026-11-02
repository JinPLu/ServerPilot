#!/usr/bin/env python3
"""Update the one local macOS App and backend, then verify the running code."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from macos_app_bundle import BundleError, build_identity, verify_bundle  # noqa: E402


class UpdateError(RuntimeError):
    """An update that has not completed all verification steps."""


def local_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.netloc != "127.0.0.1:8787"
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise UpdateError("The desktop updater requires the App's daemon at http://127.0.0.1:8787.")
    return value.rstrip("/")


def health(base_url: str, route: str, *, optional: bool = False) -> dict[str, Any] | None:
    # Ignore both environment and macOS system proxies for loopback health checks.
    try:
        with build_opener(ProxyHandler({})).open(base_url + route, timeout=10) as response:
            payload = json.load(response)
    except URLError as exc:
        if optional:
            return None
        raise UpdateError(f"Cannot verify {route}: {exc}") from exc
    except (ValueError, OSError) as exc:
        raise UpdateError(f"Invalid response from {route}: {exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError(f"Invalid response from {route}.")
    return payload


@contextlib.contextmanager
def update_lock(home: Path) -> Iterator[None]:
    path = home / "Library/Application Support/ServerPilot/desktop-update.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("Another ServerPilot update is already running.") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def backend_source(project_root: Path) -> Iterator[Path]:
    """Build from fresh sources so setuptools cannot reuse a checkout's stale build/lib."""

    with tempfile.TemporaryDirectory(prefix="serverpilot-update-source-") as directory:
        staging = Path(directory)
        for name in ("pyproject.toml", "README.md", "LICENSE", "uv.lock"):
            source = project_root / name
            if name != "uv.lock" or source.is_file():
                shutil.copy2(source, staging / name)
        shutil.copytree(
            project_root / "src/serverpilot", staging / "src/serverpilot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"),
        )
        yield staging


class MacOSUpdater:
    def __init__(
        self,
        project_root: Path,
        home: Path,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        read_health: Callable[..., Any] = health,
        report: Callable[[str], Any] = print,
    ) -> None:
        self.root = project_root.resolve()
        self.home = home.resolve()
        self.tool_root = self.home / ".local/share/uv/tools/serverpilot"
        self.cli = self.tool_root / "bin/serverpilot"
        self.python = self.tool_root / "bin/python"
        self.app = self.root / "ServerPilot.app"
        self.run = run
        self.read_health = read_health
        self.report = report
        self.stage = "preflight"
        self.environment = dict(os.environ)
        self.base_url = local_url(self.environment.get("SERVERPILOT_URL", "http://127.0.0.1:8787"))
        for key in list(self.environment):
            if key.startswith("SERVERPILOT_DESKTOP_") or key in {"PYTHONPATH", "PYTHONHOME"}:
                self.environment.pop(key)
        self.environment.update({
            "UV_TOOL_DIR": str(self.tool_root.parent),
            "UV_TOOL_BIN_DIR": str(self.home / ".local/bin"),
            "SERVERPILOT_CLI": str(self.cli),
            "SERVERPILOT_URL": self.base_url,
        })

    def command(self, *args: str | Path, timeout: int = 120) -> str:
        try:
            result = self.run(
                [str(arg) for arg in args], cwd=self.root, env=self.environment,
                text=True, capture_output=True, check=True, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            details = getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or str(exc)
            raise UpdateError(details.strip()) from exc
        return result.stdout

    def json_command(self, *args: str | Path, timeout: int = 120) -> dict[str, Any]:
        try:
            payload = json.loads(self.command(*args, timeout=timeout))
        except ValueError as exc:
            raise UpdateError(f"{self.stage}: command returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise UpdateError(f"{self.stage}: command returned invalid JSON")
        return payload

    def native(self, action: str, *args: Path) -> dict[str, Any]:
        return self.json_command("swift", self.root / "scripts/macos_app_process.swift", action, *args)

    def progress(self, message: str) -> None:
        self.stage = message
        self.report(message)

    def verify_installation(self, expected: dict[str, str]) -> None:
        installed = self.json_command(
            self.python, "-I", "-c",
            "import json,sys,serverpilot; "
            "from serverpilot.runtime_identity import package_build_id; "
            "print(json.dumps({'version':serverpilot.__version__,"
            "'build_id':package_build_id(),'prefix':sys.prefix}))",
        )
        if (
            installed.get("version") != expected["version"]
            or installed.get("build_id") != expected["backend_build_id"]
            or Path(installed.get("prefix", "")).resolve() != self.tool_root
        ):
            raise UpdateError("The canonical installed backend does not match this source build.")

    def verify_backend(self, expected: dict[str, str], previous_pid: int | None) -> int:
        self.verify_installation(expected)
        status = self.json_command(self.cli, "daemon", "status", "--json")
        if (
            not all(status.get(key) is True for key in ("installed", "loaded", "live", "ready"))
            or status.get("installed_version") != expected["version"]
            or status.get("running_version") != expected["version"]
            or status.get("base_url") != self.base_url
        ):
            raise UpdateError("The owned daemon is not ready on the expected version and address.")
        live = self.read_health(self.base_url, "/health/live")
        ready = self.read_health(self.base_url, "/health/ready")
        if (
            not live or live.get("status") != "live"
            or live.get("version") != expected["version"]
            or live.get("build_id") != expected["backend_build_id"]
            or not ready or ready.get("status") != "ready"
            or ready.get("build_id") != expected["backend_build_id"]
        ):
            raise UpdateError("The running daemon is stale or unhealthy, even if its version matches.")
        pid = ready.get("process_id")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 or pid == previous_pid:
            raise UpdateError("The daemon did not restart into a new process.")
        return pid

    def verify_app(self, result: dict[str, Any], expected: dict[str, str], old_pids: set[int]) -> int:
        instances = result.get("instances", [])
        if len(instances) != 1:
            raise UpdateError("Expected exactly one running ServerPilot App.")
        app = instances[0]
        pid = app.get("pid")
        if (
            isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 or pid in old_pids
            or app.get("bundle_path") != str(self.app)
            or app.get("executable_path") != str(self.app / "Contents/MacOS/ServerPilot")
            or app.get("bundle_version") != expected["version"]
            or app.get("bundle_build_id") != expected["app_build_id"]
            or app.get("bundle_backend_build_id") != expected["backend_build_id"]
            or app.get("finished_launching") is not True
        ):
            raise UpdateError("The running App is not a fresh instance of the canonical build.")
        return pid

    def update(self) -> dict[str, Any]:
        self.progress("Closing the old ServerPilot App")
        before = self.native("inspect")
        old_pids = {entry["pid"] for entry in before.get("instances", [])}
        closed = self.native("quit")
        if closed.get("instances") != []:
            raise UpdateError("The old App is still running; nothing was replaced.")
        old_pids.update(closed.get("quit_pids", []))

        self.progress("Building and replacing the root ServerPilot.app")
        self.command("zsh", self.root / "desktop/build-macos-app.sh", timeout=600)
        expected = build_identity(self.root)
        verify_bundle(self.root, self.home)
        old_ready = self.read_health(self.base_url, "/health/ready", optional=True)
        previous_pid = old_ready.get("process_id") if old_ready else None

        self.progress("Replacing the canonical installed backend")
        with backend_source(self.root) as source:
            self.command(
                "uv", "tool", "install", "--force", "--reinstall-package", "serverpilot",
                "--python", "3.12", source, timeout=600,
            )
        self.verify_installation(expected)
        self.command("zsh", self.root / "desktop/verify-macos-app.sh")

        self.progress("Restarting the owned ServerPilot daemon")
        self.command(self.cli, "daemon", "install", "--source-root", self.root, "--no-start")
        self.command(self.cli, "daemon", "stop")
        self.command(self.cli, "daemon", "start")
        self.progress("Verifying the installed and running backend build")
        daemon_pid = self.verify_backend(expected, previous_pid)

        self.progress("Launching and verifying the one updated App")
        # A user or macOS may reopen the GUI during the build. Close any such
        # instance and launch the final bundle in one native helper invocation.
        launched = self.native("restart", self.app)
        old_pids.update(launched.get("quit_pids", []))
        app_pid = self.verify_app(launched, expected, old_pids)
        inspected_pid = self.verify_app(self.native("inspect"), expected, old_pids)
        if inspected_pid != app_pid:
            raise UpdateError("The App process changed during verification.")
        verify_bundle(self.root, self.home)
        daemon_pid = self.verify_backend(expected, previous_pid)
        if build_identity(self.root) != expected:
            raise UpdateError("Source files changed during the update; rerun the updater.")
        return {**expected, "app_path": str(self.app), "app_pid": app_pid, "daemon_pid": daemon_pid}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This updater only supports macOS.")
    for executable in ("uv", "swift", "zsh"):
        if shutil.which(executable) is None:
            parser.error(f"Required executable is missing: {executable}")
    updater: MacOSUpdater | None = None
    try:
        updater = MacOSUpdater(
            Path(__file__).resolve().parents[1], Path.home(), report=lambda text: print(text, flush=True),
        )
        with update_lock(updater.home):
            result = updater.update()
    except (UpdateError, BundleError, OSError) as exc:
        stage = updater.stage if updater is not None else "preflight"
        print(f"Update failed during {stage}: {exc}\nUpdate has NOT been verified complete.", file=sys.stderr)
        return 1
    print("ServerPilot update verified: " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
