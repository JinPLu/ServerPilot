import json
from copy import deepcopy
from datetime import timedelta

from serverpilot.collector import parse_raw_node_probe, parse_server_script_snapshot
from serverpilot.schemas import EndpointObservation
from serverpilot.timeutil import utcnow
from tests.helpers import observation, probe_bundle, process_for_gpu


def bundle():
    return probe_bundle({
        "identity": {"hostname": "node", "boot_id": "boot"},
        "host": {"cpu_count": 8, "load_1m": 1, "memory_total_mib": 16000,
                 "memory_available_mib": 8000, "cpu_total_ticks": 100, "cpu_idle_ticks": 50},
        "gpus": [{"gpu_index": 0, "gpu_uuid": "GPU-1", "name": "A100", "total_vram_mib": 80000,
                  "memory_used_mib": 40000, "memory_free_mib": 40000, "gpu_utilization_pct": 60}],
        "processes": [],
    })


def test_direct_and_plugin_share_exact_node_parser():
    wire = bundle()
    now = utcnow()
    wire["nodes"][0]["observed_at"] = now.isoformat()
    direct = parse_raw_node_probe(wire["nodes"][0]["output"], node_id="self", observed_at=now)
    plugin = parse_server_script_snapshot(json.dumps(wire), endpoint_id="endpoint-a", observed_at=now)
    assert plugin.nodes == [direct]


def test_bad_node_does_not_erase_healthy_sibling():
    wire = bundle()
    wire["nodes"] += [{"node_id": "bad", "observed_at": utcnow().isoformat(), "output": "broken", "error_code": None, "message": None}]
    result = parse_server_script_snapshot(json.dumps(wire), endpoint_id="endpoint-a", observed_at=utcnow())
    assert len(result.nodes) == 1
    assert result.failures[0].node_id == "bad"


def test_nodes_with_same_gpu_index_and_pid_are_isolated(service, admin):
    now = utcnow() - timedelta(seconds=10)
    first = observation(count=1, observed_at=now, gpu_uuids=["GPU-a"], processes=[process_for_gpu("GPU-a", pid=17)]).nodes[0]
    second = observation(count=1, observed_at=now, gpu_uuids=["GPU-b"], processes=[process_for_gpu("GPU-b", pid=17)]).nodes[0]
    first.node_id, second.node_id = "n1", "n2"
    service.ingest_observation(EndpointObservation(endpoint_id="endpoint-a", observed_at=now, nodes=[first, second]))
    update = first.model_copy(update={"observed_at": utcnow(), "processes": []})
    service.ingest_observation(EndpointObservation(endpoint_id="endpoint-a", observed_at=utcnow(), nodes=[update],
        failures=[{"node_id": "n2", "error_code": "auth_failed", "message": "SSH rejected"}]))
    data = service.snapshot(admin)["data"]
    cards = {gpu["node_id"]: gpu for gpu in data["gpus"]}
    assert len(cards) == 2
    assert cards["n1"]["gpu_index"] == cards["n2"]["gpu_index"] == 0
    assert cards["n2"]["present"] is True
    assert cards["n2"]["processes"][0]["pid"] == 17
    nodes = {node["node_id"]: node for node in data["endpoints"][0]["resource_nodes"]}
    assert nodes["n1"]["fresh"]
    assert not nodes["n2"]["fresh"]
    assert nodes["n2"]["usage"]["gpu_utilization_pct"] is None


def test_shared_summary_and_cpu_reset(service, admin):
    first = observation(count=2, observed_at=utcnow() - timedelta(seconds=10),
                        host={"cpu_total_ticks": 1000, "cpu_idle_ticks": 500})
    first.nodes[0].gpus[0].gpu_utilization_pct = 20
    first.nodes[0].gpus[1].gpu_utilization_pct = 60
    service.ingest_observation(first)
    second = deepcopy(first)
    second.observed_at = second.nodes[0].observed_at = utcnow()
    second.nodes[0].host.cpu_total_ticks = 1200
    second.nodes[0].host.cpu_idle_ticks = 550
    service.ingest_observation(second)
    node = service.snapshot(admin)["data"]["endpoints"][0]["resource_nodes"][0]
    assert node["usage"]["gpu_utilization_pct"] == 40
    assert node["usage"]["cpu_utilization_pct"] == 75
    second.nodes[0].boot_id = "restarted"
    service.ingest_observation(second)
    node = service.snapshot(admin)["data"]["endpoints"][0]["resource_nodes"][0]
    assert node["usage"]["cpu_utilization_pct"] is None


def test_standalone_plugin_embeds_the_shared_probe():
    import ast
    from pathlib import Path

    from serverpilot.observation_probe import RAW_SSH_COMBINED_QUERY

    tree = ast.parse((Path(__file__).parents[1] / "src/serverpilot/bundled_plugins/slurm-immediate").read_text(encoding="utf-8"))
    probe = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "NODE_PROBE" for target in node.targets))
    assert probe == RAW_SSH_COMBINED_QUERY


def test_idle_plugin_gpu_is_observable_but_requires_scheduler(service, admin, monkeypatch):
    from pathlib import Path

    from serverpilot.models import Endpoint
    from serverpilot.plugins import PluginInfo

    plugin = PluginInfo(plugin_id="delegated", display_name="Delegated", schema_version=5,
                        capabilities=("observe", "apply", "release"), path=Path("/unused"), source="local",
                        release_granularity="allocation", entry_kind="allocation",
                        limits={"lease_ends": "hard_kill_at_time_limit", "max_lease_seconds": 86400,
                                "apply_max_seconds": 40, "queues": False})
    monkeypatch.setattr("serverpilot.plugins.get_plugin", lambda name, **_: plugin if name == "delegated" else None)
    service._write(lambda session: setattr(session.get(Endpoint, "endpoint-a"), "observation_profile", "delegated"))
    value = observation(count=1)
    value.nodes[0].node_id = "compute"
    service.ingest_observation(value)
    gpu = service.snapshot(admin)["data"]["gpus"][0]
    assert gpu["state"] == "SCHEDULER_MANAGED"
    assert gpu["publicly_available"] is False
    assert gpu["telemetry"]["gpu_utilization_pct"] == 0
