"""Human tables and Agent JSON CLI, all operational commands routed through REST."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
import yaml

from serverpilot import __version__
from serverpilot.api import create_app
from serverpilot.client import BrokerClient, BrokerClientError
from serverpilot.collector import SSHCollector
from serverpilot.config import ProjectConfig, Settings, load_inventory
from serverpilot.daemon import (
    DaemonError,
    MacOSDaemonManager,
    daemon_instance_id_for_paths,
    format_status,
    resolve_daemon_config,
)
from serverpilot.database import Database
from serverpilot.importer import import_servers_files, write_inventory
from serverpilot.logging_setup import configure_logging
from serverpilot.mcp_entry import (
    MCP_CLIENTS,
    MCP_SERVER_NAME,
    MCPEntryUnavailable,
    mcp_registration,
    mcp_server_entry,
    resolve_mcp_command,
)
from serverpilot.schemas import (
    RequestCreate,
    RequestCreateFlat,
)
from serverpilot.service import BrokerError, BrokerService

app = typer.Typer(
    no_args_is_help=True,
    help="Single-user GPU/CPU coordination across projects and agents.",
)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", help="Print the installed version and exit.")
    ] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit


endpoint_app = typer.Typer(no_args_is_help=True)
gpu_app = typer.Typer(no_args_is_help=True)
request_app = typer.Typer(no_args_is_help=True)
lease_app = typer.Typer(
    no_args_is_help=True,
    help="Update cooperative lease state; never start or stop workloads.",
)
reservation_app = typer.Typer(no_args_is_help=True)
collect_app = typer.Typer(no_args_is_help=True)
daemon_app = typer.Typer(
    no_args_is_help=True,
    help="Manage the macOS user daemon; data is preserved when the daemon stops.",
)
app.add_typer(endpoint_app, name="endpoint")
app.add_typer(gpu_app, name="gpu")
app.add_typer(request_app, name="request")
app.add_typer(lease_app, name="lease")
app.add_typer(reservation_app, name="reservation")
plugin_app = typer.Typer(no_args_is_help=True, help="Discover and install local server plugins.")
keepalive_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect or stop remote keepalive workers without the control plane.",
)
mcp_app = typer.Typer(
    no_args_is_help=True,
    help="Register this installation's MCP server with an agent client.",
)
app.add_typer(collect_app, name="collect")
app.add_typer(daemon_app, name="daemon")
app.add_typer(plugin_app, name="plugin")
app.add_typer(keepalive_app, name="keepalive")
app.add_typer(mcp_app, name="mcp")


def _database_url(value: str) -> str:
    if value.startswith("sqlite:///"):
        return value
    return f"sqlite:///{Path(value).expanduser().resolve()}"


def _print(value: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    data = value.get("data", value) if isinstance(value, dict) else value
    if isinstance(data, list):
        if not data:
            typer.echo("(empty)")
            return
        if all(isinstance(item, dict) for item in data):
            keys = list(dict.fromkeys(key for item in data for key in item))
            keys = [key for key in keys if not isinstance(data[0].get(key), (dict, list))][:8]
            widths = {key: min(36, max(len(key), *(len(str(item.get(key, ""))) for item in data))) for key in keys}
            typer.echo("  ".join(key.ljust(widths[key]) for key in keys))
            typer.echo("  ".join("-" * widths[key] for key in keys))
            for item in data:
                typer.echo("  ".join(str(item.get(key, ""))[: widths[key]].ljust(widths[key]) for key in keys))
            return
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _client(url: str | None, actor: str | None) -> BrokerClient:
    try:
        return BrokerClient.from_env(url=url, actor=actor)
    except BrokerClientError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _call(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except BrokerClientError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


def _idempotency_key(value: str | None) -> str:
    return secrets.token_hex(16) if value is None else value


@app.command("init")
def init(
    db: Annotated[str, typer.Option("--db", help="SQLite path or sqlite:/// URL")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
) -> None:
    """Create or migrate local state. No application key is required."""

    service = BrokerService(Database(_database_url(db), Path.cwd()), load_inventory(inventory))
    service.initialize()
    typer.echo(f"initialized {db}")


@app.command("backup")
def backup(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    output: Annotated[Path, typer.Option("--output")] = Path("state/backups/serverpilot.sqlite3"),
) -> None:
    """Create a local SQLite backup after a WAL checkpoint; no remote resource is touched."""

    database = Database(_database_url(db), Path.cwd())
    typer.echo(str(database.backup(output)))


@app.command("restore")
def restore(
    source: Annotated[Path, typer.Option("--from", exists=True, readable=True)],
    target: Annotated[Path, typer.Option("--to")],
) -> None:
    """Validate and copy a backup to a new target; never overwrite a live DB."""

    typer.echo(str(Database.restore_to(source, target)))


@app.command("serve")
def serve(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8787,
    daemon_instance_id: Annotated[str | None, typer.Option("--daemon-instance-id")] = None,
) -> None:
    """Run the loopback-only FastAPI server; remote deployment requires separate approval."""

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("non-loopback bind requires an approved production deployment")
    if daemon_instance_id is not None:
        if db.startswith("sqlite:///"):
            database_path = Path(db.removeprefix("sqlite:///")).expanduser().resolve()
        elif "://" in db:
            raise typer.BadParameter(
                "--daemon-instance-id requires a local SQLite database path"
            )
        else:
            database_path = Path(db).expanduser().resolve()
        expected_instance_id = daemon_instance_id_for_paths(
            database_path,
            inventory.expanduser().resolve(),
        )
        if daemon_instance_id != expected_instance_id:
            raise typer.BadParameter(
                "--daemon-instance-id does not match --db and --inventory"
            )
        daemon_instance_id = expected_instance_id
    settings = Settings(
        database_url=_database_url(db),
        inventory_path=inventory,
        project_root=Path.cwd(),
        bind_host=host,
        bind_port=port,
        daemon_instance_id=daemon_instance_id,
    )
    configure_logging(resolve_daemon_config().log_dir)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        loop="asyncio",
        http="h11",
        ws="none",
    )


def _daemon_call(operation):  # type: ignore[no-untyped-def]
    try:
        return operation()
    except DaemonError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


@daemon_app.command("install")
def daemon_install(
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            help="Existing serverpilot project whose inventory/state should be migrated once.",
        ),
    ] = None,
    start: Annotated[bool, typer.Option("--start/--no-start")] = True,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Install the macOS user LaunchAgent and preserve/migrate existing local state."""

    result = _daemon_call(
        lambda: MacOSDaemonManager().install(source_root=source_root, start=start)
    )
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("ensure")
def daemon_ensure(
    source_root: Annotated[
        Path | None,
        typer.Option(
            "--source-root",
            exists=True,
            file_okay=False,
            help="Existing serverpilot project to migrate when no daemon data exists yet.",
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ensure the macOS user daemon is installed, running, and ready."""

    result = _daemon_call(lambda: MacOSDaemonManager().ensure(source_root=source_root))
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("status")
def daemon_status(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report daemon installation, launchd, health, and canonical data paths."""

    result = _daemon_call(lambda: MacOSDaemonManager().status())
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("start")
def daemon_start() -> None:
    """Start the installed macOS user daemon."""

    _daemon_call(lambda: MacOSDaemonManager().start())
    typer.echo("started")


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the macOS user daemon without deleting state or its installation."""

    _daemon_call(lambda: MacOSDaemonManager().stop())
    typer.echo("stopped")


@daemon_app.command("reclaim")
def daemon_reclaim(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Stop a ServerPilot service that holds the daemon port without launchd owning it."""

    result = _daemon_call(lambda: MacOSDaemonManager().reclaim())
    typer.echo(format_status(result, as_json=as_json))


@daemon_app.command("uninstall")
def daemon_uninstall(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove the LaunchAgent while preserving inventory, database, and logs."""

    result = _daemon_call(lambda: MacOSDaemonManager().uninstall())
    typer.echo(format_status(result, as_json=as_json))


@app.command("status")
def status(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).snapshot()), as_json)


@app.command("state")
def state(
    minimum_snapshot_revision: Annotated[
        int | None,
        typer.Option("--minimum-snapshot-revision", min=0, help="Wait for at least this revision."),
    ] = None,
    timeout_seconds: Annotated[
        float,
        typer.Option("--timeout-seconds", min=0, max=300),
    ] = 0,
    poll_interval_seconds: Annotated[
        float,
        typer.Option("--poll-interval-seconds", min=0.05, max=10),
    ] = 0.25,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable JSON")] = True,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")] = None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")] = None,
) -> None:
    """Return the canonical control-plane state envelope."""

    _print(
        _call(
            lambda: _client(url, actor).control_plane_state(
                minimum_snapshot_revision=minimum_snapshot_revision,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        ),
        as_json,
    )


@endpoint_app.command("list")
def endpoint_list(
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).endpoints()), as_json)


@gpu_app.command("list")
def gpu_list(
    state: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).gpus(state=state)), as_json)


@app.command("who")
def who(
    project: Annotated[str | None, typer.Option()] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).leases(project_id=project))
    _print(response, as_json)


def _request_from_file(path: Path) -> RequestCreate:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise typer.BadParameter("request YAML must be a mapping")
    return RequestCreate.model_validate(raw) if "constraints" in raw else RequestCreateFlat.model_validate(raw).canonical()


@request_app.command("create")
def request_create(
    file: Annotated[Path, typer.Option("--file", exists=True, readable=True)],
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    request_data = _request_from_file(file)
    response = _call(
        lambda: _client(url, actor).post(
            "/api/v1/requests",
            request_data.model_dump(mode="json"),
            idempotency_key=_idempotency_key(idempotency_key),
        )
    )
    _print(response, as_json)


@request_app.command("queue")
def request_queue(
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    response = _call(lambda: _client(url, actor).requests(queued_only=True))
    _print(response, as_json)


@request_app.command("cancel")
def request_cancel(
    request_id: str,
    idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None,
    as_json: Annotated[bool, typer.Option("--json")]=False,
    url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None,
    actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None,
) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/requests/{request_id}/cancel", {}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@lease_app.command("activate")
def lease_activate(lease_id: str, idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/activate", {}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@lease_app.command("renew")
def lease_renew(lease_id: str, idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/renew", {}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@lease_app.command("release")
def lease_release(lease_id: str, reason: Annotated[str, typer.Option("--reason")], idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/release", {"reason": reason}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@lease_app.command("bind")
def lease_bind(lease_id: str, run_id: Annotated[str, typer.Option("--run-id")], process_key: Annotated[list[str] | None, typer.Option("--process-key")]=None, idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/bind-workload", {"run_id": run_id, "process_keys": process_key or []}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@lease_app.command("bind-observed")
def lease_bind_observed(lease_id: str, run_id: Annotated[str | None, typer.Option("--run-id")] = None, idempotency_key: Annotated[str | None, typer.Option("--idempotency-key")] = None, as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).post(f"/api/v1/leases/{lease_id}/bind-observed-workload", {"run_id": run_id} if run_id is not None else {}, idempotency_key=_idempotency_key(idempotency_key))), as_json)


@reservation_app.command("list")
def reservation_list(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).reservations()), as_json)


@app.command("history")
def history(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    _print(_call(lambda: _client(url, actor).get("/api/v1/events")), as_json)


@app.command("doctor")
def doctor(as_json: Annotated[bool, typer.Option("--json")]=False, url: Annotated[str | None, typer.Option(envvar="SERVERPILOT_URL")]=None, actor: Annotated[str | None, typer.Option(envvar="SERVERPILOT_ACTOR")]=None) -> None:
    payload = _call(lambda: _client(url, actor).get("/api/v1/doctor"))
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            versions = data.setdefault("versions", {})
            if isinstance(versions, dict):
                versions["cli"] = __version__
    if as_json:
        _print(payload, True)
        return
    _print_doctor_report(payload)


def _print_doctor_report(payload: Any) -> None:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(data, dict):
        _print(payload, False)
        return
    versions = data.get("versions") if isinstance(data.get("versions"), dict) else {}
    control_plane = versions.get("control_plane")
    cli_version = versions.get("cli") or __version__
    mcp = versions.get("mcp") if isinstance(versions.get("mcp"), dict) else {}
    collectors = versions.get("collectors") if isinstance(versions.get("collectors"), list) else []
    rows: list[tuple[str, str, str]] = [
        (
            "控制面 daemon",
            _doctor_version_text(control_plane),
            _doctor_compare_status(control_plane, cli_version, present=True),
        ),
        ("本机 CLI", _doctor_version_text(cli_version), "当前安装"),
        (
            "MCP 入口",
            _doctor_version_text(mcp.get("version") if mcp.get("available") else None),
            (
                _doctor_compare_status(mcp.get("version"), cli_version, present=bool(mcp.get("available")))
                if mcp.get("available")
                else "未找到"
            ),
        ),
    ]
    for item in collectors:
        if not isinstance(item, dict):
            continue
        endpoint_id = str(item.get("endpoint_id") or "")
        applies = bool(item.get("applies"))
        reported = item.get("implementation_version")
        if applies:
            status = _doctor_compare_status(reported, cli_version, present=bool(item.get("reported")))
        else:
            status = "不适用（内置探针）"
        rows.append((f"collector {endpoint_id}", _doctor_version_text(reported), status))
    typer.echo("组件版本")
    component_width = max(8, *(len(row[0]) for row in rows))
    version_width = max(4, *(len(row[1]) for row in rows))
    typer.echo(
        f"{'组件'.ljust(component_width)}  {'版本'.ljust(version_width)}  状态"
    )
    typer.echo(
        f"{'-' * component_width}  {'-' * version_width}  ----"
    )
    for component, version, status in rows:
        typer.echo(f"{component.ljust(component_width)}  {version.ljust(version_width)}  {status}")
    failures = data.get("plugin_discovery_failures")
    typer.echo("")
    typer.echo("插件发现失败")
    if not failures:
        typer.echo("无")
    else:
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            label = failure.get("plugin_id") or failure.get("path") or "unknown"
            source = failure.get("source") or "unknown"
            error = failure.get("error") or ""
            typer.echo(f"- {label} ({source}): {error}")
    next_steps = list(data.get("next_steps") or [])
    if control_plane and control_plane != cli_version:
        next_steps.insert(0, "本机 CLI 与控制面版本不一致，请重启控制面后再运行 doctor")
    typer.echo("")
    typer.echo("下一步")
    if not next_steps:
        typer.echo("- 各组件版本一致，无需操作")
        return
    for step in next_steps:
        typer.echo(f"- {step}")


def _doctor_version_text(value: Any) -> str:
    if value is None or value == "":
        return "未报告"
    return str(value)


def _doctor_compare_status(actual: Any, expected: Any, *, present: bool) -> str:
    if not present or actual is None or actual == "":
        return "未报告"
    if actual == expected:
        return "一致"
    return "不一致"


@collect_app.command("once")
def collect_once(
    db: Annotated[str, typer.Option("--db")] = "state/serverpilot.sqlite3",
    inventory: Annotated[Path, typer.Option("--inventory", exists=True)] = Path("configs/inventory.yaml"),
) -> None:
    """Explicitly run fixed, read-only telemetry probes; this command never launches/terminates work."""

    config = load_inventory(inventory)
    service = BrokerService(Database(_database_url(db), Path.cwd()), config)
    service.initialize()
    typer.echo(json.dumps(asyncio.run(SSHCollector(config).collect_once(service)), ensure_ascii=False, indent=2))


@app.command("import-servers")
def import_servers(
    paths: Annotated[list[Path], typer.Argument(exists=True, readable=True)],
    project: Annotated[list[str], typer.Option("--project", help="Project id; repeat for multiple projects.")],
    workspace_path: Annotated[
        str,
        typer.Option("--workspace-path", help="Absolute remote working directory for the imported endpoints."),
    ],
    output: Annotated[Path, typer.Option("--output")] = Path("configs/inventory.yaml"),
) -> None:
    """Parse legacy files, deduplicate only exact host:port, and emit a new global config/report."""

    projects = [ProjectConfig(id=item, display_name=item.replace("-", " ").title()) for item in project]
    report = import_servers_files(
        paths,
        project_ids=project,
        workspace_path=workspace_path,
    )
    write_inventory(output, report, projects=projects)
    typer.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


def _keepalive_target(endpoint_id: str) -> tuple[Any, Any, list[str]]:
    """Resolve one endpoint's sealed adapter and the GPU UUIDs it currently exposes.

    Connection facts are read from the control-plane database, which owns
    endpoint inventory after bootstrap. A paused endpoint must stay reachable
    here, so this uses the single-endpoint accessor rather than the collector
    filter. The UUID set comes from a fresh read-only observation because the
    database is likely stale whenever this command is needed.
    """

    from sqlalchemy.exc import SQLAlchemyError

    from serverpilot.adapters import endpoint_keepalive_adapter
    from serverpilot.config import ConfigurationError
    from serverpilot.daemon import resolve_daemon_config

    daemon_config = resolve_daemon_config()
    try:
        inventory = load_inventory(daemon_config.inventory_path)
        service = BrokerService(
            Database(_database_url(str(daemon_config.database_path)), Path.cwd()), inventory
        )
        endpoint = service.collector_endpoint(endpoint_id)
    except (ConfigurationError, SQLAlchemyError, OSError) as exc:
        typer.echo(f"cannot read the control plane at {daemon_config.data_dir}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except BrokerError as exc:
        typer.echo(f"endpoint {endpoint_id} is not in {daemon_config.database_path}", err=True)
        raise typer.Exit(code=1) from exc
    if endpoint.keepalive_adapter_id is None:
        typer.echo(f"endpoint {endpoint_id} has no sealed keepalive adapter", err=True)
        raise typer.Exit(code=1)
    adapter = endpoint_keepalive_adapter(endpoint.keepalive_adapter_id)
    observation = asyncio.run(SSHCollector(inventory).observe_endpoint(endpoint))
    gpu_uuids = [gpu.gpu_uuid for gpu in observation.gpus]
    if not gpu_uuids:
        typer.echo(f"endpoint {endpoint_id} reported no GPUs", err=True)
        raise typer.Exit(code=1)
    return endpoint, adapter, gpu_uuids


@keepalive_app.command("inspect")
def keepalive_inspect(
    endpoint: Annotated[str, typer.Option("--endpoint")],
) -> None:
    """Report which keepalive workers the endpoint helper is holding; changes nothing."""

    target, adapter, gpu_uuids = _keepalive_target(endpoint)
    attestation = asyncio.run(adapter.attest_workers(target, gpu_uuids))
    _print(
        {
            "data": {
                "endpoint_id": target.id,
                "observed_gpu_count": len(gpu_uuids),
                "workers": [
                    {
                        "gpu_uuid": worker.gpu_uuid,
                        "pid": worker.pid,
                        "driver_pid": worker.driver_pid,
                    }
                    for worker in attestation.workers
                ],
            }
        },
        as_json=True,
    )


@keepalive_app.command("stop")
def keepalive_stop(
    endpoint: Annotated[str, typer.Option("--endpoint")],
) -> None:
    """Stop every keepalive worker on one endpoint, freeing the GPUs it occupies."""

    target, adapter, gpu_uuids = _keepalive_target(endpoint)
    response = asyncio.run(adapter.set_enabled(target, False, gpu_uuids))
    _print(
        {
            "data": {
                "endpoint_id": target.id,
                "enabled": response.enabled,
                "results": [
                    {
                        "gpu_uuid": result.gpu_uuid,
                        "status": result.status,
                        "outcome": result.outcome,
                    }
                    for result in response.results
                ],
            }
        },
        as_json=True,
    )


def _mcp_command() -> str:
    try:
        return resolve_mcp_command()
    except MCPEntryUnavailable as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc


_mcp_registration = mcp_registration
_mcp_server_entry = mcp_server_entry


def _write_cursor_config(target: Path, entry: dict[str, Any]) -> None:
    document: dict[str, Any] = {}
    if target.is_file():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            typer.echo(f"{target} is not valid JSON: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        if not isinstance(loaded, dict):
            typer.echo(f"{target} must contain a JSON object", err=True)
            raise typer.Exit(code=1)
        document = loaded
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[MCP_SERVER_NAME] = entry
    document["mcpServers"] = servers
    target.parent.mkdir(parents=True, exist_ok=True)
    # A truncated write here would take every other MCP server down with it.
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            staged = Path(handle.name)
        os.replace(staged, target)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


@mcp_app.command("config")
def mcp_config(
    client: Annotated[str, typer.Option("--client", help=f"One of {', '.join(MCP_CLIENTS)}, or all.")] = "all",
) -> None:
    """Print the MCP registration for one client without changing anything."""

    clients = MCP_CLIENTS if client == "all" else (client,)
    if any(item not in MCP_CLIENTS for item in clients):
        typer.echo(f"--client must be one of {', '.join(MCP_CLIENTS)}, or all", err=True)
        raise typer.Exit(code=1)
    command = _mcp_command()
    _print({"data": [mcp_registration(item, command) for item in clients]}, as_json=True)


@mcp_app.command("install")
def mcp_install(
    client: Annotated[str, typer.Option("--client", help=f"One of {', '.join(MCP_CLIENTS)}.")],
) -> None:
    """Register the MCP server with one agent client through that client's own mechanism."""

    if client not in MCP_CLIENTS:
        typer.echo(f"--client must be one of {', '.join(MCP_CLIENTS)}", err=True)
        raise typer.Exit(code=1)
    command = _mcp_command()
    registration = mcp_registration(client, command)
    if client == "cursor":
        target = Path(registration["target"])
        _write_cursor_config(target, mcp_server_entry(command))
        typer.echo(f"registered {MCP_SERVER_NAME} in {target}")
        return
    argv = registration["command_line"]
    if shutil.which(argv[0]) is None:
        typer.echo(
            f"{argv[0]} is not on PATH; install that client or run "
            f"`serverpilot mcp config --client {client}` and register it yourself",
            err=True,
        )
        raise typer.Exit(code=1)
    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        typer.echo((result.stderr or result.stdout).strip(), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"registered {MCP_SERVER_NAME} with {client}")


@plugin_app.command("list")
def plugin_list() -> None:
    """List built-in observation profiles and discovered local plugins."""

    from serverpilot.plugins import list_observation_profiles

    _print({"data": list_observation_profiles()}, as_json=True)


@plugin_app.command("add")
def plugin_add(path: Annotated[Path, typer.Argument(exists=True, readable=True)]) -> None:
    """Validate a plugin executable and copy it into the user plugin directory."""

    from serverpilot.plugins import PluginError, add_plugin

    try:
        info = add_plugin(path)
    except PluginError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"installed {info.plugin_id} -> {info.path}")


if __name__ == "__main__":
    app()
