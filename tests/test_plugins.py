from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from serverpilot.collector import CollectionError, SSHCollector, parse_server_script_snapshot
from serverpilot.config import CollectorConfig, EndpointConfig, InventoryConfig
from serverpilot.plugins import (
    PLUGIN_ID_PATTERN,
    PluginError,
    add_plugin,
    apply_plugin,
    bundled_plugin_dir,
    discover_plugins,
    discover_plugins_with_failures,
    is_known_observation_profile,
    is_valid_plugin_id,
    list_observation_profiles,
    observe_plugin,
    parse_plugin_limits,
    probe_plugin,
    release_plugin,
    user_plugin_dir,
)
from serverpilot.timeutil import utcnow
from tests.helpers import probe_bundle

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a plugin is a POSIX executable found by its shebang and executable bit",
)


def _write_plugin(directory: Path, plugin_id: str, script: str) -> Path:
    path = directory / plugin_id
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _info_limits() -> dict[str, object]:
    return {
        "lease_ends": "on_release",
        "max_lease_seconds": None,
        "apply_max_seconds": None,
        "queues": False,
    }


def _info_script(
    plugin_id: str = "sample-plug",
    capabilities: str = '["observe"]',
    extra: str = "",
    limits: dict[str, object] | None = None,
    schema_version: int = 5,
) -> str:
    payload_limits = json.dumps(_info_limits() if limits is None else limits)
    return f"""#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ["info"]:
    payload = {{
        "plugin_id": "{plugin_id}",
        "display_name": "Sample",
        "schema_version": {schema_version},
        "capabilities": {capabilities},
        "release_granularity": "allocation",
        "entry_kind": "allocation",
        "limits": json.loads({payload_limits!r}),
    }}
    json.dump(payload, sys.stdout)
    raise SystemExit(0)
{extra}
raise SystemExit("unexpected argv")
"""


def test_plugin_id_rejects_too_long_or_invalid_names() -> None:
    assert is_valid_plugin_id("slurm-immediate")
    assert PLUGIN_ID_PATTERN.fullmatch("a" + "b" * 39)
    assert not is_valid_plugin_id("A")
    assert not is_valid_plugin_id("1bad")
    assert not is_valid_plugin_id("a" * 41)


def test_discover_plugins_probes_info_and_prefers_user_dir(tmp_path: Path) -> None:
    bundled = tmp_path / "bundled"
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    bundled.mkdir()
    user.mkdir(parents=True)
    _write_plugin(bundled, "sample-plug", _info_script())
    _write_plugin(
        user,
        "sample-plug",
        _info_script().replace('"display_name": "Sample"', '"display_name": "User Sample"'),
    )
    monkey_home = tmp_path
    plugins = discover_plugins(home=monkey_home, environment={})
    # The real bundled dir is still searched; isolate by only asserting the
    # user-dir override for sample-plug when we patch search via home.
    found = {item.plugin_id: item for item in plugins}
    assert found["sample-plug"].display_name == "User Sample"
    assert found["sample-plug"].source == "local"


def test_probe_plugin_rejects_unknown_capability(tmp_path: Path) -> None:
    path = _write_plugin(tmp_path, "sample-plug", _info_script(capabilities='["observe", "shell"]'))
    with pytest.raises(PluginError, match="capabilities"):
        probe_plugin(path, source="local")


def test_observe_reuses_schema_v2_and_rejects_bad_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    snapshot = {
        "schema_version": 2,
        "identity": {"hostname": "login", "boot_id": "boot-1"},
        "host": {
            "cpu_count": 8,
            "load_1m": 0.25,
            "cpu_total_ticks": 1000,
            "cpu_idle_ticks": 800,
            "memory_total_mib": 16000,
            "memory_available_mib": 8000,
        },
        "gpu_probe_available": False,
        "gpu_probe_status": "cpu_only",
        "gpus": [],
        "processes": [],
    }
    extra = f"""
if sys.argv[1:] == ["observe"]:
    sys.stdout.write({json.dumps(probe_bundle(snapshot))!r})
    raise SystemExit(0)
"""
    _write_plugin(user, "sample-plug", _info_script(extra=extra))
    monkeypatch.setattr(
        "serverpilot.plugins.user_plugin_dir",
        lambda **_kwargs: user,
    )
    raw = observe_plugin("sample-plug")
    observation = parse_server_script_snapshot(
        raw, endpoint_id="cluster-ep", observed_at=utcnow()
    )
    assert observation.nodes[0].host.cpu_count == 8
    assert observation.nodes[0].gpu_probe_status == "cpu_only"

    bad = user / "broken-plug"
    bad.write_text(
        _info_script("broken-plug", extra='if sys.argv[1:] == ["observe"]:\n    print("not-json")\n    raise SystemExit(0)\n'),
        encoding="utf-8",
    )
    bad.chmod(0o755)
    with pytest.raises(PluginError, match="not valid JSON"):
        parse_server_script_snapshot(
            observe_plugin("broken-plug"),
            endpoint_id="x",
            observed_at=utcnow(),
        )


def test_info_is_probed_once_per_file_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery may run on every collection and every claim; ``info`` may not.

    The directory listing stays live so a plugin dropped in later is still
    found, but the subprocess is remembered until the file itself changes.
    """

    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    counter = tmp_path / "info-calls"
    counter.write_text("0", encoding="utf-8")
    probe = (
        "\nimport pathlib\n"
        f"_c = pathlib.Path({str(counter)!r})\n"
        '_c.write_text(str(int(_c.read_text()) + 1))\n'
    )
    script = _info_script("count-plug").replace("import json, sys", "import json, sys" + probe)
    _write_plugin(user, "count-plug", script)
    monkeypatch.setattr("serverpilot.plugins.user_plugin_dir", lambda **_kwargs: user)

    for _ in range(5):
        assert is_known_observation_profile("count-plug")
    assert counter.read_text(encoding="utf-8") == "1"

    # A plugin dropped in while the daemon runs is still discovered.
    _write_plugin(user, "later-plug", _info_script("later-plug"))
    assert is_known_observation_profile("later-plug")
    assert counter.read_text(encoding="utf-8") == "1"

    # Rewriting the file invalidates the remembered verdict.
    _write_plugin(user, "count-plug", script + "\n# changed\n")
    assert is_known_observation_profile("count-plug")
    assert counter.read_text(encoding="utf-8") == "2"


def test_unknown_profile_is_rejected() -> None:
    assert is_known_observation_profile("linux")
    assert not is_known_observation_profile("not-a-plugin")


def test_observe_timeout_and_overlong_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    extra = """
import time
if sys.argv[1:] == ["observe"]:
    time.sleep(2)
    raise SystemExit(0)
"""
    _write_plugin(user, "slow-plug", _info_script("slow-plug", extra=extra))
    monkeypatch.setattr("serverpilot.plugins.user_plugin_dir", lambda **_kwargs: user)
    monkeypatch.setattr("serverpilot.plugins.PLUGIN_OBSERVE_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(PluginError, match="timed out"):
        observe_plugin("slow-plug")

    extra_long = """
if sys.argv[1:] == ["observe"]:
    sys.stdout.write("x" * 2_000_000)
    raise SystemExit(0)
"""
    _write_plugin(user, "huge-plug", _info_script("huge-plug", extra=extra_long))
    with pytest.raises(PluginError, match="output limit"):
        observe_plugin("huge-plug")


def test_apply_and_release_parse_typed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    extra = """
if sys.argv[1] == "apply":
    assert sys.argv[sys.argv.index("--duration-seconds") + 1] == "3600"
    json.dump({
        "allocation_ref": "alloc-1", "request_id": "request-1", "state": "active",
        "locations": [{"node": "node01", "gpu_count": 2, "devices": []}],
        "entry": {"kind": "allocation", "host": "login", "port": 22, "user": "alice",
                  "workspace_path": "/home/work", "argv": ["srun", "--jobid=alloc-1", "bash"],
                  "instructions": "Enter existing allocation", "verified": False},
        "expires_at": None, "observed_at": "2026-09-08T01:00:00Z", "workload_observed_at": None,
    }, sys.stdout)
    raise SystemExit(0)
if sys.argv[1] == "release":
    json.dump({"state": "released"}, sys.stdout)
    raise SystemExit(0)
"""
    _write_plugin(
        user,
        "alloc-plug",
        _info_script("alloc-plug", capabilities='["observe", "apply", "release"]', extra=extra),
    )
    monkeypatch.setattr("serverpilot.plugins.user_plugin_dir", lambda **_kwargs: user)
    applied = apply_plugin("alloc-plug", gpu_count=2, task_ref="train-a", duration_seconds=3600, request_id="request-1", requirements={})
    assert applied["allocation_ref"] == "alloc-1"
    assert applied["locations"][0]["devices"] == []
    assert applied["entry"]["verified"] is False
    assert release_plugin("alloc-plug", allocation_ref="alloc-1") == {"state": "released"}


def test_add_plugin_copies_into_user_dir(tmp_path: Path) -> None:
    source = _write_plugin(tmp_path, "copy-plug", _info_script("copy-plug"))
    home = tmp_path / "home"
    info = add_plugin(source, home=home, environment={})
    assert info.plugin_id == "copy-plug"
    assert info.path == user_plugin_dir(home=home, environment={}) / "copy-plug"
    assert info.path.is_file()
    assert os.access(info.path, os.X_OK)


def test_collector_observe_endpoint_uses_local_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    snapshot = {
        "schema_version": 2,
        "identity": {"hostname": "login", "boot_id": "boot-1"},
        "host": {
            "cpu_count": 16,
            "load_1m": 1.5,
            "cpu_total_ticks": 2000,
            "cpu_idle_ticks": 1000,
            "memory_total_mib": 32000,
            "memory_available_mib": 12000,
        },
        "gpu_probe_available": False,
        "gpu_probe_status": "cpu_only",
        "gpus": [],
        "processes": [],
    }
    extra = f"""
if sys.argv[1:] == ["observe"]:
    sys.stdout.write({json.dumps(probe_bundle(snapshot))!r})
    raise SystemExit(0)
"""
    _write_plugin(user, "sample-plug", _info_script(extra=extra))
    monkeypatch.setattr("serverpilot.plugins.user_plugin_dir", lambda **_kwargs: user)
    endpoint = EndpointConfig(
        id="plugin-ep",
        host="ignored.example.test",
        port=22,
        ssh_user="monitor",
        workspace_path="/srv/work",
        observation_profile="sample-plug",
    )
    collector = SSHCollector(
        InventoryConfig(schema_version=1, collector=CollectorConfig(), endpoints=[endpoint])
    )
    observation = asyncio.run(collector.observe_endpoint(endpoint))
    assert observation.nodes[0].host.cpu_count == 16
    assert observation.nodes[0].gpu_probe_status == "cpu_only"


def test_bundled_slurm_immediate_declares_observe_apply_release() -> None:
    path = bundled_plugin_dir() / "slurm-immediate"
    assert path.is_file()
    info = probe_plugin(path, source="builtin")
    assert info.plugin_id == "slurm-immediate"
    assert info.capabilities == ("observe", "apply", "release", "connect", "disconnect")
    assert info.schema_version == 5
    assert info.limits == {
        "lease_ends": "hard_kill_at_time_limit",
        "max_lease_seconds": 2592000,
        "apply_max_seconds": 40,
        "queues": False,
    }
    profiles = {item["id"] for item in list_observation_profiles()}
    assert "slurm-immediate" in profiles
    assert "linux" in profiles


def test_slurm_immediate_sinfo_parsers_and_job_name() -> None:
    import types

    path = bundled_plugin_dir() / "slurm-immediate"
    module = types.ModuleType("slurm_immediate_plugin")
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    # Exercise the helper sent over SSH, not a second local parser.
    import ast

    remote = types.ModuleType("slurm_remote")
    remote.re = __import__("re")
    remote.UNSCHEDULABLE_TOKENS = module.UNSCHEDULABLE_TOKENS
    tree = ast.parse(module.REMOTE_HELPER)
    functions = ast.Module(
        body=[node for node in tree.body if isinstance(node, ast.FunctionDef)],
        type_ignores=[],
    )
    exec(compile(functions, "slurm-remote", "exec"), remote.__dict__)
    assert remote.parse_idx("0-4,6") == {0, 1, 2, 3, 4, 6}
    assert remote.parse_gres_used_indexes("gpu:a100:6(IDX:0-4,6)", 8) == {0, 1, 2, 3, 4, 6}
    assert remote.parse_gres_used_indexes("gpu:a100:0", 8) == set()
    assert remote.parse_gres_used_indexes("(null)", 8) == set()
    assert remote.parse_gres_used_indexes("gpu:a100:0(IDX:N/A)", 8) == set()
    assert len(remote.parse_gres_used_indexes("gpu:a100:5(IDX:N/A)", 8)) == 5
    assert len(remote.parse_gres_used_indexes("gpu:5(IDX:N/A)", 8)) == 5
    with pytest.raises(ValueError, match="allocation count"):
        remote.parse_gres_used_indexes("gpu:a100:unknown(IDX:N/A)", 8)
    assert remote.is_unschedulable("drng")
    assert remote.is_unschedulable("mix+drain")
    assert not remote.is_unschedulable("mix")
    assert not remote.is_unschedulable("alloc")
    remote.PARTITION = "test"
    remote.GPUS_PER_NODE = 8
    rows = (
        f"{'node1':32}{'mix':20}{'gpu:a100:8':40}gpu:a100:5(IDX:N/A)\n"
        f"{'node2':32}{'idle':20}{'gpu:a100:8':40}gpu:a100:0(IDX:N/A)\n"
        f"{'node3':32}{'drain':20}{'gpu:a100:8':40}gpu:a100:0(IDX:N/A)\n"
    )
    remote.subprocess = types.SimpleNamespace(check_output=lambda *args, **kwargs: rows)
    capacity = remote.cluster_gpu_capacity()
    assert capacity["free_gpu_count"] == 11
    assert capacity["largest_free_block"] == 8
    assert [node["name"] for node in capacity["nodes"]] == ["node1", "node2", "node3"]
    assert capacity["nodes"][-1]["schedulable"] is False
    assert capacity["nodes"][-1]["allocated_gpus"] == 0
    remote.job_details = lambda job_id: {"JobState": "RUNNING", "NodeList": "node2", "TRES": "cpu=64,node=1,gres/gpu=8"}
    remote.subprocess = types.SimpleNamespace(run=lambda *args, **kwargs: types.SimpleNamespace(stdout="node2\n", returncode=0))
    remote.timestamp = lambda: "2026-09-08T05:00:00Z"
    fact = remote.allocation_fact("901")
    assert fact["locations"] == [{"node": "node2", "gpu_count": 8, "devices": [], "running_process_count": None}]
    assert fact["request_id"] is None


def test_plugin_observe_reports_own_gpus_and_empty_cluster_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    empty = {
        "schema_version": 2,
        "identity": {"hostname": "login", "boot_id": "boot-1"},
        "host": {
            "cpu_count": 8,
            "load_1m": 0.1,
            "cpu_total_ticks": 100,
            "cpu_idle_ticks": 80,
            "memory_total_mib": 16000,
            "memory_available_mib": 8000,
        },
        "gpu_probe_available": False,
        "gpu_probe_status": "cpu_only",
        "gpus": [],
        "processes": [],
        "scheduler": {
            "free_gpu_count": 12, "gpu_name": "Example GPU 80GB",
            "nodes": [
                {"name": "node-a", "state": "idle", "total_gpus": 8,
                 "allocated_gpus": 0, "schedulable": True},
                {"name": "node-b", "state": "mix", "total_gpus": 8,
                 "allocated_gpus": 4, "schedulable": True},
            ],
        },
    }
    owned = {
        **empty,
        "gpu_probe_available": True,
        "gpu_probe_status": "gpu",
        "gpus": [
            {
                "gpu_index": 0,
                "cuda_ordinal": 0,
                "gpu_uuid": "GPU-real-1",
                "name": "Example GPU 80GB",
                "total_vram_mib": 81920,
                "memory_used_mib": 0,
                "memory_free_mib": 81920,
                "gpu_utilization_pct": 0,
                "memory_utilization_pct": 0,
                "temperature_c": 30,
                "power_watts": 50.0,
                "pstate": "P0",
                "health": "OK",
            }
        ],
        "processes": [],
        "scheduler": {"free_gpu_count": 11, "gpu_name": "Example GPU 80GB"},
    }
    extra = f"""
import os
payloads = {{"empty": {json.dumps(probe_bundle(empty))!r}, "owned": {json.dumps(probe_bundle(owned))!r}}}
if sys.argv[1:] == ["observe"]:
    sys.stdout.write(payloads["empty"] if not os.environ.get("OWN_GPUS") else payloads["owned"])
    raise SystemExit(0)
"""
    _write_plugin(user, "own-plug", _info_script("own-plug", extra=extra))
    monkeypatch.setattr("serverpilot.plugins.user_plugin_dir", lambda **_kwargs: user)
    empty_obs = parse_server_script_snapshot(
        observe_plugin("own-plug"), endpoint_id="cluster-ep", observed_at=utcnow()
    )
    assert empty_obs.gpus == []
    assert empty_obs.nodes[0].gpu_probe_status == "cpu_only"
    assert empty_obs.scheduler["free_gpu_count"] == 12
    assert empty_obs.scheduler["nodes"] == empty["scheduler"]["nodes"]
    invalid = {**empty, "scheduler": {**empty["scheduler"], "nodes": [
        {"name": "bad", "state": "mix", "total_gpus": 8,
         "allocated_gpus": 9, "schedulable": True},
    ]}}
    with pytest.raises(CollectionError, match="allocated GPUs exceed"):
        parse_server_script_snapshot(json.dumps(probe_bundle(invalid)), endpoint_id="cluster-ep", observed_at=utcnow())
    monkeypatch.setenv("OWN_GPUS", "1")
    owned_obs = parse_server_script_snapshot(
        observe_plugin("own-plug"), endpoint_id="cluster-ep", observed_at=utcnow()
    )
    assert [gpu.gpu_uuid for gpu in owned_obs.gpus] == ["GPU-real-1"]
    assert owned_obs.gpus[0].cuda_ordinal == 0


def test_parse_plugin_limits_enforces_lease_ends_and_unknown_keys() -> None:
    assert parse_plugin_limits(_info_limits()) == _info_limits()
    hard = {
        "lease_ends": "hard_kill_at_time_limit",
        "max_lease_seconds": 2592000,
        "apply_max_seconds": 40,
        "queues": False,
    }
    assert parse_plugin_limits(hard) == hard
    with pytest.raises(PluginError, match="unknown keys"):
        parse_plugin_limits({**_info_limits(), "extra": True})
    with pytest.raises(PluginError, match="must be null"):
        parse_plugin_limits({**_info_limits(), "max_lease_seconds": 60})
    with pytest.raises(PluginError, match="positive integer"):
        parse_plugin_limits(
            {
                "lease_ends": "hard_kill_at_time_limit",
                "max_lease_seconds": None,
                "apply_max_seconds": None,
                "queues": False,
            }
        )
    with pytest.raises(PluginError, match="must be false"):
        parse_plugin_limits({**_info_limits(), "queues": True})


def test_discover_plugins_exposes_schema_v2_probe_failure(tmp_path: Path) -> None:
    user = tmp_path / "Library/Application Support/ServerPilot/plugins"
    user.mkdir(parents=True)
    _write_plugin(user, "sample-plug", _info_script())
    _write_plugin(user, "legacy-plug", _info_script("legacy-plug", schema_version=2))
    plugins, failures = discover_plugins_with_failures(home=tmp_path, environment={})
    found = {item.plugin_id: item for item in plugins}
    assert "sample-plug" in found
    assert "legacy-plug" not in found
    assert any(
        failure.plugin_id == "legacy-plug" and "schema_version must be 5" in failure.error
        for failure in failures
    )


def test_probe_plugin_rejects_schema_v2_and_missing_limits(tmp_path: Path) -> None:
    path = _write_plugin(tmp_path, "sample-plug", _info_script(schema_version=2))
    with pytest.raises(PluginError, match="schema_version must be 5"):
        probe_plugin(path, source="local")
    missing = """#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ["info"]:
    json.dump({
        "plugin_id": "sample-plug",
        "display_name": "Sample",
        "schema_version": 5,
        "capabilities": ["observe"],
        "release_granularity": "allocation", "entry_kind": "allocation",
    }, sys.stdout)
    raise SystemExit(0)
raise SystemExit("unexpected argv")
"""
    path = _write_plugin(tmp_path, "sample-plug", missing)
    with pytest.raises(PluginError, match="limits"):
        probe_plugin(path, source="local")
