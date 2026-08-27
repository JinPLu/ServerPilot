from __future__ import annotations

from importlib.resources import files

import pytest


@pytest.mark.parametrize(
    "resource",
    [
        "migrations/env.py",
        "migrations/script.py.mako",
        "migrations/versions/20260719_0003_endpoint_host_telemetry.py",
        "migrations/versions/20260719_0005_auto_activate_requests.py",
    ],
)
def test_runtime_package_resources_are_present(resource: str) -> None:
    assert files("serverpilot").joinpath(resource).is_file(), resource
