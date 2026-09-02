"""How long each kind of record is kept.

One table. Retention used to be a property of exactly two tables that happened
to have a pruner, while the rest -- every audit event, every replay key, every
resolved alert -- grew for as long as the control plane ran. Nothing failed
because of it, which is why it went unnoticed: the cost was a database that
only ever got slower to open and back up.

A window here is a promise about what can still be read, so shortening one is a
user-visible change and belongs in the changelog.
"""

from __future__ import annotations

from typing import Final

HOUR: Final = 60 * 60
DAY: Final = 24 * HOUR

# Per-GPU and per-endpoint history behind the charts. The app's own history
# view asks for at most this window, so keeping more serves nobody.
TELEMETRY_SECONDS: Final = 1 * DAY

# A replay key exists to make one client's retry safe. Every caller that sends
# one has given up on that call long before this, so an older row can only be
# matched by a key nobody will send again.
IDEMPOTENCY_SECONDS: Final = 1 * DAY

# The operator-facing history of what was claimed, released and corrected.
# Long enough to explain last month's incident, not long enough to become the
# largest thing in the database.
AUDIT_SECONDS: Final = 30 * DAY

# An alert that is no longer active describes a condition that has already
# ended. It is kept briefly so a person who steps away still sees what happened.
RESOLVED_ALERT_SECONDS: Final = 7 * DAY

# Process sightings are evidence for the absence clock, which reasons in
# minutes. Anything this old is history nothing consults.
PROCESS_OBSERVATION_SECONDS: Final = 7 * DAY

# Reclaiming free pages costs a full rewrite of the file, so it is worth doing
# only when enough of the file is free to matter. Checked after each pass;
# in a steady state it is false and nothing happens.
VACUUM_FREE_BYTES: Final = 64 * 1024 * 1024
VACUUM_FREE_FRACTION: Final = 0.25
