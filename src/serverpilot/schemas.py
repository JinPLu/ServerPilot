"""External contracts. Unknown fields are rejected so admission is never guessed."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from serverpilot.config import (
    KeepaliveAdapterId,
    KeepalivePolicy,
    absolute_single_line_path,
    plain_text,
)

DEFAULT_LEASE_WINDOW_SECONDS = 8 * 60 * 60


def optional_absolute_workspace_path(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return absolute_single_line_path(stripped)


def resolve_stored_workspace_override(
    *,
    server_group_id: str | None,
    workspace_path: str | None,
    workspace_path_override: str | None,
    fields_set: set[str],
    require_ungrouped_path: bool,
) -> str | None:
    """Resolve the stored per-endpoint workspace override.

    Explicit ``workspace_path_override`` wins (null means inherit). Grouped
    callers that omit it may still pass a legacy ``workspace_path``. Ungrouped
    endpoints need a non-null stored override. Conflicting non-equal values fail.
    """

    override_explicit = "workspace_path_override" in fields_set
    legacy_explicit = "workspace_path" in fields_set
    override_value = workspace_path_override if override_explicit else None
    legacy_value = workspace_path if legacy_explicit else None
    if override_explicit and legacy_explicit and override_value != legacy_value:
        raise ValueError("workspace_path and workspace_path_override conflict")
    if override_explicit:
        stored = override_value
    elif legacy_explicit:
        stored = legacy_value
    else:
        stored = None
    if require_ungrouped_path and server_group_id is None and stored is None:
        raise ValueError("workspace_path or workspace_path_override is required when ungrouped")
    return stored


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ControlPlaneSnapshotData(StrictModel):
    current: dict[str, Any]
    history: dict[str, Any]


class ControlPlaneSnapshot(StrictModel):
    schema_version: str
    snapshot_revision: int
    server_time: str | None
    data: ControlPlaneSnapshotData


class ResourceConstraints(StrictModel):
    gpu_count: int = Field(ge=1, le=1024)
    # A direct-GPU claim reserves these amounts at *each endpoint selected for
    # the gang*.  Per-endpoint semantics prevent a multi-host request from
    # silently dividing a host requirement across machines.
    cpu_cores: float | None = Field(default=None, gt=0, le=4096)
    memory_mib: int | None = Field(default=None, gt=0, le=16 * 1024 * 1024)
    min_available_cpu_cores: float | None = Field(default=None, ge=0)
    min_available_memory_mib: int | None = Field(default=None, ge=0)
    min_total_vram_mib: int | None = Field(default=None, ge=1)
    min_free_vram_mib: int | None = Field(default=None, ge=0)
    nodes: int = Field(default=1, ge=1, le=1024)
    gpus_per_node: int | None = Field(default=None, ge=1, le=1024)
    same_host: bool = False
    placement: Literal["pack", "spread", "exact"] = "pack"
    endpoint_labels: list[str] = Field(default_factory=list)
    gpu_labels: list[str] = Field(default_factory=list)
    endpoint_ids: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    deny_endpoint_ids: list[str] = Field(default_factory=list)
    deny_gpu_ids: list[str] = Field(default_factory=list)
    server_group_ids: list[str] = Field(default_factory=list)
    allow_conservative_backfill: bool = False

    @field_validator(
        "endpoint_labels",
        "gpu_labels",
        "endpoint_ids",
        "gpu_ids",
        "deny_endpoint_ids",
        "deny_gpu_ids",
        "server_group_ids",
    )
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("constraint lists must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("constraint list values must not be empty")
        return values

    @model_validator(mode="after")
    def validate_topology(self) -> ResourceConstraints:
        if self.nodes > 1 and self.gpus_per_node is None:
            raise ValueError("nodes > 1 requires explicit gpus_per_node")
        if self.gpus_per_node is not None and self.gpus_per_node * self.nodes != self.gpu_count:
            raise ValueError("gpu_count must equal nodes * gpus_per_node when gpus_per_node is set")
        if self.same_host and self.nodes != 1:
            raise ValueError("same_host requires nodes=1")
        if self.placement == "exact" and not self.gpu_ids:
            raise ValueError("exact placement requires stable gpu_ids")
        if self.gpu_ids and len(self.gpu_ids) != self.gpu_count:
            raise ValueError("gpu_ids must contain exactly gpu_count values")
        overlap = set(self.gpu_ids).intersection(self.deny_gpu_ids)
        if overlap:
            raise ValueError(f"gpu ids appear in both allow and deny constraints: {sorted(overlap)}")
        return self


class RequestCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    duration_seconds: int = Field(default=DEFAULT_LEASE_WINDOW_SECONDS, ge=60, le=60 * 60 * 24 * 30)
    start_after: datetime | None = None
    deadline: datetime | None = None
    approval_ref: str | None = Field(default=None, max_length=500)
    constraints: ResourceConstraints

    @model_validator(mode="after")
    def validate_times(self) -> RequestCreate:
        if self.constraints.gpu_count == 0:
            raise ValueError("bare-metal requests require gpu_count >= 1")
        if self.start_after and self.start_after.tzinfo is None:
            raise ValueError("start_after must include a timezone")
        if self.deadline and self.deadline.tzinfo is None:
            raise ValueError("deadline must include a timezone")
        if self.start_after and self.deadline and self.deadline <= self.start_after:
            raise ValueError("deadline must be after start_after")
        return self


class RequestCreateFlat(StrictModel):
    """CLI-friendly request form that is converted to the canonical nested schema."""

    project_id: str = Field(min_length=1, max_length=64)
    task_ref: str = Field(min_length=1, max_length=255)
    purpose: str = Field(min_length=1, max_length=1000)
    gpu_count: int = Field(ge=1)
    duration_seconds: int = Field(default=DEFAULT_LEASE_WINDOW_SECONDS, ge=60)
    start_after: datetime | None = None
    deadline: datetime | None = None
    approval_ref: str | None = Field(default=None, max_length=500)
    min_available_cpu_cores: float | None = Field(default=None, ge=0)
    min_available_memory_mib: int | None = Field(default=None, ge=0)
    min_total_vram_mib: int | None = Field(default=None, ge=1)
    min_free_vram_mib: int | None = Field(default=None, ge=0)
    nodes: int = Field(default=1, ge=1)
    gpus_per_node: int | None = Field(default=None, ge=1)
    same_host: bool = False
    placement: Literal["pack", "spread", "exact"] = "pack"
    endpoint_labels: list[str] = Field(default_factory=list)
    gpu_labels: list[str] = Field(default_factory=list)
    endpoint_ids: list[str] = Field(default_factory=list)
    gpu_ids: list[str] = Field(default_factory=list)
    deny_endpoint_ids: list[str] = Field(default_factory=list)
    deny_gpu_ids: list[str] = Field(default_factory=list)
    server_group_ids: list[str] = Field(default_factory=list)
    allow_conservative_backfill: bool = False

    def canonical(self) -> RequestCreate:
        data = self.model_dump()
        constraint_fields = set(ResourceConstraints.model_fields)
        constraints = {key: data.pop(key) for key in list(data) if key in constraint_fields}
        return RequestCreate.model_validate({**data, "constraints": constraints})


class LeaseBind(StrictModel):
    run_id: str = Field(min_length=1, max_length=255)
    process_keys: list[str] = Field(default_factory=list)

    @field_validator("process_keys")
    @classmethod
    def process_key_count(cls, value: list[str]) -> list[str]:
        if len(value) > 1024:
            raise ValueError("too many process keys")
        return value


class LeaseObservedBind(StrictModel):
    """Bind every fresh observed process on a lease to one already-started run."""

    run_id: str | None = Field(default=None, min_length=1, max_length=255)


class LeaseGPUAssignment(StrictModel):
    """Exact GPU assignment selected by the human operator."""

    gpu_ids: list[str] = Field(min_length=1, max_length=1024)

    @field_validator("gpu_ids")
    @classmethod
    def unique_gpu_ids(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(not value for value in values):
            raise ValueError("gpu_ids must contain unique non-empty values")
        return values


class ReservationCreate(StrictModel):
    project_id: str = Field(min_length=1, max_length=64)
    gpu_ids: list[str] = Field(default_factory=list)
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=1, max_length=1000)
    constraints: ResourceConstraints | None = None

    @model_validator(mode="after")
    def validate_window(self) -> ReservationCreate:
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("reservation times must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("reservation end_at must be after start_at")
        if not self.gpu_ids and self.constraints is None:
            raise ValueError("reservation requires gpu_ids or constraints")
        if self.constraints is not None and self.constraints.gpu_count == 0:
            raise ValueError("reservations require constraints.gpu_count >= 1")
        return self


class MaintenanceCreate(StrictModel):
    endpoint_id: str | None = None
    gpu_id: str | None = None
    start_at: datetime
    end_at: datetime
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_target_and_window(self) -> MaintenanceCreate:
        if (self.endpoint_id is None) == (self.gpu_id is None):
            raise ValueError("maintenance must target exactly one endpoint_id or gpu_id")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise ValueError("maintenance times must include a timezone")
        if self.end_at <= self.start_at:
            raise ValueError("maintenance end_at must be after start_at")
        return self


class ServerGroupCreate(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,127}$")
    display_name: str = Field(min_length=1, max_length=120)
    workspace_path: str = Field(min_length=1, max_length=2000)
    environment_notes: str | None = Field(default=None, max_length=8000)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("workspace_path")
    @classmethod
    def valid_workspace_path(cls, value: str) -> str:
        return absolute_single_line_path(value)

    @field_validator("environment_notes", "description")
    @classmethod
    def plain_text_fields(cls, value: str | None) -> str | None:
        return plain_text(value)


class ServerGroupUpdate(StrictModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=2000)
    environment_notes: str | None = Field(default=None, max_length=8000)
    description: str | None = Field(default=None, max_length=1000)

    @field_validator("workspace_path")
    @classmethod
    def valid_workspace_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return absolute_single_line_path(value)

    @field_validator("environment_notes", "description")
    @classmethod
    def plain_text_fields(cls, value: str | None) -> str | None:
        return plain_text(value)

    @model_validator(mode="after")
    def has_update(self) -> ServerGroupUpdate:
        if not self.model_fields_set:
            raise ValueError("server group update must include at least one mutable field")
        if "workspace_path" in self.model_fields_set and self.workspace_path is None:
            raise ValueError("workspace_path cannot be cleared")
        return self


class EndpointCreate(StrictModel):
    """Immutable endpoint identity plus its initial safe monitoring metadata."""

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,127}$")
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    ssh_user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_alias: str | None = Field(default=None, min_length=1, max_length=120)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=2000)
    workspace_path_override: str | None = Field(default=None, min_length=1, max_length=2000)
    # One default, shared with ``EndpointConfig``. The two disagreed: YAML
    # seeded ``linux-nvidia`` while every REST and MCP caller that omitted the
    # field got ``server-script-v1`` -- the profile for a host that carries its
    # own collection script, and the one answer that cannot work on a plain
    # NVIDIA box. A server registered without saying what it is is a GPU host.
    observation_profile: str = Field(default="linux-nvidia", min_length=1, max_length=40)
    keepalive_adapter_id: KeepaliveAdapterId | None = None
    keepalive_policy: KeepalivePolicy = "disabled"
    labels: list[str] = Field(default_factory=list)
    storage_group: str | None = Field(default=None, max_length=120)
    server_group_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,127}$")
    expected_gpu_count: int | None = Field(default=None, ge=1, le=1024)
    expected_gpu_total_vram_mib: int | None = Field(default=None, ge=1)
    owner_project_id: str | None = Field(default=None, min_length=1, max_length=64)
    # Kept only for one-project legacy imports.  Endpoint ownership is now one
    # project, rather than a scheduler placement allowlist.
    project_ids: list[str] = Field(default_factory=list)

    @field_validator("labels", "project_ids")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("endpoint lists must not contain duplicates")
        if any(not value for value in values):
            raise ValueError("endpoint list values must not be empty")
        return values

    @field_validator("workspace_path", "workspace_path_override")
    @classmethod
    def valid_workspace_path(cls, value: str | None) -> str | None:
        return optional_absolute_workspace_path(value)

    @field_validator("observation_profile")
    @classmethod
    def known_observation_profile(cls, value: str) -> str:
        from serverpilot.plugins import is_known_observation_profile

        if not is_known_observation_profile(value):
            raise ValueError(f"unknown observation profile: {value}")
        return value

    def stored_workspace_override(self) -> str | None:
        return resolve_stored_workspace_override(
            server_group_id=self.server_group_id,
            workspace_path=self.workspace_path,
            workspace_path_override=self.workspace_path_override,
            fields_set=self.model_fields_set,
            require_ungrouped_path=True,
        )

    @model_validator(mode="after")
    def resolve_owner(self) -> EndpointCreate:
        self.stored_workspace_override()
        if self.owner_project_id and self.project_ids and self.project_ids != [self.owner_project_id]:
            raise ValueError("project_ids may only repeat owner_project_id for legacy imports")
        if self.owner_project_id is None and len(self.project_ids) == 1:
            self.owner_project_id = self.project_ids[0]
        if self.keepalive_policy == "idle_keepalive" and self.keepalive_adapter_id is None:
            raise ValueError("idle_keepalive requires a sealed keepalive adapter")
        return self


class EndpointUpdate(StrictModel):
    """Only mutable endpoint metadata; identity and lifecycle use dedicated operations."""

    ssh_user: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_alias: str | None = Field(default=None, min_length=1, max_length=120)
    workspace_path: str | None = Field(default=None, min_length=1, max_length=2000)
    workspace_path_override: str | None = Field(default=None, min_length=1, max_length=2000)
    observation_profile: str | None = Field(default=None, min_length=1, max_length=40)
    keepalive_adapter_id: KeepaliveAdapterId | None = None
    labels: list[str] | None = None
    storage_group: str | None = Field(default=None, max_length=120)
    server_group_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9-]{1,127}$")
    expected_gpu_count: int | None = Field(default=None, ge=1, le=1024)
    expected_gpu_total_vram_mib: int | None = Field(default=None, ge=1)
    owner_project_id: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("labels")
    @classmethod
    def unique_labels(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and (len(values) != len(set(values)) or any(not value for value in values)):
            raise ValueError("endpoint labels must contain unique non-empty values")
        return values

    @field_validator("workspace_path", "workspace_path_override")
    @classmethod
    def valid_workspace_path(cls, value: str | None) -> str | None:
        return optional_absolute_workspace_path(value)

    @field_validator("observation_profile")
    @classmethod
    def known_observation_profile(cls, value: str | None) -> str | None:
        from serverpilot.plugins import is_known_observation_profile

        if value is not None and not is_known_observation_profile(value):
            raise ValueError(f"unknown observation profile: {value}")
        return value

    def workspace_override_specified(self) -> bool:
        return bool({"workspace_path", "workspace_path_override"} & self.model_fields_set)

    def stored_workspace_override(self) -> str | None:
        return resolve_stored_workspace_override(
            server_group_id=self.server_group_id,
            workspace_path=self.workspace_path,
            workspace_path_override=self.workspace_path_override,
            fields_set=self.model_fields_set,
            require_ungrouped_path=False,
        )

    @model_validator(mode="after")
    def has_update(self) -> EndpointUpdate:
        if not self.model_fields_set:
            raise ValueError("endpoint update must include at least one mutable field")
        self.stored_workspace_override()
        return self


class EndpointUpsert(EndpointCreate):
    """Deprecated in-process compatibility model for the legacy GUI importer.

    Public REST clients use EndpointCreate (POST) and EndpointUpdate (PATCH).
    """

    lifecycle_state: Literal["active", "draining"] | None = None
    enabled: bool | None = None


class EndpointKeepaliveRequest(StrictModel):
    """The endpoint control accepts one explicit boolean, never a GPU target."""

    enabled: bool


class CollectorSettingsUpdate(StrictModel):
    interval_seconds: Literal[5, 10, 30]


class TelemetryInput(StrictModel):
    gpu_uuid: str = Field(min_length=1, max_length=160)
    gpu_index: int = Field(ge=0, le=1024)
    cuda_ordinal: int = Field(ge=0, le=1024)
    name: str = Field(min_length=1, max_length=255)
    total_vram_mib: int = Field(ge=1)
    memory_used_mib: int = Field(ge=0)
    memory_free_mib: int = Field(ge=0)
    gpu_utilization_pct: int | None = Field(default=None, ge=0, le=100)
    memory_utilization_pct: int | None = Field(default=None, ge=0, le=100)
    temperature_c: int | None = Field(default=None, ge=-100, le=300)
    power_watts: float | None = Field(default=None, ge=0)
    pstate: str | None = Field(default=None, max_length=32)
    health: str = Field(default="OK", min_length=1, max_length=32)


class ProcessInput(StrictModel):
    gpu_uuid: str = Field(min_length=1, max_length=160)
    pid: int = Field(ge=1, le=2**31 - 1)
    used_memory_mib: int = Field(ge=0)
    executable: str = Field(min_length=1, max_length=255)
    username: str | None = Field(default=None, max_length=120)
    process_started_at: datetime

    @field_validator("process_started_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("process_started_at must include a timezone")
        return value


class HostTelemetryInput(StrictModel):
    """Host-wide telemetry captured with the GPU sample for one endpoint."""

    cpu_count: int = Field(ge=1, le=1_048_576)
    load_1m: float = Field(ge=0)
    cpu_total_ticks: int | None = Field(default=None, ge=0)
    cpu_idle_ticks: int | None = Field(default=None, ge=0)
    cpu_usage_usec: int | None = Field(default=None, ge=0)
    cpu_quota_usec: int | None = Field(default=None, ge=0)
    cpu_period_usec: int | None = Field(default=None, ge=1)
    memory_total_mib: int = Field(ge=1)
    memory_available_mib: int = Field(ge=0)
    memory_limit_mib: int | None = Field(default=None, ge=1)
    memory_current_mib: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def available_memory_is_bounded(self) -> HostTelemetryInput:
        if self.memory_available_mib > self.memory_total_mib:
            raise ValueError("memory_available_mib must not exceed memory_total_mib")
        if (
            self.cpu_total_ticks is not None
            and self.cpu_idle_ticks is not None
            and self.cpu_idle_ticks > self.cpu_total_ticks
        ):
            raise ValueError("cpu_idle_ticks must not exceed cpu_total_ticks")
        return self


class EndpointObservation(StrictModel):
    endpoint_id: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    boot_id: str = Field(min_length=1, max_length=120)
    host: HostTelemetryInput
    gpus: list[TelemetryInput]
    processes: list[ProcessInput] = Field(default_factory=list)
    observation_complete: bool = True
    # ``cpu_only`` is a positive hardware discovery result. ``unknown`` keeps
    # a failed NVIDIA probe fail-closed without overwriting earlier GPU facts.
    gpu_probe_status: Literal["gpu", "cpu_only", "unknown"] = "unknown"
    scheduler: dict[str, Any] | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def gpu_identity_is_unique(self) -> EndpointObservation:
        gpu_uuids = [gpu.gpu_uuid for gpu in self.gpus]
        if len(gpu_uuids) != len(set(gpu_uuids)):
            raise ValueError("observation gpus must contain unique gpu_uuid values")
        gpu_indexes = [gpu.gpu_index for gpu in self.gpus]
        if len(gpu_indexes) != len(set(gpu_indexes)):
            raise ValueError("observation gpus must contain unique gpu_index values")
        cuda_ordinals = [gpu.cuda_ordinal for gpu in self.gpus]
        if len(cuda_ordinals) != len(set(cuda_ordinals)):
            raise ValueError("observation gpus must contain unique cuda_ordinal values")
        return self


class RetentionPrune(StrictModel):
    older_than_seconds: int = Field(ge=60, le=60 * 60 * 24 * 3650)
