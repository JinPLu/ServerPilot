from __future__ import annotations

from collections import Counter

from serverpilot.models import GPUDevice
from serverpilot.schemas import ResourceConstraints
from serverpilot.service import BrokerService


def fleet(free_by_endpoint: dict[str, int]) -> list[GPUDevice]:
    candidates: list[GPUDevice] = []
    for endpoint_id, count in free_by_endpoint.items():
        for index in range(count):
            candidates.append(
                GPUDevice(id=f"{endpoint_id}:{index}", endpoint_id=endpoint_id, gpu_index=index)
            )
    return candidates


def placement(free_by_endpoint: dict[str, int], gpu_count: int, **kwargs: object) -> dict[str, int]:
    constraints = ResourceConstraints(gpu_count=gpu_count, **kwargs)  # type: ignore[arg-type]
    selected = BrokerService._select_resources(fleet(free_by_endpoint), constraints)
    assert selected is not None
    return dict(Counter(gpu.endpoint_id for gpu in selected))


def test_one_card_does_not_break_the_last_eight_card_machine() -> None:
    assert placement({"big": 8, "small": 2}, 1) == {"small": 1}


def test_a_whole_machine_request_still_lands_on_the_machine_that_fits_it() -> None:
    assert placement({"big": 8, "small": 2}, 8) == {"big": 8}


def test_an_exact_fit_is_preferred_over_a_larger_free_block() -> None:
    assert placement({"big": 8, "small": 2}, 2) == {"small": 2}


def test_an_already_fragmented_host_is_consumed_before_an_intact_one() -> None:
    # Best fit reads free capacity, not machine size: spending the last card of
    # a partly used host protects the host that can still serve a whole gang.
    assert placement({"partial": 1, "intact": 8}, 1) == {"partial": 1}


def test_ties_resolve_deterministically_by_endpoint_id() -> None:
    assert placement({"b-host": 4, "a-host": 4}, 4) == {"a-host": 4}


def test_spanning_stays_possible_when_no_single_host_fits() -> None:
    assert placement({"x": 5, "y": 3, "z": 3}, 8) == {"x": 5, "y": 3}


def test_same_host_requires_one_host_and_still_best_fits() -> None:
    assert placement({"big": 8, "small": 2}, 1, same_host=True) == {"small": 1}


def test_same_host_refuses_when_no_single_host_fits() -> None:
    constraints = ResourceConstraints(gpu_count=8, same_host=True)
    assert BrokerService._select_resources(fleet({"x": 5, "y": 3}), constraints) is None


def test_spread_still_distributes_across_hosts() -> None:
    assert placement({"a": 4, "b": 4, "c": 4}, 3, placement="spread") == {"a": 1, "b": 1, "c": 1}


def test_one_node_topology_picks_the_same_host_as_same_host() -> None:
    # gpus_per_node with nodes=1 and same_host express the same thing, so they
    # must not resolve to different machines.
    fleet_shape = {"partial": 2, "intact": 8}
    assert placement(fleet_shape, 2, nodes=1, gpus_per_node=2) == placement(
        fleet_shape, 2, same_host=True
    )


def test_multi_node_topology_does_not_break_an_intact_machine() -> None:
    # Two nodes of four fit on the two smaller hosts; the eight-card machine
    # must survive for a request that needs all of it.
    assert placement({"a-small": 4, "b-small": 4, "c-intact": 8}, 8, nodes=2, gpus_per_node=4) == {
        "a-small": 4,
        "b-small": 4,
    }


def test_selection_within_a_host_follows_gpu_index() -> None:
    constraints = ResourceConstraints(gpu_count=3)
    selected = BrokerService._select_resources(fleet({"only": 6}), constraints)
    assert selected is not None
    assert [gpu.gpu_index for gpu in selected] == [0, 1, 2]


def test_spanning_takes_the_largest_blocks_first() -> None:
    assert placement({"a": 3, "b": 3, "c": 2}, 6) == {"a": 3, "b": 3}


def test_same_host_ignores_placement_because_one_host_is_one_host() -> None:
    fleet_shape = {"partial": 2, "intact": 8}
    assert placement(fleet_shape, 2, same_host=True, placement="spread") == placement(
        fleet_shape, 2, same_host=True, placement="pack"
    )
