"""Shared constants for the sealed remote collector protocol.

The broker always invokes this exact entry point.  A target administrator may
implement the command locally (for example to reach a containerized
``nvidia-smi``), but cannot influence its path or arguments through endpoint
configuration.
"""

from __future__ import annotations

SERVER_SCRIPT_SCHEMA_VERSION = 2
SERVER_SCRIPT_ENTRYPOINT = "serverpilot-collect"
SERVER_SCRIPT_REMOTE_COMMAND = (
    f"{SERVER_SCRIPT_ENTRYPOINT} --schema-version {SERVER_SCRIPT_SCHEMA_VERSION}"
)

# Optional snapshot field. Schema admission stays on ``schema_version``; a
# missing implementation version must not reject an otherwise valid collector.
_implementation_versions: dict[int, str] = {}


def remember_collector_implementation_version(
    observation: object, version: str | None
) -> None:
    """Attach a parsed collector package version to one observation object."""

    key = id(observation)
    if version is None:
        _implementation_versions.pop(key, None)
        return
    _implementation_versions[key] = version


def take_collector_implementation_version(observation: object) -> str | None:
    """Return and forget the version remembered for ``observation``."""

    return _implementation_versions.pop(id(observation), None)
