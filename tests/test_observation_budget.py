"""The time budgets, the socket path, and the sealed option list.

These lock the three facts that the "every server shows 连接失败 at once"
failure was made of: one number serving as two budgets, a control path that
silently disabled multiplexing, and probes that shared a deadline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverpilot.adapters import (
    RAW_SSH_COMBINED_QUERY,
    control_socket_path,
    observation_ssh_argv,
)
from serverpilot.config import SSH_BUDGETS, EndpointConfig, default_control_dir, stale_after_seconds


def _endpoint(**overrides: object) -> EndpointConfig:
    values: dict[str, object] = {
        "id": "server-a",
        "host": "10.40.0.199",
        "port": 4580,
        "ssh_user": "root",
        "workspace_path": "/workspace",
    }
    values.update(overrides)
    return EndpointConfig(**values)  # type: ignore[arg-type]


def test_budget_ordering_holds() -> None:
    """Each budget must be larger than the one whose failure it has to survive.

    Read as a sentence: a probe is issued several times before a broken channel
    is even declared dead; the channel is declared dead and reopened before a
    probe can time out; and a probe times out before the host is called stale.
    Any inversion here reproduces the original bug, where a host that answered
    slowly was reported unreachable.
    """

    interval = 5
    assert interval < SSH_BUDGETS.master_dead_after_seconds
    assert SSH_BUDGETS.master_dead_after_seconds < SSH_BUDGETS.probe_deadline_seconds
    assert SSH_BUDGETS.probe_deadline_seconds < stale_after_seconds(interval)


def test_probe_deadline_is_the_sum_of_the_two_phases() -> None:
    """The whole-probe clock is strictly longer than the connect clock.

    This is what makes a wall-clock expiry unambiguous: ssh would already have
    exited with its own message inside the connect budget, so reaching our
    deadline always means the remote command is what did not finish.
    """

    assert SSH_BUDGETS.probe_deadline_seconds == (
        SSH_BUDGETS.connect_seconds + SSH_BUDGETS.command_seconds
    )
    assert SSH_BUDGETS.probe_deadline_seconds > SSH_BUDGETS.connect_seconds


@pytest.mark.parametrize("interval", [5, 10, 30])
def test_stale_after_leaves_room_for_one_whole_probe(interval: int) -> None:
    """A host is not late until a full cycle plus a full probe could have run."""

    assert stale_after_seconds(interval) == interval + SSH_BUDGETS.probe_deadline_seconds
    assert stale_after_seconds(interval) > interval + SSH_BUDGETS.connect_seconds


def test_control_path_stays_under_the_unix_socket_limit() -> None:
    """A path over 104 bytes makes multiplexing fail silently, not loudly.

    OpenSSH's own `%C` expands to 64 hex characters, which under the real data
    directory exceeds `sun_path` and would put every probe back on a full
    handshake with no error anyone would see.
    """

    path = control_socket_path(_endpoint(), default_control_dir())
    assert len(str(path)) < 104


def test_one_socket_per_connection_triple() -> None:
    """Two ports on one host are two connections, and must not share a channel."""

    control_dir = Path("/tmp/serverpilot-test-ssh")
    same = control_socket_path(_endpoint(), control_dir)
    assert control_socket_path(_endpoint(), control_dir) == same
    assert control_socket_path(_endpoint(port=6103), control_dir) != same
    assert control_socket_path(_endpoint(ssh_user="jinplu"), control_dir) != same
    assert control_socket_path(_endpoint(host="10.40.1.76"), control_dir) != same


def test_probe_argv_is_the_single_sealed_option_list() -> None:
    """Every option that decides how a probe fails is pinned by us, not the endpoint."""

    control_dir = Path("/tmp/serverpilot-test-ssh")
    argv = observation_ssh_argv(
        _endpoint(), control_dir=control_dir, remote_command=RAW_SSH_COMBINED_QUERY
    )
    options = {argv[index + 1] for index, item in enumerate(argv) if item == "-o"}

    assert argv[0] == "ssh"
    assert argv[-1] == RAW_SSH_COMBINED_QUERY
    assert argv[-2] == "root@10.40.0.199"
    assert "BatchMode=yes" in options
    assert "StrictHostKeyChecking=yes" in options
    assert f"ConnectTimeout={SSH_BUDGETS.connect_seconds}" in options
    # Multiplexing is the point of the change: without these three the probe
    # pays a full TCP handshake, key exchange and authentication every cycle.
    assert "ControlMaster=auto" in options
    assert f'ControlPath="{control_socket_path(_endpoint(), control_dir)}"' in options
    assert f"ControlPersist={SSH_BUDGETS.control_persist_seconds}" in options
    # A channel whose peer vanished has to die on its own before the next probe
    # reaches for it, or the probe inherits it and burns the command budget.
    assert f"ServerAliveInterval={SSH_BUDGETS.server_alive_interval_seconds}" in options
    assert f"ServerAliveCountMax={SSH_BUDGETS.server_alive_count_max}" in options
    assert "NumberOfPasswordPrompts=0" in options
    # Never prompt, never offer every key an agent holds.
    assert "IdentitiesOnly=yes" in options
    assert "PreferredAuthentications=publickey" in options


def test_control_path_is_quoted_because_the_real_one_contains_a_space() -> None:
    """ssh splits an option value on whitespace, and our data dir has a space.

    The real control directory lives under "Application Support". Passed
    unquoted, ssh rejects the option as "extra arguments at end of line" and
    every probe on the machine fails at once -- while reporting something that
    reads like an SSH or network fault.
    """

    argv = observation_ssh_argv(
        _endpoint(), control_dir=default_control_dir(), remote_command="echo"
    )
    control_option = next(
        argv[index + 1]
        for index, item in enumerate(argv)
        if item == "-o" and argv[index + 1].startswith("ControlPath=")
    )
    value = control_option.removeprefix("ControlPath=")
    assert value.startswith('"') and value.endswith('"')
    assert " " in value


def test_probe_argv_carries_the_endpoint_port_explicitly() -> None:
    """The port comes from the endpoint row, never from a matching ssh_config Host."""

    argv = observation_ssh_argv(
        _endpoint(port=6972), control_dir=Path("/tmp/x"), remote_command="echo"
    )
    assert "-p" in argv
    assert argv[argv.index("-p") + 1] == "6972"
