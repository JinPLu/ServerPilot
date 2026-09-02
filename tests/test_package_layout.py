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


def test_no_coroutine_touches_the_domain_or_a_plugin_directly() -> None:
    """Blocking work has exactly one way onto the event loop: off it.

    Every domain method is synchronous SQLite, and every plugin call -- the
    verbs and the discovery that finds them -- is a subprocess. Called straight
    from a coroutine either one stops the whole control plane for its duration,
    which is how a 404 came to take 1.7 seconds. Domain work goes through
    `service.in_domain`; anything that touches a plugin goes through
    `asyncio.to_thread`.
    """

    import ast
    from pathlib import Path

    offenders: list[str] = []
    # Discovery belongs here as much as the verbs do: resolving a profile forks
    # every candidate's `info` with a multi-second timeout, so a coroutine that
    # merely asks "is this a plugin?" stalls the loop exactly as hard as one
    # that runs the plugin. Leaving discovery out of this set is what let two
    # such calls sit on the collection and claim paths unnoticed.
    plugin_verbs = {
        "observe_plugin",
        "apply_plugin",
        "release_plugin",
        "get_plugin",
        "is_plugin_profile",
        "is_known_observation_profile",
        "discover_plugins",
        "discover_plugins_with_failures",
    }
    # `in_domain` is the boundary itself; `collector_interval_seconds` reads
    # loaded configuration; `shutdown` disposes the boundary and cannot use it.
    on_the_loop = {"in_domain", "collector_interval_seconds", "shutdown"}
    root = Path(__file__).resolve().parents[1] / "src" / "serverpilot"
    for path in (root / "api.py", root / "collector.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    value = func.value
                    if (
                        isinstance(value, ast.Name)
                        and value.id == "service"
                        and func.attr not in on_the_loop
                    ):
                        offenders.append(f"{path.name}:{node.lineno} service.{func.attr}()")
                elif isinstance(func, ast.Name) and func.id in plugin_verbs:
                    offenders.append(f"{path.name}:{node.lineno} {func.id}()")
    assert offenders == []
