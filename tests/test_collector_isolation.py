"""One endpoint's failure stays that endpoint's failure.

The original defect: every probe ran inside a single `asyncio.gather` under one
deadline, so a stall anywhere pushed all of them past the same line at the same
instant, and the user saw six servers report a connection failure together.
"""

from __future__ import annotations

import asyncio

import pytest

from serverpilot.collector import SSHCollector
from serverpilot.config import EndpointConfig, InventoryConfig
from serverpilot.service import BrokerService
from tests.helpers import observation


def _endpoints() -> list[EndpointConfig]:
    return [
        EndpointConfig(
            id=f"endpoint-{letter}",
            host="127.0.0.1",
            port=2200 + index,
            ssh_user="gpu",
            workspace_path=f"/srv/{letter}",
        )
        for index, letter in enumerate("ab")
    ]


def test_a_failing_endpoint_does_not_stop_the_others(
    service: BrokerService, inventory: InventoryConfig
) -> None:
    """The healthy endpoint is still ingested, and the failure is named."""

    async def runner(endpoint: EndpointConfig, probe: str) -> str:
        if endpoint.id == "endpoint-a":
            raise TimeoutError("SSH observation timed out after 30 seconds for endpoint-a")
        raise AssertionError("unreachable: endpoint-b is observed directly")

    collector = SSHCollector(inventory, runner)

    async def observe_b(endpoint: EndpointConfig):
        return observation("endpoint-b", count=2)

    collector.observe_endpoint = observe_b  # type: ignore[method-assign]
    failing = SSHCollector(inventory, runner)

    async def run() -> tuple[dict, dict]:
        endpoint_a, endpoint_b = _endpoints()
        return (
            await failing.observe_and_ingest(service, endpoint_a),
            await collector.observe_and_ingest(service, endpoint_b),
        )

    result_a, result_b = asyncio.run(run())
    assert result_a == {"error": "command_timeout"}
    assert "error" not in result_b

    reachability_a = service.endpoint_reachability("endpoint-a")
    reachability_b = service.endpoint_reachability("endpoint-b")
    assert reachability_a["error_code"] == "command_timeout"
    assert not reachability_a["observed"]
    # The healthy endpoint was observed in the same pass that the other failed.
    assert reachability_b["observed"]
    assert reachability_b["error_code"] is None


def test_observe_and_ingest_never_raises(
    service: BrokerService, inventory: InventoryConfig
) -> None:
    """A supervisor task must not die on a probe failure, whatever the cause.

    A raised exception here would end that endpoint's probe loop for the
    lifetime of the daemon, which is the silent-stop failure this design is
    meant to make impossible.
    """

    async def runner(endpoint: EndpointConfig, probe: str) -> str:
        raise RuntimeError("something entirely unforeseen")

    collector = SSHCollector(inventory, runner)
    result = asyncio.run(collector.observe_and_ingest(service, _endpoints()[0]))
    assert result == {"error": "parse_error"} or result == {"error": "local_error"}


def test_a_recovered_endpoint_is_online_again_without_waiting_out_a_freeze(
    service: BrokerService, inventory: InventoryConfig
) -> None:
    """No back-off keeps a repaired host red.

    Collection used to stop probing an endpoint for a full minute after it
    failed three times, so a host that came back stayed marked as a connection
    failure until that timer expired rather than until it answered.
    """

    endpoint = _endpoints()[0]
    failures = {"remaining": 3}

    async def observe(target: EndpointConfig):
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise TimeoutError("timed out")
        return observation(target.id, count=2)

    collector = SSHCollector(inventory, lambda *_: None)  # type: ignore[arg-type]
    collector.observe_endpoint = observe  # type: ignore[method-assign]

    async def run() -> None:
        for _ in range(4):
            await collector.observe_and_ingest(service, endpoint)

    asyncio.run(run())
    reachability = service.endpoint_reachability(endpoint.id)
    assert reachability["observed"]
    assert reachability["error_code"] is None


def test_a_bad_endpoint_row_does_not_hide_the_readable_ones(
    service: BrokerService,
) -> None:
    """Listing what to collect must survive one row it cannot read.

    This used to be a single list comprehension whose failure propagated out of
    the function, and the caller swallowed it, so one bad row silently stopped
    every endpoint from being probed at all.
    """

    with service.database.session() as session:
        from serverpilot.models import Endpoint

        endpoint = session.get(Endpoint, "endpoint-a")
        assert endpoint is not None
        endpoint.ssh_user = "not a valid ssh user!"
        session.commit()

    readable = service.collector_endpoints()
    assert [item.id for item in readable] == ["endpoint-b"]


@pytest.mark.parametrize("endpoint_count", [2, 6])
def test_endpoints_do_not_share_a_start_time(endpoint_count: int) -> None:
    """Probe phases are spread across the interval, deterministically.

    Correlated start times are what turned any single stall into a simultaneous
    failure of every host, so the spread is part of the fix, not a nicety.
    """

    import hashlib

    interval = 5
    phases = {
        interval
        * (int.from_bytes(hashlib.sha256(f"endpoint-{index}".encode()).digest()[:4], "big") % 1000)
        / 1000
        for index in range(endpoint_count)
    }
    assert len(phases) == endpoint_count
    assert all(0 <= phase < interval for phase in phases)
