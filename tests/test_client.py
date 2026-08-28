from __future__ import annotations

import httpx
import pytest

from serverpilot.client import (
    CONTROL_PLANE_CLAIM_TIMEOUT_MAX_SECONDS,
    CONTROL_PLANE_READ_TIMEOUT_SECONDS,
    BrokerClient,
    BrokerClientError,
    control_plane_claim_timeout,
    control_plane_request_timeout,
)


def test_client_returns_first_gateway_error_without_retry(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def request(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((args, kwargs))
        return httpx.Response(
            502,
            json={"error": {"code": "gateway_unavailable", "message": "temporarily unavailable"}},
        )

    monkeypatch.setattr("serverpilot.client.httpx.request", request)

    with pytest.raises(
        BrokerClientError,
        match="broker HTTP 502: gateway_unavailable: temporarily unavailable",
    ):
        BrokerClient("http://127.0.0.1:8787").get("/api/v1/snapshot")
    assert len(calls) == 1
    assert all(call[1]["trust_env"] is False for call in calls)


def test_client_patch_sends_endpoint_update_over_rest(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, url, kwargs))
        return httpx.Response(200, json={"schema_version": "v1", "endpoint": {"id": "server-a"}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    response = BrokerClient("http://127.0.0.1:8787", actor="endpoint-admin").patch(
        "/api/v1/endpoints/server-a",
        {"ssh_user": "gpu"},
        idempotency_key="endpoint-update-key",
    )

    assert response["endpoint"]["id"] == "server-a"
    assert calls == [
        (
            "PATCH",
            "http://127.0.0.1:8787/api/v1/endpoints/server-a",
            {
                "headers": {
                    "X-ServerPilot-Actor": "endpoint-admin",
                    "Idempotency-Key": "endpoint-update-key",
                },
                "json": {"ssh_user": "gpu"},
                "params": None,
                "timeout": CONTROL_PLANE_READ_TIMEOUT_SECONDS,
                "trust_env": False,
            },
        )
    ]


def _state(revision: int, current: dict | None = None) -> dict:
    return {
        "schema_version": "v1",
        "snapshot_revision": revision,
        "server_time": "2026-08-06T00:00:00Z",
        "data": {"current": current or {"gpus": [], "leases": [], "requests": []}, "history": {}},
    }


def test_control_plane_state_waits_for_minimum_revision(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(4)),
            httpx.Response(200, json=_state(6)),
        ]
    )
    paths = []
    sleeps = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        paths.append((method, url, kwargs.get("params")))
        return next(responses)

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    monkeypatch.setattr("serverpilot.client.time.sleep", lambda seconds: sleeps.append(seconds))

    result = BrokerClient("http://127.0.0.1:8787").control_plane_state(
        minimum_snapshot_revision=5,
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    assert result["snapshot_revision"] == 6
    assert paths == [
        ("GET", "http://127.0.0.1:8787/api/v1/state", None),
        ("GET", "http://127.0.0.1:8787/api/v1/state", None),
    ]
    assert sleeps == [pytest.approx(0.1)]


def test_control_plane_state_rejects_revision_rollback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(7)),
            httpx.Response(200, json=_state(6)),
        ]
    )

    def request(*args, **kwargs):  # type: ignore[no-untyped-def]
        return next(responses)

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient("http://127.0.0.1:8787")

    assert client.control_plane_state()["snapshot_revision"] == 7
    with pytest.raises(BrokerClientError, match="rolled back"):
        client.control_plane_state()


def test_control_plane_state_retains_observed_revision_after_minimum_timeout(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    responses = iter(
        [
            httpx.Response(200, json=_state(9)),
            httpx.Response(200, json=_state(8)),
        ]
    )

    monkeypatch.setattr(
        "serverpilot.client.httpx.request",
        lambda *args, **kwargs: next(responses),
    )
    client = BrokerClient("http://127.0.0.1:8787")

    with pytest.raises(BrokerClientError, match="below required 10"):
        client.control_plane_state(minimum_snapshot_revision=10, timeout_seconds=0)
    with pytest.raises(BrokerClientError, match="rolled back from 9 to 8"):
        client.control_plane_state()


def test_operational_read_aliases_project_from_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    current = {
        "endpoints": [{"id": "server-a"}],
        "gpus": [
            {"id": "gpu-a", "endpoint_id": "server-a", "state": "AVAILABLE", "processes": []},
            {"id": "gpu-b", "endpoint_id": "server-b", "state": "HELD", "processes": []},
        ],
        "leases": [{"id": "lease-a", "project_id": "project-a"}],
        "requests": [{"id": "req-a", "state": "QUEUED"}, {"id": "req-b", "state": "LEASED"}],
        "reservations": [],
        "host_capacity": [{"endpoint": {"id": "server-a"}, "admission_state": "available"}],
    }
    calls = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, url))
        envelope = {
            "schema_version": "v1",
            "snapshot_revision": 11,
            "server_time": "2026-08-06T00:00:00Z",
        }
        if url.endswith("/api/v1/gpus"):
            return httpx.Response(
                200,
                json={**envelope, "data": [
                    {"id": "gpu-a", "endpoint_id": "server-a", "state": "AVAILABLE"}
                ]},
            )
        if url.endswith("/api/v1/endpoints"):
            return httpx.Response(200, json={**envelope, "data": current["endpoints"]})
        if url.endswith("/api/v1/requests"):
            return httpx.Response(200, json={**envelope, "data": [current["requests"][0]]})
        raise AssertionError(url)

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient("http://127.0.0.1:8787")

    assert client.endpoints()["data"] == [{"id": "server-a"}]
    assert client.gpus(only_available=True, compact=True)["data"] == [
        {"id": "gpu-a", "endpoint_id": "server-a", "state": "AVAILABLE"}
    ]
    assert client.requests(queued_only=True)["data"] == [{"id": "req-a", "state": "QUEUED"}]
    assert calls.count(("GET", "http://127.0.0.1:8787/api/v1/gpus")) == 1
    assert all(not url.endswith("/api/v1/state") for _method, url in calls)


def test_control_plane_claim_timeout_scales_with_gpu_count() -> None:
    assert control_plane_claim_timeout(1) == CONTROL_PLANE_READ_TIMEOUT_SECONDS
    assert control_plane_claim_timeout(8) == 120.0
    assert control_plane_claim_timeout(16) == CONTROL_PLANE_CLAIM_TIMEOUT_MAX_SECONDS
    assert control_plane_request_timeout("/api/v1/snapshot") == CONTROL_PLANE_READ_TIMEOUT_SECONDS
    assert (
        control_plane_request_timeout(
            "/api/v1/routine/claims",
            {"constraints": {"gpu_count": 8}},
        )
        == 120.0
    )
    assert (
        control_plane_request_timeout(
            "/api/v1/claims",
            {"constraints": {"gpu_count": 1}},
        )
        == CONTROL_PLANE_READ_TIMEOUT_SECONDS
    )


def test_broker_client_claim_uses_scaled_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls = []

    def request(method, url, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((method, url, kwargs["timeout"]))
        return httpx.Response(200, json={"schema_version": "v1", "data": {}})

    monkeypatch.setattr("serverpilot.client.httpx.request", request)
    client = BrokerClient("http://127.0.0.1:8787")
    client.get("/api/v1/snapshot")
    client.post(
        "/api/v1/routine/claims",
        {"constraints": {"gpu_count": 8}},
        idempotency_key="claim-eight",
    )

    assert calls == [
        ("GET", "http://127.0.0.1:8787/api/v1/snapshot", CONTROL_PLANE_READ_TIMEOUT_SECONDS),
        ("POST", "http://127.0.0.1:8787/api/v1/routine/claims", 120.0),
    ]
