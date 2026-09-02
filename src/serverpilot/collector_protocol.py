"""The wire shape a plugin's ``observe`` must answer in.

This was once the protocol for a collector script installed on each observed
server, invoked over SSH. That path is gone: an endpoint is observed either by
the one built-in probe, which needs nothing on the remote host, or by a local
plugin. What survives is the JSON contract itself, because a plugin's
``observe`` output is validated against exactly this shape.
"""

from __future__ import annotations

SERVER_SCRIPT_SCHEMA_VERSION = 2

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
