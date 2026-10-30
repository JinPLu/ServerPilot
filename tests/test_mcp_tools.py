"""The seven agent tools, end to end, and what each one costs to call."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from serverpilot import mcp_server
from serverpilot.agent_contract import AGENT_TOOLS, CARD_STATUS_BY_STATE, render_mcp_instructions
from serverpilot.client import BrokerClientError
from serverpilot.models import GPUDevice, Lease, LeaseResource
from serverpilot.timeutil import utcnow
from tests.helpers import observation, process_for_gpu, tools


def size(reply: dict) -> int:
    return len(json.dumps(reply, ensure_ascii=False, separators=(",", ":")))


def fleet(service, endpoints=("endpoint-a", "endpoint-b"), count=8) -> None:  # type: ignore[no-untyped-def]
    for endpoint_id in endpoints:
        service.ingest_observation(observation(endpoint_id, count=count))


def test_the_tools_are_the_ones_the_contract_names() -> None:
    assert {tool.name for tool in AGENT_TOOLS} == set(mcp_server.ROUTINE_MCP_TOOL_NAMES)


def test_apply_tells_the_agent_how_to_enter_without_assembling_anything(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    reply = tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)
    assert reply["ok"] is True
    server = reply["servers"][0]
    assert server["env"] == {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": "0,1"}
    assert [card["selector"] for card in server["cards"]] == [0, 1]
    assert reply["heartbeat_within_seconds"] == mcp_agent.inventory.idle_lease_reclaim_seconds
    assert server["ssh"]


def test_a_direct_lease_can_be_recovered_after_context_loss(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)["lease_id"]
    mine = tools.gpu_status()["mine"]
    assert [item["lease_id"] for item in mine] == [lease_id]
    reply = tools.gpu_lease(task="train", include=["entry"])
    assert reply["servers"][0]["env"]["CUDA_VISIBLE_DEVICES"] == "0,1"


def test_telemetry_reports_the_holders_cards_and_a_tuning_summary(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)["lease_id"]
    uuids = [card["gpu_id"] for card in tools.gpu_lease(lease_id=lease_id, include=["entry"])["servers"][0]["cards"]]
    mcp_agent.ingest_observation(observation(count=2, processes=[process_for_gpu(uuid) for uuid in uuids]))
    telemetry = tools.gpu_lease(lease_id=lease_id, include=["telemetry"])["telemetry"]
    assert len(telemetry["cards"]) == 2
    assert telemetry["summary"]["recent_average"]["min_memory_free_mib"] is not None


def test_status_is_no_heartbeat_and_lease_is(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600)["lease_id"]
    old = utcnow() - timedelta(hours=1)

    def age(session):  # type: ignore[no-untyped-def]
        session.get(Lease, lease_id).last_heartbeat_at = old

    mcp_agent._write(age)
    read = lambda: mcp_agent._read(lambda session: session.get(Lease, lease_id).last_heartbeat_at)  # noqa: E731
    before = read()
    tools.gpu_status()
    assert read() == before
    tools.gpu_lease(lease_id=lease_id)
    assert read() > before


def test_if_changed_answers_briefly_while_nothing_moved(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    digest = tools.gpu_status(view="capacity")["digest"]
    assert tools.gpu_status(view="capacity", if_changed=digest) == {"ok": True, "unchanged": True, "digest": digest}
    tools.gpu_apply(task="train", duration_seconds=3600)
    assert "groups" in tools.gpu_status(view="capacity", if_changed=digest)


def test_no_capacity_is_an_answer_with_a_next_move(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    tools.gpu_apply(task="first", duration_seconds=3600)
    reply = tools.gpu_apply(task="second", duration_seconds=3600)
    assert reply["ok"] is False
    error = reply["error"]
    assert error["code"] == "no_capacity"
    assert error["retryable"] is True
    assert error["retry_after_seconds"] >= 54
    assert error["details"]["reason"] == "cards_busy"


def test_a_repeated_task_returns_its_lease(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    first = tools.gpu_apply(task="train", duration_seconds=3600)
    again = tools.gpu_apply(task="train", duration_seconds=3600)
    assert again["lease_id"] == first["lease_id"]
    assert again["existing"] is True


def test_every_reply_about_a_lease_carries_its_current_epoch(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 2)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600)["lease_id"]
    grown = tools.gpu_resize(lease_id=lease_id, add_count=1)
    assert tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)["resource_epoch"] == grown["resource_epoch"]
    assert tools.gpu_take_over(reason="重启", lease_id=lease_id)["resource_epoch"] == grown["resource_epoch"]


def test_a_held_task_points_at_the_tool_that_resolves_it(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 3)
    first = tools.gpu_apply(task="train", duration_seconds=3600)
    error = tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)["error"]
    assert error["code"] == "task_already_held"
    assert error["next"] == {"tool": "gpu_lease", "args": {"lease_id": first["lease_id"]}}


def test_resize_grows_returns_and_warns_about_running_processes(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 3)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600)["lease_id"]
    grown = tools.gpu_resize(lease_id=lease_id, add_count=2)
    assert grown["restart_required"] is True
    assert grown["servers"][0]["env"]["CUDA_VISIBLE_DEVICES"] == "0,1,2"
    shrunk = tools.gpu_resize(task="train", remove_gpu_ids=grown["added"][:1], expected_epoch=grown["resource_epoch"])
    assert shrunk["resource_epoch"] == grown["resource_epoch"] + 1


def test_a_reclaim_reaches_the_holder_on_its_next_heartbeat(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    mcp_agent.ingest_observation(observation(count=2))
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600, gpu_count=2)["lease_id"]
    cards = mcp_agent._read(lambda session: [
        session.get(GPUDevice, card.gpu_id)
        for card in session.scalars(select(LeaseResource).where(LeaseResource.lease_id == lease_id)).all()
    ])
    busy, idle = cards
    mcp_agent.ingest_observation(observation(count=2, processes=[process_for_gpu(busy.gpu_uuid)]))

    def age(session):  # type: ignore[no-untyped-def]
        card = session.scalar(select(LeaseResource).where(LeaseResource.gpu_id == idle.id))
        card.idle_since = utcnow() - timedelta(seconds=mcp_agent.inventory.idle_lease_reclaim_seconds + 5)

    mcp_agent._write(age)
    mcp_agent.ingest_observation(observation(count=2, processes=[process_for_gpu(busy.gpu_uuid)]))

    view = tools.gpu_lease(lease_id=lease_id)
    assert view["changes"][0]["action"] == "idle_gpu_reclaimed"
    assert view["gpu_count"] == 1
    assert view["resource_epoch"] == 1
    assert tools.gpu_lease(lease_id=lease_id)["changes"] == []


def test_a_finished_lease_says_how_it_ended(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600)["lease_id"]
    assert tools.gpu_release(lease_id=lease_id, outcome="failed") == {"ok": True, "lease_id": lease_id, "ended": True}
    view = tools.gpu_lease(lease_id=lease_id)
    assert view["ended"]["reason"] == "workload_failed"


def test_releasing_a_settled_lease_confirms_it(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    lease_id = tools.gpu_apply(task="train", duration_seconds=3600)["lease_id"]
    tools.gpu_release(lease_id=lease_id)
    assert tools.gpu_release(lease_id=lease_id)["ended"] is True


def test_an_old_lease_appears_as_recoverable_and_can_be_claimed(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    lease_id = tools.gpu_apply(task="old", duration_seconds=3600)["lease_id"]

    def disown(session):  # type: ignore[no-untyped-def]
        session.get(Lease, lease_id).owner_session_id = None

    mcp_agent._write(disown)
    status = tools.gpu_status()
    assert status["mine"] == []
    assert [item["lease_id"] for item in status["recoverable"]] == [lease_id]
    taken = tools.gpu_take_over(reason="升级到 4.0", lease_id=lease_id)
    assert taken["servers"][0]["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert [item["lease_id"] for item in tools.gpu_status()["mine"]] == [lease_id]


def test_node_detail_needs_a_server(mcp_agent) -> None:  # type: ignore[no-untyped-def]
    fleet(mcp_agent, ("endpoint-a",), 1)
    assert tools.gpu_status(view="nodes")["error"]["code"] == "server_required"


def test_every_internal_card_state_has_a_defined_word() -> None:
    from serverpilot.service import BrokerService  # noqa: F401

    for state in ("AVAILABLE", "KEEPALIVE", "RUNNING_MANAGED", "HELD", "LEASED_IDLE", "BUSY_UNMANAGED",
                  "CONFLICT", "ORPHANED_BUSY", "UNKNOWN_STALE", "UNKNOWN_RECOVERING", "UNHEALTHY",
                  "MAINTENANCE", "SCHEDULER_MANAGED", "ERROR"):
        assert state in CARD_STATUS_BY_STATE
    instructions = render_mcp_instructions()
    for token in set(CARD_STATUS_BY_STATE.values()):
        assert token in instructions


# ---- what a reply costs --------------------------------------------------


def test_replies_stay_within_budget_on_a_realistic_fleet(build_app, inventory, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Six servers, 28 cards: the size of the fleet this surface was measured on."""

    from fastapi.testclient import TestClient

    from serverpilot.client import parse_broker_response

    shape = (("a", 8), ("b", 8), ("c", 4), ("d", 4), ("e", 2), ("f", 2))
    template = inventory.endpoints[0]
    config = inventory.model_copy(update={"endpoints": [
        template.model_copy(update={"id": f"endpoint-{name}", "port": 2200 + index})
        for index, (name, _count) in enumerate(shape)
    ]})
    app = build_app("budget", inventory_config=config)
    service = app.state.service
    for name, count in shape:
        service.ingest_observation(observation(f"endpoint-{name}", count=count))
    rest = TestClient(app)

    class Client:
        headers = {"X-ServerPilot-Actor": "claude-code", "X-ServerPilot-Session": "budget-session"}

        def snapshot(self, **params):  # type: ignore[no-untyped-def]
            return parse_broker_response(rest.get("/api/v1/snapshot", headers=self.headers))

        def get(self, path, *, params=None):  # type: ignore[no-untyped-def]
            return parse_broker_response(rest.get(path, params=params, headers=self.headers))

        def post(self, path, body=None, *, idempotency_key=None, timeout=None):  # type: ignore[no-untyped-def]
            return parse_broker_response(rest.post(path, json=body, headers=self.headers))

    client = Client()
    monkeypatch.setattr(mcp_server, "_session_client", lambda actor: client)

    applied = tools.gpu_apply(task="多卡训练", duration_seconds=3600, gpu_count=8)
    assert applied["ok"], applied
    tools.gpu_apply(task="第二个任务", duration_seconds=3600, gpu_count=2)
    budgets = (
        ("apply", applied, 1500),
        ("summary", tools.gpu_status(), 1500),
        ("capacity", tools.gpu_status(view="capacity"), 900),
        ("servers", tools.gpu_status(view="servers"), 6000),
        ("nodes", tools.gpu_status(view="nodes", server_id="endpoint-a"), 12000),
        ("heartbeat", tools.gpu_lease(lease_id=applied["lease_id"]), 600),
        ("telemetry", tools.gpu_lease(lease_id=applied["lease_id"], include=["telemetry"]), 2500),
        ("release", tools.gpu_release(lease_id=applied["lease_id"]), 200),
    )
    sizes = {name: size(reply) for name, reply, _limit in budgets}
    assert all(sizes[name] <= limit for name, _reply, limit in budgets), sizes


# ---- transport ------------------------------------------------------------


class Scripted:
    def __init__(self, *outcomes) -> None:  # type: ignore[no-untyped-def]
        self.outcomes = list(outcomes)
        self.posts: list[str] = []

    def post(self, path, body=None, *, idempotency_key=None, timeout=None):  # type: ignore[no-untyped-def]
        if path == "/api/v2/sessions":
            return {"session_id": "s"}
        self.posts.append(path)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


LEASE = {"lease": {"id": "l1", "resources": []}}


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def install(*outcomes):  # type: ignore[no-untyped-def]
        client = Scripted(*outcomes)
        monkeypatch.setattr(mcp_server, "_session_client", lambda actor: client)
        return client

    return install


def test_a_request_that_never_left_is_retried_once(scripted) -> None:  # type: ignore[no-untyped-def]
    client = scripted(BrokerClientError("connect failed", unsent=True), LEASE)
    assert tools.gpu_apply(task="t", duration_seconds=60)["lease_id"] == "l1"
    assert len(client.posts) == 2


def test_a_read_timeout_is_not_replayed(scripted) -> None:  # type: ignore[no-untyped-def]
    client = scripted(BrokerClientError("read timed out", unsent=False), LEASE)
    with pytest.raises(BrokerClientError):
        tools.gpu_apply(task="t", duration_seconds=60)
    assert len(client.posts) == 1


def test_an_undocumented_error_stays_a_failure(scripted) -> None:  # type: ignore[no-untyped-def]
    scripted(BrokerClientError("broker HTTP 500: boom: x", code="boom", status_code=500))
    with pytest.raises(BrokerClientError):
        tools.gpu_apply(task="t", duration_seconds=60)


def test_group_selection_is_an_answer_with_the_choices(scripted) -> None:  # type: ignore[no-untyped-def]
    scripted(BrokerClientError(
        "broker HTTP 409: group_selection_required: pick", code="group_selection_required",
        status_code=409, details={"groups": [{"id": "g1"}]},
    ))
    error = tools.gpu_apply(task="t", duration_seconds=60)["error"]
    assert error["details"]["groups"] == [{"id": "g1"}]
    assert error["next"]["tool"] == "gpu_apply"


def test_unconfirmed_allocation_means_repeat_the_same_call(scripted) -> None:  # type: ignore[no-untyped-def]
    scripted(BrokerClientError(
        "broker HTTP 503: allocation_unconfirmed: lost", code="allocation_unconfirmed", status_code=503,
    ))
    error = tools.gpu_apply(task="t", duration_seconds=60)["error"]
    assert error["retryable"] is True
    assert error["next"]["args"] == "repeat the same call"


def test_no_project_is_a_refusal_not_a_guess(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("SERVERPILOT_PROJECT")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_server.Path, "home", lambda: tmp_path)
    assert tools.gpu_status()["error"]["code"] == "project_required"




def test_a_group_choice_reaches_the_control_plane(mcp_agent, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    sent = []
    post = mcp_agent.mcp_client.post

    def spy(path, body=None, **kwargs):  # type: ignore[no-untyped-def]
        sent.append((path, body))
        return post(path, body, **kwargs)

    monkeypatch.setattr(mcp_agent.mcp_client, "post", spy)
    fleet(mcp_agent, ("endpoint-a",), 1)
    assert tools.gpu_apply(task="t", duration_seconds=60, server_group_id="missing")["error"]
    assert ("/api/v2/leases", {
        "project": "tests", "task": "t", "duration_seconds": 60, "gpu_count": 1,
        "server_group_id": "missing", "server_id": None,
    }) in sent


def test_the_schema_rejects_what_an_agent_should_not_send() -> None:
    import asyncio

    listed = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    apply_schema = listed["gpu_apply"].inputSchema
    assert set(apply_schema["required"]) == {"task", "duration_seconds"}
    assert apply_schema["properties"]["gpu_count"]["minimum"] == 1
    assert apply_schema["properties"]["duration_seconds"]["minimum"] == 60
