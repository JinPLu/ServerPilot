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
    is_known_observation_profile,
    is_valid_plugin_id,
    list_observation_profiles,
    observe_plugin,
    probe_plugin,
    release_plugin,
    user_plugin_dir,
)
from serverpilot.timeutil import utcnow


def _write_plugin(directory: Path, plugin_id: str, script: str) -> Path:
    path = directory / plugin_id
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _info_script(
    plugin_id: str = "sample-plug",
    capabilities: str = '["observe"]',
    extra: str = "",
) -> str:
    return f"""#!/usr/bin/env python3
import json, sys
if sys.argv[1:] == ["info"]:
    json.dump({{
        "plugin_id": "{plugin_id}",
        "display_name": "Sample",
        "schema_version": 2,
        "capabilities": {capabilities},
    }}, sys.stdout)
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
    sys.stdout.write({json.dumps(snapshot)!r})
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
    assert observation.host.cpu_count == 8
    assert observation.gpu_probe_status == "cpu_only"

    bad = user / "broken-plug"
    bad.write_text(
        _info_script("broken-plug", extra='if sys.argv[1:] == ["observe"]:\n    print("not-json")\n    raise SystemExit(0)\n'),
        encoding="utf-8",
    )
    bad.chmod(0o755)
    with pytest.raises(CollectionError):
        parse_server_script_snapshot(
            observe_plugin("broken-plug"),
            endpoint_id="x",
            observed_at=utcnow(),
        )


def test_unknown_profile_is_rejected() -> None:
    assert is_known_observation_profile("linux-nvidia")
    assert not is_known_observation_profile("not-a-plugin")
    with pytest.raises(Exception, match="unknown observation profile"):
        EndpointConfig(
            id="ep-1",
            host="host.example.test",
            port=22,
            ssh_user="monitor",
            workspace_path="/srv/work",
            observation_profile="not-a-plugin",
        )


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
    json.dump({
        "allocation_ref": "alloc-1",
        "gpus": [],
        "ssh": {"host": "node01.example.test", "port": 22, "user": "alice"},
        "workspace_path": "/home/work",
        "cuda_visible_devices": "0,1",
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
    applied = apply_plugin("alloc-plug", gpu_count=2, task_ref="train-a")
    assert applied["allocation_ref"] == "alloc-1"
    assert applied["cuda_visible_devices"] == "0,1"
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
    sys.stdout.write({json.dumps(snapshot)!r})
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
    assert observation.host.cpu_count == 16
    assert observation.gpu_probe_status == "cpu_only"


def test_bundled_slurm_immediate_declares_observe_apply_release() -> None:
    path = bundled_plugin_dir() / "slurm-immediate"
    assert path.is_file()
    info = probe_plugin(path, source="builtin")
    assert info.plugin_id == "slurm-immediate"
    assert info.capabilities == ("observe", "apply", "release")
    profiles = {item["id"] for item in list_observation_profiles()}
    assert "slurm-immediate" in profiles
    assert {"linux-nvidia", "linux-host", "server-script-v1"} <= profiles


def test_slurm_immediate_sinfo_parsers_and_job_name() -> None:
    import types

    path = bundled_plugin_dir() / "slurm-immediate"
    module = types.ModuleType("slurm_immediate_plugin")
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    assert module.parse_idx("0-4,6") == {0, 1, 2, 3, 4, 6}
    assert module.parse_gres_used_indexes("gpu:a100:6(IDX:0-4,6)", 8) == {0, 1, 2, 3, 4, 6}
    assert module.parse_gres_used_indexes("gpu:a100:0", 8) == set()
    assert module.parse_gres_used_indexes("(null)", 8) == set()
    assert module.is_unschedulable("drng")
    assert module.is_unschedulable("mix+drain")
    assert not module.is_unschedulable("mix")
    assert not module.is_unschedulable("alloc")
    assert module.job_name_from_task_ref("train/a@1") == "sp-train-a-1"
    source = path.read_text(encoding="utf-8")
    assert "squeue -u" in source or "squeue" in source
    assert "write_sidecar(first" in source
    assert "srun --overlap" not in source


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
        "scheduler": {"free_gpu_count": 12, "gpu_name": "Example GPU 80GB"},
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
payloads = {{"empty": {json.dumps(empty)!r}, "owned": {json.dumps(owned)!r}}}
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
    assert empty_obs.gpu_probe_status == "cpu_only"
    assert empty_obs.scheduler["free_gpu_count"] == 12
    monkeypatch.setenv("OWN_GPUS", "1")
    owned_obs = parse_server_script_snapshot(
        observe_plugin("own-plug"), endpoint_id="cluster-ep", observed_at=utcnow()
    )
    assert [gpu.gpu_uuid for gpu in owned_obs.gpus] == ["GPU-real-1"]
    assert owned_obs.gpus[0].cuda_ordinal == 0
