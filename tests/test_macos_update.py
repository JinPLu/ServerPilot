"""A local update succeeds only after both new processes pass verification."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import update_macos


class FakeUpdateEnvironment:
    """Replace every OS command and health request with an isolated local double."""

    def __init__(self, root: Path, home: Path) -> None:
        self.root = root
        self.home = home
        self.expected = {
            "version": "4.1.2",
            "backend_build_id": "a" * 64,
            "app_build_id": "b" * 64,
        }
        self.events: list[str] = []
        self.commands: list[tuple[list[str], dict]] = []
        self.reports: list[str] = []
        self.old_app_pid = 101
        self.app_pid = 202
        self.interim_app_pid = 505
        self.old_daemon_pid = 303
        self.daemon_pid = 404
        self.gui_pid: int | None = self.old_app_pid
        self.closed_app_pids: list[int] = []
        self.reopen_during_build = False
        self.daemon_restarted = False
        self.refuse_quit = False
        self.stale_live_build = False
        self.fail_command: str | None = None
        self.duplicate_on_final_verification = False
        self.app_mode = "fresh"
        self.bundle_checks = 0
        self.install_source: Path | None = None
        self.install_source_files: dict[str, bytes] = {}

    def instance(self, pid: int) -> dict:
        app = self.root / "ServerPilot.app"
        if self.app_mode == "wrong-path":
            app = self.root / "ServerPilot 2.app"
        return {
            "pid": pid,
            "bundle_path": str(app),
            "executable_path": str(app / "Contents/MacOS/ServerPilot"),
            "bundle_version": self.expected["version"],
            "bundle_build_id": self.expected["app_build_id"],
            "bundle_backend_build_id": self.expected["backend_build_id"],
            "finished_launching": True,
        }

    def run(self, args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        self.commands.append((args, kwargs))
        executable = Path(args[0]).name
        payload: dict | None = None
        if executable == "swift":
            action = args[2]
            event = f"app-{action}"
            if action == "inspect":
                payload = {"instances": [self.instance(self.gui_pid)] if self.gui_pid is not None else []}
            elif action == "quit":
                quit_pids = [self.gui_pid] if self.gui_pid is not None else []
                if not self.refuse_quit:
                    self.closed_app_pids.extend(quit_pids)
                    self.gui_pid = None
                payload = {
                    "quit_pids": quit_pids,
                    "instances": [self.instance(self.gui_pid)] if self.gui_pid is not None else [],
                }
            elif action == "restart":
                event = "app-launch"
                quit_pids = [self.gui_pid] if self.gui_pid is not None else []
                self.closed_app_pids.extend(quit_pids)
                if self.app_mode == "old-pid":
                    pid = self.old_app_pid
                elif self.app_mode == "interim-pid":
                    pid = self.interim_app_pid
                else:
                    pid = self.app_pid
                self.gui_pid = pid
                instances = [self.instance(pid)]
                if self.app_mode == "two-instances":
                    instances.append(self.instance(pid + 1))
                payload = {"quit_pids": quit_pids, "launched_pid": pid, "instances": instances}
            else:
                raise AssertionError(f"Unexpected native action: {action}")
        elif executable == "zsh":
            event = "build" if Path(args[1]).name == "build-macos-app.sh" else "verify-app"
            if event == "build" and self.reopen_during_build:
                self.gui_pid = self.interim_app_pid
        elif executable == "uv":
            event = "install-backend"
            self.install_source = Path(args[-1])
            self.install_source_files = {
                path.relative_to(self.install_source).as_posix(): path.read_bytes()
                for path in self.install_source.rglob("*") if path.is_file()
            }
        elif executable == "serverpilot":
            event = f"daemon-{args[2]}"
            if args[2] == "start":
                self.daemon_restarted = True
            elif args[2] == "status":
                payload = {
                    "installed": True, "loaded": True, "live": True, "ready": True,
                    "installed_version": self.expected["version"],
                    "running_version": self.expected["version"],
                    "base_url": "http://127.0.0.1:8787",
                }
        elif executable == "python":
            event = "verify-installed-backend"
            payload = {
                "version": self.expected["version"],
                "build_id": self.expected["backend_build_id"],
                "prefix": str(self.home / ".local/share/uv/tools/serverpilot"),
            }
        else:
            raise AssertionError(f"Unexpected executable: {args}")
        self.events.append(event)
        if event == self.fail_command:
            raise subprocess.CalledProcessError(1, args, stderr=f"{event} failed")
        return subprocess.CompletedProcess(args, 0, json.dumps(payload) if payload is not None else "")

    def health(self, _base_url: str, route: str, *, optional: bool = False) -> dict:
        self.events.append(f"health-{Path(route).name}")
        build_id = self.expected["backend_build_id"]
        if route == "/health/live":
            return {
                "status": "live", "version": self.expected["version"],
                "build_id": "older-code-same-version" if self.stale_live_build else build_id,
            }
        assert route == "/health/ready"
        return {
            "status": "ready", "build_id": build_id,
            "process_id": self.daemon_pid if self.daemon_restarted else self.old_daemon_pid,
        }

    def verify_bundle(self, _root: Path, _home: Path) -> None:
        self.bundle_checks += 1
        if self.duplicate_on_final_verification and self.bundle_checks > 1:
            raise update_macos.BundleError("A duplicate ServerPilot App reappeared")


@pytest.fixture
def update_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "checkout"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "serverpilot"\nversion = "4.1.2"\n')
    (root / "README.md").write_text("Current source readme\n")
    (root / "LICENSE").write_text("Current source license\n")
    (root / "uv.lock").write_text("version = 1\n")
    package = root / "src/serverpilot"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "4.1.2"\n')
    (package / "service.py").write_text('SOURCE = "current"\n')
    monkeypatch.setenv("SERVERPILOT_URL", "http://127.0.0.1:8787")
    fake = FakeUpdateEnvironment(root, home)
    monkeypatch.setattr(update_macos, "build_identity", lambda _root: dict(fake.expected))
    monkeypatch.setattr(update_macos, "verify_bundle", fake.verify_bundle)
    updater = update_macos.MacOSUpdater(
        root, home, run=fake.run, read_health=fake.health, report=fake.reports.append,
    )
    return updater, fake


def test_update_replaces_both_products_and_verifies_new_processes(update_environment) -> None:
    updater, fake = update_environment

    result = updater.update()

    assert result == {
        **fake.expected, "app_path": str(fake.root / "ServerPilot.app"),
        "app_pid": fake.app_pid, "daemon_pid": fake.daemon_pid,
    }
    install, options = next(
        (args, options) for args, options in fake.commands if Path(args[0]).name == "uv"
    )
    assert install[1:-1] == [
        "tool", "install", "--force", "--reinstall-package", "serverpilot",
        "--python", "3.12",
    ]
    assert fake.install_source is not None
    assert Path(install[-1]) == fake.install_source != fake.root
    assert fake.install_source_files["pyproject.toml"] == (fake.root / "pyproject.toml").read_bytes()
    assert fake.install_source_files["src/serverpilot/service.py"] == b'SOURCE = "current"\n'
    assert not fake.install_source.exists()
    assert options["env"]["UV_TOOL_DIR"] == str(fake.home / ".local/share/uv/tools")
    assert options["env"]["SERVERPILOT_CLI"] == str(updater.cli)
    assert fake.events.index("app-quit") < fake.events.index("build")
    assert fake.events.index("build") < fake.events.index("install-backend")
    assert fake.events.index("daemon-stop") < fake.events.index("daemon-start")
    assert fake.events.index("daemon-start") < fake.events.index("health-live")
    assert fake.events.index("health-live") < fake.events.index("app-launch")
    assert all(
        args[0] == str(updater.cli)
        for args, _ in fake.commands if Path(args[0]).name == "serverpilot"
    )


def test_backend_install_uses_clean_sources_without_old_build_outputs_or_an_app(update_environment) -> None:
    updater, fake = update_environment
    stale = fake.root / "build/lib/serverpilot/stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("Deleted source left behind by setuptools\n")
    metadata = fake.root / "src/serverpilot.egg-info/PKG-INFO"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Old packaging metadata\n")
    cache = fake.root / "src/serverpilot/__pycache__/service.cpython-312.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"stale bytecode")
    app = fake.root / "ServerPilot.app/Contents/MacOS/ServerPilot"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"desktop executable")

    updater.update()

    assert set(fake.install_source_files) == {
        "pyproject.toml", "README.md", "LICENSE", "uv.lock",
        "src/serverpilot/__init__.py", "src/serverpilot/service.py",
    }
    assert fake.install_source_files["src/serverpilot/service.py"] == b'SOURCE = "current"\n'
    assert fake.install_source is not None and not fake.install_source.exists()
    assert stale.is_file() and metadata.is_file() and app.is_file()


@pytest.mark.parametrize("failure", ["same-version-old-code", "unchanged-pid"])
def test_stale_backend_aborts_without_launching_the_app(update_environment, failure: str) -> None:
    updater, fake = update_environment
    if failure == "same-version-old-code":
        fake.stale_live_build = True
        message = "stale or unhealthy"
    else:
        fake.daemon_pid = fake.old_daemon_pid
        message = "new process"

    with pytest.raises(update_macos.UpdateError, match=message):
        updater.update()

    assert "daemon-start" in fake.events
    assert "app-launch" not in fake.events


def test_old_app_refusing_to_quit_aborts_before_replacing_anything(update_environment) -> None:
    updater, fake = update_environment
    fake.refuse_quit = True

    with pytest.raises(update_macos.UpdateError, match="old App is still running"):
        updater.update()

    assert "build" not in fake.events
    assert "install-backend" not in fake.events


@pytest.mark.parametrize("command", ["build", "install-backend"])
def test_build_or_backend_install_failure_aborts_remaining_steps(update_environment, command: str) -> None:
    updater, fake = update_environment
    fake.fail_command = command

    with pytest.raises(update_macos.UpdateError, match=f"{command} failed"):
        updater.update()

    assert "daemon-start" not in fake.events
    assert "app-launch" not in fake.events
    if command == "build":
        assert "install-backend" not in fake.events


def test_a_duplicate_app_found_after_launch_prevents_update_success(update_environment) -> None:
    updater, fake = update_environment
    fake.duplicate_on_final_verification = True

    with pytest.raises(update_macos.BundleError, match="duplicate ServerPilot App"):
        updater.update()

    assert "app-launch" in fake.events
    assert fake.bundle_checks == 2


@pytest.mark.parametrize("app_mode", ["fresh", "interim-pid"])
def test_an_app_reopened_during_build_is_restarted_and_its_pid_cannot_be_reused(
    update_environment, app_mode: str,
) -> None:
    updater, fake = update_environment
    fake.reopen_during_build = True
    fake.app_mode = app_mode

    if app_mode == "interim-pid":
        with pytest.raises(update_macos.UpdateError, match="fresh instance"):
            updater.update()
    else:
        result = updater.update()
        assert result["app_pid"] == fake.app_pid
        assert fake.gui_pid == fake.app_pid

    assert fake.closed_app_pids == [fake.old_app_pid, fake.interim_app_pid]


@pytest.mark.parametrize("app_mode", ["old-pid", "two-instances", "wrong-path"])
def test_a_stale_or_second_app_cannot_pass_final_verification(update_environment, app_mode: str) -> None:
    updater, fake = update_environment
    fake.app_mode = app_mode

    with pytest.raises(update_macos.UpdateError, match="fresh instance|exactly one"):
        updater.update()

    assert "app-launch" in fake.events


def test_an_incorrect_installed_build_is_rejected_before_restarting_the_daemon(update_environment) -> None:
    updater, fake = update_environment

    def run(args, **kwargs):  # type: ignore[no-untyped-def]
        result = fake.run(args, **kwargs)
        if Path(args[0]).name == "python":
            payload = json.loads(result.stdout)
            payload["build_id"] = "stale-installed-code"
            result.stdout = json.dumps(payload)
        return result

    updater.run = run
    with pytest.raises(update_macos.UpdateError, match="canonical installed backend"):
        updater.update()

    assert "daemon-stop" not in fake.events
    assert "daemon-start" not in fake.events
    assert "app-launch" not in fake.events


@pytest.mark.parametrize("url", ["http://127.0.0.1:9999", "http://localhost", "https://example.com"])
def test_a_daemon_url_the_app_does_not_use_is_rejected_before_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str,
) -> None:
    monkeypatch.setenv("SERVERPILOT_URL", url)

    with pytest.raises(update_macos.UpdateError, match="App's daemon"):
        update_macos.MacOSUpdater(tmp_path / "root", tmp_path / "home")


def test_update_lock_rejects_a_parallel_update_and_is_released_after_failure(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="interrupted"), update_macos.update_lock(tmp_path):
        with pytest.raises(update_macos.UpdateError, match="already running"), update_macos.update_lock(tmp_path):
            pytest.fail("a second updater acquired the same lock")
        raise RuntimeError("interrupted")

    with update_macos.update_lock(tmp_path):
        pass
