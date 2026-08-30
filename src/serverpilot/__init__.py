"""Global, cooperative GPU resource control plane."""

# The remote collector and keepalive entrypoints import this package from a
# plain source tree, where no distribution metadata exists. The literal is the
# single source; pyproject.toml reads it back out at build time.
__version__ = "2.3.0"
SCHEMA_VERSION = "v1"

# Lets an upgraded MCP fail clearly when the loopback service has not yet been
# restarted into the same release.
API_CAPABILITIES = (
    "instant_claims",
    "control_plane_state",
    "endpoint_telemetry_history",
    "telemetry_recent_averages",
    "endpoint_update",
    "endpoint_delete",
    "endpoint_keepalive",
    "endpoint_conflict_cleanup",
    "operator_lease_release",
    "operator_lease_reassignment",
    "cuda_ordinal_selectors",
    "keepalive_protocol_v3",
    "keepalive_worker_attestation",
    "collector_settings",
    "observation_profiles",
    "mcp_entry",
    "server_group_crud",
)
