"""A full cluster and a broken one must not look the same to an agent.

`apply` signals "nothing free right now" with a dedicated exit code. Anything
else -- a quota refusal, an unreachable scheduler, a plugin that crashed -- is a
failure the caller has to see, not an empty cluster it should wait out.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from serverpilot.plugins import (
    PLUGIN_NO_CAPACITY_EXIT_CODE,
    PluginError,
    invoke_plugin,
)

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a plugin is a POSIX executable found by its shebang and executable bit",
)


def _plugin(directory: Path, body: str) -> Path:
    path = directory / "probe-plug"
    path.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_dedicated_exit_code_means_nothing_was_free(tmp_path: Path) -> None:
    path = _plugin(
        tmp_path,
        f'sys.stderr.write("no immediate GPU capacity\\n")\nsys.exit({PLUGIN_NO_CAPACITY_EXIT_CODE})',
    )

    with pytest.raises(PluginError) as error:
        invoke_plugin(path, ["apply"], timeout_seconds=5)

    assert error.value.no_capacity is True
    assert "no immediate GPU capacity" in str(error.value)


def test_an_ordinary_failure_is_not_reported_as_an_empty_cluster(tmp_path: Path) -> None:
    # A quota refusal names the reason; it must reach the caller as a failure.
    path = _plugin(tmp_path, 'sys.stderr.write("QOSMaxGRESPerUser\\n")\nsys.exit(1)')

    with pytest.raises(PluginError) as error:
        invoke_plugin(path, ["apply"], timeout_seconds=5)

    assert error.value.no_capacity is False
    assert "QOSMaxGRESPerUser" in str(error.value)


def test_a_scheduler_message_alone_does_not_mean_no_capacity(tmp_path: Path) -> None:
    # The outcome used to be guessed from the scheduler's wording, so a log line
    # that merely mentioned an allocation failure was enough to report an empty
    # cluster. Only the exit code decides now.
    path = _plugin(
        tmp_path,
        'sys.stderr.write("srun: error: Unable to allocate resources: Invalid account\\n")\nsys.exit(1)',
    )

    with pytest.raises(PluginError) as error:
        invoke_plugin(path, ["apply"], timeout_seconds=5)

    assert error.value.no_capacity is False


def test_a_timeout_is_a_failure_not_an_empty_cluster(tmp_path: Path) -> None:
    path = _plugin(tmp_path, "import time\ntime.sleep(5)")

    with pytest.raises(PluginError) as error:
        invoke_plugin(path, ["apply"], timeout_seconds=0.2)

    assert error.value.no_capacity is False
    assert "timed out" in str(error.value)


def test_the_reference_plugin_reports_an_unconfigured_cluster_as_a_failure() -> None:
    # Being unconfigured is a fault in the operator's setup, not a full cluster.
    from serverpilot.plugins import bundled_plugin_dir

    path = bundled_plugin_dir() / "slurm-immediate"

    with pytest.raises(PluginError) as error:
        invoke_plugin(path, ["apply", "--gpu-count", "1", "--task-ref", "probe"], timeout_seconds=10)

    assert error.value.no_capacity is False
    assert "unconfigured" in str(error.value)
