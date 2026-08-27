from __future__ import annotations

from typer.testing import CliRunner

from serverpilot import __version__
from serverpilot import cli as cli_module
from serverpilot.cli import app as cli_app
from serverpilot.models import Endpoint
from tests.helpers import observation


def test_doctor_reports_control_plane_and_unreported_collector(service, admin) -> None:
    def mark_remote_collector(session):  # type: ignore[no-untyped-def]
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.observation_profile = "server-script-v1"

    service._write(mark_remote_collector)
    report = service.doctor(admin)["data"]
    assert report["versions"]["control_plane"] == __version__
    collectors = {item["endpoint_id"]: item for item in report["versions"]["collectors"]}
    assert collectors["endpoint-a"]["implementation_version"] is None
    assert collectors["endpoint-a"]["reported"] is False
    assert collectors["endpoint-a"]["status"] == "unreported"
    assert collectors["endpoint-b"]["status"] == "not_applicable"
    assert any("endpoint-a" in step and "重装" in step for step in report["next_steps"])
    assert isinstance(report["plugin_discovery_failures"], list)


def test_doctor_persists_and_compares_collector_implementation_version(service, admin) -> None:
    def mark_remote_collector(session):  # type: ignore[no-untyped-def]
        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.observation_profile = "server-script-v1"

    service._write(mark_remote_collector)
    service.ingest_observation(observation(count=1), implementation_version="1.9.0")
    mismatched = service.doctor(admin)["data"]
    collectors = {item["endpoint_id"]: item for item in mismatched["versions"]["collectors"]}
    assert collectors["endpoint-a"]["implementation_version"] == "1.9.0"
    assert collectors["endpoint-a"]["status"] == "mismatch"

    service.ingest_observation(observation(count=1), implementation_version=__version__)
    matched = service.doctor(admin)["data"]
    collectors = {item["endpoint_id"]: item for item in matched["versions"]["collectors"]}
    assert collectors["endpoint-a"]["implementation_version"] == __version__
    assert collectors["endpoint-a"]["status"] == "ok"

    service.ingest_observation(observation(count=1))
    cleared = service.doctor(admin)["data"]
    collectors = {item["endpoint_id"]: item for item in cleared["versions"]["collectors"]}
    assert collectors["endpoint-a"]["implementation_version"] is None
    assert collectors["endpoint-a"]["status"] == "unreported"


def test_cli_doctor_prints_version_table(monkeypatch) -> None:
    payload = {
        "schema_version": "v1",
        "data": {
            "versions": {
                "control_plane": "1.9.0",
                "mcp": {"available": True, "command": "/tmp/serverpilot-mcp", "version": "1.9.0"},
                "collectors": [
                    {
                        "endpoint_id": "server-10-40-0-199-p4580",
                        "observation_profile": "server-script-v1",
                        "implementation_version": None,
                        "reported": False,
                        "applies": True,
                        "status": "unreported",
                    }
                ],
            },
            "plugin_discovery_failures": [
                {
                    "path": "/tmp/legacy-plug",
                    "source": "local",
                    "error": "plugin info schema_version must be 3",
                    "plugin_id": "legacy-plug",
                }
            ],
            "next_steps": ["在 server-10-40-0-199-p4580 上重装与控制面同版本的 collector"],
        },
    }

    class FakeClient:
        def get(self, path):  # type: ignore[no-untyped-def]
            assert path == "/api/v1/doctor"
            return payload

    monkeypatch.setattr(cli_module, "_client", lambda url, actor: FakeClient())
    result = CliRunner().invoke(cli_app, ["doctor"])
    assert result.exit_code == 0
    assert "组件版本" in result.stdout
    assert "控制面 daemon" in result.stdout
    assert "本机 CLI" in result.stdout
    assert "MCP 入口" in result.stdout
    assert "collector server-10-40-0-199-p4580" in result.stdout
    assert "未报告" in result.stdout
    assert "legacy-plug" in result.stdout
    assert "plugin info schema_version must be 3" in result.stdout
    assert "重启控制面" in result.stdout
    assert "重装" in result.stdout
