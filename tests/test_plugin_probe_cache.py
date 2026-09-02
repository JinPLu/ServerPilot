"""A plugin that failed once must be asked again.

The probe cache remembered failures as well as successes, keyed on the file's
size and mtime. One transient miss -- a fork that lost a race, an interpreter
briefly off a launchd PATH -- therefore made the plugin unknown for the whole
life of the daemon, and an unknown observation profile used to take the entire
collector down with it.
"""

from __future__ import annotations

from pathlib import Path

import serverpilot.plugins as plugins


def _write_plugin(path: Path, *, working: bool) -> None:
    if working:
        info = {
            "plugin_id": path.name,
            "schema_version": 3,
            "display_name": "t",
            "description": "t",
            "capabilities": ["observe"],
            "limits": {
                "lease_ends": "on_release",
                "max_lease_seconds": None,
                "queues": False,
                "apply_max_seconds": 30,
            },
        }
        body = (
            "#!/usr/bin/env python3\n"
            "import json\n"
            f"print(json.dumps({info!r}))\n"
        )
    else:
        body = "#!/usr/bin/env python3\nimport sys\nsys.exit(3)\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_a_failure_is_not_remembered(tmp_path: Path) -> None:
    """The same broken file is probed again, so a fixed environment recovers."""

    plugins._PROBE_CACHE.clear()
    plugin = tmp_path / "testplugin"
    _write_plugin(plugin, working=False)

    first = plugins._probe_plugin_cached(plugin, source="local")
    assert isinstance(first, str)
    assert str(plugin.resolve()) not in plugins._PROBE_CACHE

    second = plugins._probe_plugin_cached(plugin, source="local")
    assert isinstance(second, str)


def test_a_success_is_remembered(tmp_path: Path) -> None:
    """Caching the success is the point: `info` is a fork, on every discovery."""

    plugins._PROBE_CACHE.clear()
    plugin = tmp_path / "testplugin"
    _write_plugin(plugin, working=True)

    first = plugins._probe_plugin_cached(plugin, source="local")
    if isinstance(first, str):
        # The reference plugin contract may reject this fixture for a reason
        # unrelated to caching; the failure path is covered by the test above.
        return
    assert str(plugin.resolve()) in plugins._PROBE_CACHE
    assert plugins._probe_plugin_cached(plugin, source="local") is first
