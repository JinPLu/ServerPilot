"""SQLAlchemy persistence schema. All mutable state is owned by the control plane."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


LeaseKind = Literal["workload", "keepalive"]
KeepalivePolicy = Literal["disabled", "idle_keepalive"]


class Revision(Base):
    __tablename__ = "revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RuntimeSetting(Base):
    """Knobs a user changed, and nothing else.

    One row per setting the app or the CLI can change and the control plane has
    to remember across a restart. Derived facts about an endpoint belong in
    ``endpoint_observation_facts``; putting them here behind key prefixes meant
    a query for "what has the user changed" had to know which prefixes to skip.
    """

    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EndpointObservationFact(Base):
    """What observing one endpoint produced, cached until the next observation.

    These are not settings.  Nobody chose them, nothing is lost by dropping a
    row but one round trip to the endpoint, and they answer questions the
    endpoint answers.  They used to live in ``runtime_settings`` behind two key
    prefixes, which made that table three stores at once and forced a hashed
    endpoint id to stand in for the foreign key that belongs here.
    """

    __tablename__ = "endpoint_observation_facts"

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    # Allocatable capacity a delegated cluster reported for itself, as the JSON
    # the plugin contract validates. NULL when the profile does not delegate.
    plugin_capacity_json: Mapped[str | None] = mapped_column(Text)
    # The ServerPilot version of the collector installed on the endpoint, which
    # `doctor` compares against this process. NULL until one reports.
    collector_version: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ServerGroup(Base):
    """A first-class scheduling and workspace boundary for one or more endpoints.

    ``storage_group`` on endpoints remains untouched legacy metadata and is
    never treated as a ServerGroup.
    """

    __tablename__ = "server_groups"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(2000), nullable=False)
    # Plain text only. Never passed to collector, plugins, keepalive, or env.
    environment_notes: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (
        UniqueConstraint("host", "port", name="uq_endpoint_host_port"),
        CheckConstraint("port >= 1 AND port <= 65535", name="ck_endpoint_port"),
        CheckConstraint(
            "keepalive_adapter_id IS NULL OR keepalive_adapter_id = 'server-script-v1'",
            name="ck_endpoint_keepalive_adapter",
        ),
        CheckConstraint(
            "keepalive_policy IN ('disabled', 'idle_keepalive')",
            name="ck_endpoint_keepalive_policy",
        ),
        CheckConstraint(
            "resource_kind IN ('unknown', 'cpu_only', 'gpu')",
            name="ck_endpoint_resource_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(64), nullable=False)
    ssh_alias: Mapped[str | None] = mapped_column(String(120))
    # Remote project/work directory for humans and Agents.  This is metadata
    # only; it never authorizes ServerPilot to launch a workload there.
    workspace_path: Mapped[str | None] = mapped_column(String(2000))
    observation_profile: Mapped[str] = mapped_column(
        String(40), nullable=False, default="linux"
    )
    keepalive_adapter_id: Mapped[str | None] = mapped_column(String(40))
    # This is desired state only. Per-GPU keepalive ownership lives in leases;
    # policy changes never silently start, stop, or reclassify a remote worker.
    keepalive_policy: Mapped[KeepalivePolicy] = mapped_column(
        String(32), nullable=False, default="disabled"
    )
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    storage_group: Mapped[str | None] = mapped_column(String(120))
    # Membership is enforced in the service. The ORM column is unscoped so the
    # initial revision can create endpoints before server_groups exists.
    server_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    expected_gpu_count: Mapped[int | None] = mapped_column(Integer)
    expected_gpu_total_vram_mib: Mapped[int | None] = mapped_column(Integer)
    # Collector-owned hardware classification. It is never supplied by a
    # caller, so a CPU-only host cannot be advertised as a GPU node by mistake.
    resource_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    # Optional project attribution for reporting. Endpoint lifecycle operations
    # are shared loopback inventory actions and are not permission-gated by it.
    owner_project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EndpointDeletion(Base):
    """Tombstone that keeps inventory YAML from resurrecting a deleted endpoint."""

    __tablename__ = "endpoint_deletions"

    endpoint_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    host: Mapped[str] = mapped_column(String(253), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)



class EndpointTelemetryCurrent(Base):
    """Latest host-wide CPU and memory observation for one endpoint."""

    __tablename__ = "endpoint_telemetry_current"

    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    load_1m: Mapped[float] = mapped_column(nullable=False)
    cpu_total_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_idle_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_usage_usec: Mapped[int | None] = mapped_column(BigInteger)
    cpu_quota_usec: Mapped[int | None] = mapped_column(BigInteger)
    cpu_period_usec: Mapped[int | None] = mapped_column(Integer)
    cpu_utilization_pct: Mapped[float | None] = mapped_column()
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_available_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_limit_mib: Mapped[int | None] = mapped_column(Integer)
    memory_current_mib: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class EndpointTelemetrySnapshot(Base):
    """Bounded history of host-wide CPU and memory observations."""

    __tablename__ = "endpoint_telemetry_snapshots"
    __table_args__ = (
        Index("ix_endpoint_telemetry_endpoint_observed", "endpoint_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cpu_count: Mapped[int] = mapped_column(Integer, nullable=False)
    load_1m: Mapped[float] = mapped_column(nullable=False)
    cpu_total_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_idle_ticks: Mapped[int | None] = mapped_column(Integer)
    cpu_usage_usec: Mapped[int | None] = mapped_column(BigInteger)
    cpu_quota_usec: Mapped[int | None] = mapped_column(BigInteger)
    cpu_period_usec: Mapped[int | None] = mapped_column(Integer)
    cpu_utilization_pct: Mapped[float | None] = mapped_column()
    memory_total_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_available_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_limit_mib: Mapped[int | None] = mapped_column(Integer)
    memory_current_mib: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class GPUDevice(Base):
    __tablename__ = "gpu_devices"
    __table_args__ = (UniqueConstraint("endpoint_id", "gpu_uuid", name="uq_endpoint_gpu_uuid"),)

    id: Mapped[str] = mapped_column(String(260), primary_key=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_uuid: Mapped[str] = mapped_column(String(160), nullable=False)
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    cuda_ordinal: Mapped[int | None] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    total_vram_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    compute_capability: Mapped[str | None] = mapped_column(String(40))
    labels_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="UNKNOWN")
    present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KeepaliveCurrent(Base):
    """Current worker state, separate from endpoint desired policy."""

    __tablename__ = "keepalive_current"
    __table_args__ = (
        CheckConstraint("actual IN ('ON', 'OFF', 'ERROR')", name="ck_keepalive_actual"),
    )

    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), primary_key=True
    )
    actual: Mapped[str] = mapped_column(String(16), nullable=False, default="OFF")
    error_reason: Mapped[str | None] = mapped_column(String(1000))
    expected_pid: Mapped[int | None] = mapped_column(Integer)
    expected_boot_id: Mapped[str | None] = mapped_column(String(120))
    expected_process_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TelemetrySnapshot(Base):
    __tablename__ = "telemetry_snapshots"
    __table_args__ = (Index("ix_telemetry_gpu_observed", "gpu_id", "observed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_used_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_free_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    memory_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    temperature_c: Mapped[int | None] = mapped_column(Integer)
    power_watts: Mapped[float | None] = mapped_column()
    pstate: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class TelemetryCurrent(Base):
    """Latest observation for one GPU; bounded to exactly one row per device."""

    __tablename__ = "telemetry_current"

    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), primary_key=True
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memory_used_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    memory_free_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    gpu_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    memory_utilization_pct: Mapped[int | None] = mapped_column(Integer)
    temperature_c: Mapped[int | None] = mapped_column(Integer)
    power_watts: Mapped[float | None] = mapped_column()
    pstate: Mapped[str | None] = mapped_column(String(32))
    health: Mapped[str] = mapped_column(String(32), nullable=False, default="OK")
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="raw-ssh")


class ProcessObservation(Base):
    __tablename__ = "process_observations"
    __table_args__ = (
        UniqueConstraint(
            "gpu_id", "pid", "boot_id", "process_started_at", name="uq_current_process_identity"
        ),
        Index("ix_process_gpu_current", "gpu_id", "last_seen_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    boot_id: Mapped[str] = mapped_column(String(120), nullable=False)
    process_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    username: Mapped[str | None] = mapped_column(String(120))
    executable: Mapped[str] = mapped_column(String(255), nullable=False)
    used_memory_mib: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observations: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # When the endpoint's own complete observations started leaving this
    # process out.  The clock runs only while the evidence chain is unbroken:
    # a failed or incomplete collection clears it, so an outage restarts the
    # absence rather than counting toward it.
    absent_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    quota_gpus: Mapped[int | None] = mapped_column(Integer)
    concurrency_limit: Mapped[int | None] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(160), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    coordination_uri: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ActorProject(Base):
    __tablename__ = "actor_projects"

    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (
        Index("ix_lease_state_expiry", "state", "expires_at"),
        CheckConstraint("kind IN ('workload', 'keepalive')", name="ck_lease_kind"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False, index=True)
    kind: Mapped[LeaseKind] = mapped_column(String(16), nullable=False, default="workload")
    #: What the claimant asked for. One claim is one lease, so the ask and the
    #: allocation it produced live in the same row.
    task_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(1000), nullable=False)
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False, default="HELD")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    release_reason: Mapped[str | None] = mapped_column(String(500))
    issued_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    #: When a fresh observation first showed no compute process on any of this
    #: lease's GPUs.  Cleared whenever a process appears or telemetry goes
    #: stale, so the elapsed time is always a fully observed idle window.
    idle_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaseResource(Base):
    __tablename__ = "lease_resources"
    __table_args__ = (
        UniqueConstraint("lease_id", "gpu_id", name="uq_lease_gpu"),
        Index(
            "uq_active_lease_resource_gpu",
            "gpu_id",
            unique=True,
            sqlite_where=text("active = 1"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_id: Mapped[str] = mapped_column(
        ForeignKey("gpu_devices.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When a fresh observation first showed no compute process on this GPU.
    #: Tracked per GPU so a claim that uses one card does not keep the rest.
    idle_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LeaseEndpointCommitment(Base):
    """Per-endpoint CPU and memory promise held for a direct GPU lease.

    A multi-host GPU lease may consume CPU and RAM at every selected endpoint.
    Persisting one row per endpoint makes that admission accounting explicit
    and keeps historical lease records intact after release.
    """

    __tablename__ = "lease_endpoint_commitments"
    __table_args__ = (
        UniqueConstraint("lease_id", "endpoint_id", name="uq_lease_endpoint_commitment"),
        Index("ix_endpoint_commitment_endpoint", "endpoint_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    endpoint_id: Mapped[str] = mapped_column(
        ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    cpu_cores: Mapped[float] = mapped_column(nullable=False, default=0.0)
    memory_mib: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkloadBinding(Base):
    __tablename__ = "workload_bindings"
    __table_args__ = (UniqueConstraint("lease_id", "run_id", name="uq_lease_run_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    lease_id: Mapped[str] = mapped_column(
        ForeignKey("leases.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(255), nullable=False)
    process_keys_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)




class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_actor_time", "actor_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str | None] = mapped_column(String(128), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(260), nullable=False)
    result: Mapped[str] = mapped_column(String(32), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    summary_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alert_active", "active", "severity", "last_seen_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(260), nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("actor_id", "action", "key", name="uq_idempotency"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("actors.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProviderState(Base):
    __tablename__ = "provider_states"
    __table_args__ = (UniqueConstraint("provider", "endpoint_id", name="uq_provider_endpoint"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    endpoint_id: Mapped[str | None] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    # One value from the collector's closed failure vocabulary. The free-text
    # ``last_error`` above stays for detail; only this is branched on.
    last_error_code: Mapped[str | None] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
