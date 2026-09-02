"""Every collection failure resolves to exactly one name.

Before this vocabulary existed the only record of a failure was a formatted
Python exception, and the desktop client re-derived a category from it by
matching English substrings against text the server had already seen.
"""

from __future__ import annotations

import pytest

from serverpilot.collector import CollectionError, ProbeFailed, classify_failure
from serverpilot.plugins import PluginError


def _ssh_failure(stderr: str, *, reached_remote: bool = False) -> ProbeFailed:
    return ProbeFailed(
        "SSH probe failed for server-a", returncode=255, stderr=stderr, reached_remote=reached_remote
    )


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("root@10.0.0.1: Permission denied (publickey).", "auth_failed"),
        ("Received disconnect: Too many authentication failures", "auth_failed"),
        ("Host key verification failed.", "host_key_rejected"),
        ("@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@", "host_key_rejected"),
        ("Unable to negotiate: no matching host key type found", "host_key_rejected"),
        ("ssh: Could not resolve hostname nowhere: nodename nor servname provided", "dns_failure"),
        ("ssh: connect to host 10.0.0.1 port 22: Connection refused", "connection_refused"),
        ("ssh: connect to host 10.0.0.1 port 22: No route to host", "network_unreachable"),
        ("ssh: connect to host 10.0.0.1 port 22: Network is unreachable", "network_unreachable"),
        ("ssh: connect to host 10.0.0.1 port 22: Operation timed out", "connect_timeout"),
        ("Connection closed by 10.0.0.1 port 22", "connection_reset"),
        ("some stderr nobody has seen before", "ssh_failed"),
    ],
)
def test_ssh_stderr_resolves_to_one_code(stderr: str, expected: str) -> None:
    assert classify_failure(_ssh_failure(stderr)) == expected


def test_reaching_the_remote_shell_outranks_the_exit_code() -> None:
    """255 is both ssh's own failure code and a legal remote exit code.

    The exit code alone therefore cannot tell "I never got there" from "the host
    answered badly". Seeing our own section marker in stdout proves the remote
    shell ran, and that is the discriminator.
    """

    assert (
        classify_failure(
            _ssh_failure("Permission denied (publickey).", reached_remote=True)
        )
        == "remote_error"
    )


def test_our_own_deadline_is_a_command_timeout() -> None:
    assert classify_failure(TimeoutError("SSH observation timed out")) == "command_timeout"


def test_a_plugin_failure_is_never_confused_with_ssh() -> None:
    assert classify_failure(PluginError("no live ControlMaster for hanhai22")) == "plugin_error"


def test_unreadable_output_is_a_parse_error() -> None:
    assert classify_failure(CollectionError("nvidia-smi GPU output has an invalid PCI bus ID")) == (
        "parse_error"
    )
    assert classify_failure(ValueError("SSH stdout is not valid UTF-8")) == "parse_error"


def test_anything_unforeseen_still_gets_a_name() -> None:
    """The vocabulary is closed, so an unexpected failure is named, not blank."""

    assert classify_failure(RuntimeError("something local broke")) == "local_error"
