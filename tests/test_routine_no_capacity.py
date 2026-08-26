"""`no_capacity` is a documented outcome, so an agent must read it as data.

Returning it as a tool error string forces every caller to parse the message,
and makes a full cluster indistinguishable from a broken transport.
"""

from __future__ import annotations

import pytest

from serverpilot import mcp_server
from serverpilot.client import BrokerClientError
from tests.helpers import tools


class RefusingClient:
    def __init__(self, error: BrokerClientError) -> None:
        self.error = error
        self.calls = 0

    def post(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        raise self.error


def _install(monkeypatch: pytest.MonkeyPatch, error: BrokerClientError) -> RefusingClient:
    client = RefusingClient(error)
    monkeypatch.setattr(mcp_server, "_routine_client", lambda: client)
    return client


def test_a_full_cluster_comes_back_as_a_result_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(
        monkeypatch,
        BrokerClientError(
            "broker HTTP 409: no_capacity: 当前没有满足本次申请的可用 GPU；本次申请未排队。",
            code="no_capacity",
            status_code=409,
        ),
    )

    result = tools.gpu_apply(gpu_count=8, task="probe")

    assert set(result) == {"no_capacity"}
    assert result["no_capacity"]["gpu_count"] == 8
    assert result["no_capacity"]["server_id"] is None
    assert "GPU" in result["no_capacity"]["message"]
    # It is an answer, not a transport hiccup, so it is never retried.
    assert client.calls == 1


def test_the_narrowed_server_is_echoed_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        BrokerClientError("broker HTTP 409: no_capacity: none", code="no_capacity"),
    )

    result = tools.gpu_apply(server_id="server-a", gpu_count=2, task="probe")

    assert result["no_capacity"]["server_id"] == "server-a"
    assert result["no_capacity"]["gpu_count"] == 2


def test_other_broker_errors_still_surface_as_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        BrokerClientError(
            "broker HTTP 422: validation_error: constraints are invalid",
            code="validation_error",
            status_code=422,
        ),
    )

    with pytest.raises(BrokerClientError):
        tools.gpu_apply(gpu_count=1, task="probe")


def test_a_transport_failure_is_retried_once_with_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _install(monkeypatch, BrokerClientError("broker request failed: ReadTimeout"))

    with pytest.raises(BrokerClientError):
        tools.gpu_apply(gpu_count=1, task="probe")

    assert client.calls == 2


def test_a_full_cluster_found_on_the_retry_is_still_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The contract is "no_capacity is an answer", so it has to hold on the leg
    # reached after a transport retry too, not only on the first call.
    errors = [
        BrokerClientError("broker request failed: ReadTimeout"),
        BrokerClientError("broker HTTP 409: no_capacity: none", code="no_capacity"),
    ]

    class Retrying:
        def __init__(self) -> None:
            self.calls = 0

        def post(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            raise errors[self.calls - 1]

    client = Retrying()
    monkeypatch.setattr(mcp_server, "_routine_client", lambda: client)

    result = tools.gpu_apply(gpu_count=1, task="probe")

    assert set(result) == {"no_capacity"}
    assert client.calls == 2


def test_releasing_an_already_settled_lease_confirms_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # gpu_release declares idempotentHint, and the agent contract tells a caller
    # to release and confirm. A second confirmation must not read as a failure.
    _install(
        monkeypatch,
        BrokerClientError(
            "broker HTTP 409: lease_already_released: already settled",
            code="lease_already_released",
        ),
    )

    result = tools.gpu_release(lease_id="lease-a")

    assert result == {"released": True, "lease_id": "lease-a", "state": "RELEASED"}
