"""The agent surface over real REST: identity, recovery, resizing, refusals."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.helpers import observation, process_for_gpu


class Agent:
    def __init__(self, rest: TestClient, session_id: str, client: str = "claude-code") -> None:
        self.rest = rest
        self.headers = {"X-ServerPilot-Actor": client, "X-ServerPilot-Session": session_id}
        opened = rest.post("/api/v2/sessions", headers=self.headers, json={
            "session_id": session_id, "client": client, "project": "storyboard",
        })
        assert opened.status_code == 200, opened.text

    def post(self, path: str, body: dict, *, expect: int = 200) -> dict:
        response = self.rest.post(path, json=body, headers=self.headers)
        assert response.status_code == expect, response.text
        return response.json()

    def apply(self, task: str = "train", gpu_count: int = 1, **extra) -> dict:  # type: ignore[no-untyped-def]
        expect = extra.pop("expect", 200)
        return self.post("/api/v2/leases", {
            "project": "storyboard", "task": task, "duration_seconds": 3600,
            "gpu_count": gpu_count, **extra,
        }, expect=expect)


@pytest.fixture()
def rest(build_app):  # type: ignore[no-untyped-def]
    app = build_app("sessions")
    client = TestClient(app)
    client.service = app.state.service  # type: ignore[attr-defined]
    return client


def test_a_repeated_apply_is_the_same_lease(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation(count=2))
    agent = Agent(rest, "session-one")
    first = agent.apply()
    again = agent.apply()
    assert again["existing"] is True
    assert again["lease"]["id"] == first["lease"]["id"]
    refused = agent.apply(gpu_count=2, expect=409)
    assert refused["error"]["code"] == "task_already_held"


def test_a_new_session_cannot_touch_a_lease_until_it_takes_over(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation(count=2))
    old, new = Agent(rest, "session-old"), Agent(rest, "session-new")
    lease_id = old.apply()["lease"]["id"]

    assert new.post("/api/v2/leases/heartbeat", {"lease_id": lease_id}, expect=403)["error"]["code"] == "lease_forbidden"
    assert new.post(f"/api/v2/leases/{lease_id}/release", {}, expect=403)

    taken = new.post("/api/v2/leases/take-over", {
        "task": "train", "project": "storyboard", "reason": "旧会话崩溃",
    })
    assert taken["lease"]["id"] == lease_id
    assert new.post("/api/v2/leases/heartbeat", {"lease_id": lease_id})["changes"][-1]["action"] == "taken_over"
    assert old.post("/api/v2/leases/heartbeat", {"lease_id": lease_id}, expect=409)["error"]["code"] == "lease_taken_over"


def test_heartbeat_finds_the_lease_by_task_after_a_restart(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation(count=1))
    agent = Agent(rest, "session-one")
    lease_id = agent.apply()["lease"]["id"]
    view = agent.post("/api/v2/leases/heartbeat", {"task": "train", "project": "storyboard"})
    assert view["lease_id"] == lease_id
    assert len(str(view)) < 600


def test_cards_can_be_added_on_this_server_and_returned(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation(count=3))
    agent = Agent(rest, "session-one")
    lease = agent.apply()["lease"]
    grown = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"add_count": 2, "expected_epoch": 0})
    assert len(grown["added_gpu_ids"]) == 2
    assert grown["resource_epoch"] == 1
    assert grown["lease"]["resources"][0]["cuda_visible_devices"] == "0,1,2"

    stale = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"add_count": 1, "expected_epoch": 0}, expect=409)
    assert stale["error"]["code"] == "epoch_conflict"

    shrunk = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"remove_gpu_ids": grown["added_gpu_ids"][:1]})
    assert shrunk["resource_epoch"] == 2
    assert len(shrunk["lease"]["resources"][0]["cuda_visible_devices"].split(",")) == 2


def test_a_lease_can_reach_a_second_server_and_gives_both_entries(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation("endpoint-a", count=1))
    rest.service.ingest_observation(observation("endpoint-b", count=2))
    agent = Agent(rest, "session-one")
    lease = agent.apply(server_id="endpoint-a")["lease"]

    # By default a lease grows only where it already is.
    refused = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"add_count": 1}, expect=409)
    assert refused["error"]["code"] == "no_capacity"
    assert refused["error"]["details"]["alternatives"]

    grown = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"add_count": 2, "server_id": "endpoint-b"})
    hosts = {item["endpoint"]["id"] for item in grown["lease"]["resources"]}
    assert hosts == {"endpoint-a", "endpoint-b"}


def test_a_card_with_a_process_cannot_be_returned(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation(count=2))
    agent = Agent(rest, "session-one")
    lease = agent.apply(gpu_count=2)["lease"]
    gpu_id = lease["resources"][0]["gpus"][0]["id"]
    uuid = lease["resources"][0]["gpus"][0]["gpu_uuid"]
    rest.service.ingest_observation(observation(count=2, processes=[process_for_gpu(uuid)]))
    refused = agent.post(f"/api/v2/leases/{lease['id']}/resize", {"remove_gpu_ids": [gpu_id]}, expect=409)
    assert refused["error"]["code"] == "card_in_use"
    assert len(agent.post("/api/v2/leases/heartbeat", {"lease_id": lease["id"]})["changes"]) == 0


def test_no_capacity_says_what_to_do_next(rest) -> None:  # type: ignore[no-untyped-def]
    rest.service.ingest_observation(observation("endpoint-a", count=1))
    first, second = Agent(rest, "session-one"), Agent(rest, "session-two")
    first.apply(task="running", server_id="endpoint-a")
    details = second.apply(task="waiting", expect=409)["error"]["details"]
    assert details["reason"] == "cards_busy"
    assert details["needed"] == 1
    assert details["eta"] is not None
    assert details["eta_basis"] == "declared_durations"
    assert 54 <= details["retry_after_seconds"] <= 1980


def test_pulse_is_small_and_moves_with_the_state(rest) -> None:  # type: ignore[no-untyped-def]
    before = rest.get("/api/v2/pulse").json()
    rest.service.ingest_observation(observation(count=1))
    Agent(rest, "session-one").apply()
    after = rest.get("/api/v2/pulse").json()
    assert after["revision"] > before["revision"]
    assert len(rest.get("/api/v2/pulse").content) < 200
