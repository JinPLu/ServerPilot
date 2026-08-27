from __future__ import annotations

import importlib.util
import json
import os
import sys
import types
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "desktop" / "windows_launcher.py"
    spec = importlib.util.spec_from_file_location("serverpilot_windows_launcher", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_windows_runtime_paths_use_local_app_data(tmp_path: Path) -> None:
    launcher = load_launcher()

    paths = launcher.runtime_paths(
        {
            "LOCALAPPDATA": str(tmp_path),
            "SERVERPILOT_INVENTORY": "",
            "SERVERPILOT_DATABASE_URL": "",
            "SERVERPILOT_BIND_PORT": "8899",
        }
    )

    assert paths.data_dir == tmp_path / "ServerPilot"
    assert paths.inventory_path == tmp_path / "ServerPilot" / "inventory.yaml"
    assert paths.database_url.endswith("/ServerPilot/state/serverpilot.sqlite3")
    assert paths.port == 8899
    assert paths.external_inventory is False


def test_windows_runtime_paths_reject_invalid_port() -> None:
    launcher = load_launcher()

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"SERVERPILOT_BIND_PORT": "not-a-port"})

    with pytest.raises(launcher.LauncherError):
        launcher.runtime_paths({"SERVERPILOT_BIND_PORT": "70000"})


def test_windows_launcher_creates_default_inventory_without_overwriting(tmp_path: Path) -> None:
    launcher = load_launcher()
    inventory = tmp_path / "inventory.yaml"

    launcher.ensure_inventory(inventory, create_default=True)

    parsed = yaml.safe_load(inventory.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 1
    assert parsed["collector"]["enabled"] is True
    assert parsed["endpoints"] == []

    inventory.write_text("schema_version: 1\nendpoints: []\n", encoding="utf-8")
    launcher.ensure_inventory(inventory, create_default=True)

    assert inventory.read_text(encoding="utf-8") == "schema_version: 1\nendpoints: []\n"


def test_windows_launcher_requires_external_inventory_to_exist(tmp_path: Path) -> None:
    launcher = load_launcher()

    with pytest.raises(launcher.LauncherError):
        launcher.ensure_inventory(tmp_path / "missing.yaml", create_default=False)


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_windows_desktop_bridge_limits_the_rest_surface_and_sets_identity_headers(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append((request, timeout))
        return FakeResponse({"data": {"current": {"endpoints": []}}})

    paths = launcher.RuntimePaths(
        data_dir=tmp_path,
        inventory_path=tmp_path / "inventory.yaml",
        database_url="sqlite:///example.sqlite3",
        port=8787,
        external_inventory=False,
    )
    bridge = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener)

    result = bridge.claim(
        {
            "project_id": "demo",
            "task_ref": "train",
            "purpose": "test",
            "constraints": {"gpu_count": 1},
            "unexpected_url": "https://example.invalid",
        }
    )

    assert result["ok"] is True
    request, timeout = calls[0]
    assert request.full_url == "http://127.0.0.1:8787/api/v1/claims"
    assert timeout == 8
    assert request.get_header("X-serverpilot-actor") == "human"
    assert request.get_header("Idempotency-key").startswith("windows-")
    assert json.loads(request.data.decode("utf-8")) == {
        "project_id": "demo",
        "task_ref": "train",
        "purpose": "test",
        "constraints": {"gpu_count": 1},
    }


def test_windows_desktop_bridge_keeps_group_constraint_and_strips_notes_from_claims(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(request)
        return FakeResponse({"ok": True})

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    before = dict(os.environ)
    result = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener).claim(
        {
            "project_id": "demo",
            "task_ref": "train",
            "purpose": "test",
            "constraints": {
                "gpu_count": 2,
                "same_host": True,
                "server_group_ids": ["training-a"],
                "server_group_id": "old-singular",
                "endpoint_ids": ["should-not-mix-when-present"],
                "environment_notes": "CUDA_VISIBLE_DEVICES=0",
                "unexpected": "drop-me",
            },
        }
    )

    assert result["ok"] is True
    assert json.loads(calls[0].data.decode("utf-8"))["constraints"] == {
        "gpu_count": 2,
        "same_host": True,
        "server_group_ids": ["training-a"],
        "endpoint_ids": ["should-not-mix-when-present"],
    }
    assert dict(os.environ) == before


def test_windows_desktop_bridge_keeps_same_host_and_grouped_claim_keys(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(request)
        return FakeResponse({"ok": True})

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    bridge = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener)
    grouped = bridge.claim(
        {
            "project_id": "demo",
            "task_ref": "train",
            "purpose": "test",
            "constraints": {
                "gpu_count": 1,
                "placement": "pack",
                "same_host": True,
                "server_group_ids": ["training-a"],
            },
        }
    )
    ungrouped = bridge.claim(
        {
            "project_id": "demo",
            "task_ref": "train",
            "purpose": "test",
            "constraints": {
                "gpu_count": 1,
                "placement": "pack",
                "same_host": True,
                "endpoint_ids": ["server-c"],
            },
        }
    )

    assert grouped["ok"] is ungrouped["ok"] is True
    assert json.loads(calls[0].data.decode("utf-8"))["constraints"] == {
        "gpu_count": 1,
        "placement": "pack",
        "same_host": True,
        "server_group_ids": ["training-a"],
    }
    assert json.loads(calls[1].data.decode("utf-8"))["constraints"] == {
        "gpu_count": 1,
        "placement": "pack",
        "same_host": True,
        "endpoint_ids": ["server-c"],
    }


def test_windows_desktop_bridge_allows_group_fields_on_endpoints_without_notes(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(request)
        return FakeResponse({"endpoint": {"id": "server-a"}})

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    bridge = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener)
    created = bridge.create_endpoint(
        {
            "id": "server-a",
            "host": "10.0.0.1",
            "port": 22,
            "ssh_user": "gpu",
            "workspace_path": "/srv/effective-must-drop",
            "workspace_path_override": "/srv/override",
            "server_group_id": "training-a",
            "observation_profile": "server-script-v1",
            "environment_notes": "must-not-leave-the-allowlist",
        }
    )
    inherited = bridge.create_endpoint(
        {
            "id": "server-b",
            "host": "10.0.0.2",
            "port": 22,
            "ssh_user": "gpu",
            "workspace_path": "/srv/effective-must-drop",
            "server_group_id": "training-a",
            "observation_profile": "server-script-v1",
        }
    )
    ungrouped = bridge.create_endpoint(
        {
            "id": "server-c",
            "host": "10.0.0.3",
            "port": 22,
            "ssh_user": "gpu",
            "workspace_path": "/srv/solo",
            "workspace_path_override": "/srv/must-omit",
            "observation_profile": "server-script-v1",
        }
    )
    updated = bridge.update_endpoint(
        "server-a",
        {"server_group_id": None, "workspace_path": "/srv/solo", "workspace_path_override": "/srv/must-omit", "environment_notes": "drop"},
    )

    assert created["ok"] is inherited["ok"] is ungrouped["ok"] is updated["ok"] is True
    create_body = json.loads(calls[0].data.decode("utf-8"))
    assert create_body["server_group_id"] == "training-a"
    assert create_body["workspace_path_override"] == "/srv/override"
    assert "workspace_path" not in create_body
    assert "environment_notes" not in create_body
    inherit_body = json.loads(calls[1].data.decode("utf-8"))
    assert inherit_body["server_group_id"] == "training-a"
    assert inherit_body["workspace_path_override"] is None
    assert "workspace_path" not in inherit_body
    ungrouped_body = json.loads(calls[2].data.decode("utf-8"))
    assert ungrouped_body["workspace_path"] == "/srv/solo"
    assert "workspace_path_override" not in ungrouped_body
    assert "server_group_id" not in ungrouped_body
    update_body = json.loads(calls[3].data.decode("utf-8"))
    assert calls[3].full_url == "http://127.0.0.1:8787/api/v1/endpoints/server-a"
    assert calls[3].get_method() == "PATCH"
    assert update_body["workspace_path"] == "/srv/solo"
    assert "workspace_path_override" not in update_body
    assert "environment_notes" not in update_body


def test_windows_desktop_bridge_sends_group_crud_notes_as_json_not_process_env(tmp_path: Path) -> None:
    launcher = load_launcher()
    calls = []

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        calls.append(request)
        return FakeResponse({"server_group": {"id": "training-a"}})

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    bridge = launcher.DesktopBridge("http://127.0.0.1:8787/", paths, opener=opener)
    environ = os.environ
    before = dict(environ)
    created = bridge.create_server_group(
        {
            "id": "training-a",
            "display_name": "训练 A",
            "workspace_path": "/srv/shared",
            "environment_notes": "module load cuda",
            "description": "A 区",
            "unexpected": "drop",
        }
    )
    updated = bridge.update_server_group(
        "training-a",
        {"display_name": "训练 A2", "id": "ignored", "environment_notes": "keep json only"},
    )
    deleted = bridge.delete_server_group("training-a")

    assert created["ok"] is updated["ok"] is deleted["ok"] is True
    assert json.loads(calls[0].data.decode("utf-8")) == {
        "id": "training-a",
        "display_name": "训练 A",
        "workspace_path": "/srv/shared",
        "environment_notes": "module load cuda",
        "description": "A 区",
    }
    update_body = json.loads(calls[1].data.decode("utf-8"))
    assert update_body["environment_notes"] == "keep json only"
    assert "id" not in update_body
    assert calls[2].get_method() == "DELETE"
    assert calls[2].full_url == "http://127.0.0.1:8787/api/v1/server-groups/training-a"
    assert dict(environ) == before
    assert all("module load cuda" not in str(value) for value in environ.values())
    assert all("keep json only" not in str(value) for value in environ.values())


def test_windows_desktop_bridge_maps_service_errors_without_exposing_transport_details(tmp_path: Path) -> None:
    launcher = load_launcher()

    def opener(request, *, timeout: float):  # type: ignore[no-untyped-def]
        del timeout
        raise HTTPError(
            request.full_url,
            422,
            "unprocessable entity",
            hdrs=None,
            fp=BytesIO('{"error":{"code":"no_capacity","message":"当前没有可用 GPU。"}}'.encode()),
        )

    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)
    result = launcher.DesktopBridge("http://127.0.0.1:8787", paths, opener=opener).snapshot()

    assert result == {
        "ok": False,
        "error": {"code": "no_capacity", "message": "当前没有可用 GPU。"},
    }


def test_windows_broker_health_requires_control_plane_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    launcher = load_launcher()

    class HealthResponse:
        def __init__(self, payload: dict[str, object], status: int = 200) -> None:
            self.payload = payload
            self.status = status

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

        def __enter__(self) -> HealthResponse:
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def live_control_plane(url: str, timeout: float) -> HealthResponse:
        assert url.endswith("/health/live")
        assert timeout == 0.8
        return HealthResponse({"status": "live", "capabilities": ["instant_claims", "control_plane_state"]})

    monkeypatch.setattr(launcher, "urlopen", live_control_plane)
    assert launcher.broker_health(8787) is True

    def deleted_board_only(url: str, timeout: float) -> HealthResponse:
        del url, timeout
        return HealthResponse({"status": "live", "capabilities": ["instant_claims"]})

    monkeypatch.setattr(launcher, "urlopen", deleted_board_only)
    assert launcher.broker_health(8787) is False


def test_windows_loopback_urlopen_ignores_proxy_environment(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    launcher = load_launcher()

    class Loopback(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = json.dumps(
                {"status": "live", "capabilities": ["instant_claims", "control_plane_state"]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    class Poison(BaseHTTPRequestHandler):
        hits = 0

        def do_GET(self) -> None:
            type(self).hits += 1
            self.send_response(502)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    loopback = ThreadingHTTPServer(("127.0.0.1", 0), Loopback)
    poison = ThreadingHTTPServer(("127.0.0.1", 0), Poison)
    Thread(target=loopback.serve_forever, daemon=True).start()
    Thread(target=poison.serve_forever, daemon=True).start()
    try:
        proxy = f"http://127.0.0.1:{poison.server_address[1]}"
        monkeypatch.setenv("HTTP_PROXY", proxy)
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        monkeypatch.setenv("ALL_PROXY", proxy)
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)

        assert launcher.broker_health(loopback.server_address[1]) is True
        assert Poison.hits == 0
    finally:
        loopback.shutdown()
        poison.shutdown()


def test_windows_bridge_rest_paths_exist_in_api() -> None:
    root = Path(__file__).resolve().parents[1]
    launcher_source = (root / "desktop" / "windows_launcher.py").read_text(encoding="utf-8")
    api_source = (root / "src" / "serverpilot" / "api.py").read_text(encoding="utf-8")
    required = (
        "/health/live",
        "/api/v1/state",
        "/api/v1/observation-profiles",
        "/api/v1/endpoints",
        "/api/v1/endpoints/{endpoint_id}/history",
        "/api/v1/server-groups",
        "/api/v1/server-groups/{group_id}",
        "/api/v1/claims",
        "/api/v1/endpoints/{endpoint_id}/keepalive",
        "/api/v1/settings/collector",
        "/api/v1/mcp-entry",
    )
    for path in required:
        assert path in api_source, path
        if "{" in path:
            assert path.split("{", 1)[0] in launcher_source, path
        else:
            assert path in launcher_source, path
    assert "coordination_board" not in launcher_source
    assert "/api/v1/coordination" not in launcher_source
    assert "/ui/" not in launcher_source
    assert "workload-profile" not in launcher_source
    assert "scheduler_targets" not in launcher_source


def test_windows_desktop_ui_is_a_packaged_surface_against_loopback_rest() -> None:
    root = Path(__file__).resolve().parents[1]
    index = root / "desktop" / "windows" / "ui" / "index.html"
    script = root / "desktop" / "windows" / "ui" / "app.js"
    spec = root / "desktop" / "windows" / "ServerPilotWindows.spec"
    launcher_source = (root / "desktop" / "windows_launcher.py").read_text(encoding="utf-8")
    js_source = script.read_text(encoding="utf-8")

    index_source = index.read_text(encoding="utf-8")
    assert "服务器" in index_source
    assert 'id="usage-page"' in index_source
    assert "外部调度器" not in index_source
    assert "GPU 显存状态" in js_source
    assert "endpoint_history" in js_source
    assert "usageRecords" in js_source
    assert "resource_claims" not in js_source
    assert "scheduler_targets" not in js_source
    assert "DesktopBridge" in launcher_source
    assert "web console" not in launcher_source.lower()
    assert "packaged" in launcher_source
    assert "loopback REST API" in launcher_source
    spec_source = spec.read_text(encoding="utf-8")
    assert '"desktop" / "windows" / "ui"' in spec_source
    assert 'icon=str(project_root / "desktop" / "assets" / "ServerPilot Icon.png")' in spec_source


def test_windows_desktop_window_uses_local_ui_and_webview2_http_host(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    launcher = load_launcher()
    calls: dict[str, object] = {}

    def create_window(title: str, **kwargs: object) -> None:
        calls["title"] = title
        calls["window"] = kwargs

    def start(**kwargs: object) -> None:
        calls["start"] = kwargs

    fake_webview = types.SimpleNamespace(create_window=create_window, start=start)
    monkeypatch.setitem(sys.modules, "webview", fake_webview)
    stopped = []
    server = types.SimpleNamespace(stop=lambda: stopped.append(True))
    paths = launcher.RuntimePaths(tmp_path, tmp_path / "inventory.yaml", "sqlite:///example.sqlite3", 8787, False)

    launcher.run_desktop_window("http://127.0.0.1:8787/", paths, server)

    assert calls["title"] == "ServerPilot"
    window_url = str(calls["window"]["url"]).replace("\\", "/")  # type: ignore[index]
    assert window_url.endswith("desktop/windows/ui/index.html")
    assert calls["start"] == {"gui": "edgechromium", "private_mode": True, "http_server": True}
    assert stopped == [True]
