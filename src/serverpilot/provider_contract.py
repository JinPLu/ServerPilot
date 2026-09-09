"""Provider-neutral allocation and connection facts shared by every consumer."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class AllocatedDevice(ProviderModel):
    gpu_uuid: str = Field(min_length=1, max_length=160)
    physical_index: int | None = Field(default=None, ge=0)
    device_selector: str | None = Field(default=None, min_length=1, max_length=160)
    name: str | None = Field(default=None, max_length=255)
    total_vram_mib: int | None = Field(default=None, ge=1)
    memory_used_mib: int | None = Field(default=None, ge=0)
    gpu_utilization_pct: int | None = Field(default=None, ge=0, le=100)
    running_process_count: int | None = Field(default=None, ge=0)


class AllocationLocation(ProviderModel):
    node: str = Field(min_length=1, max_length=253, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    gpu_count: int = Field(ge=0, le=1024)
    devices: list[AllocatedDevice] = Field(default_factory=list)
    running_process_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def device_identity(self) -> AllocationLocation:
        ids = [item.gpu_uuid for item in self.devices]
        if len(ids) != len(set(ids)) or len(ids) > self.gpu_count:
            raise ValueError("allocation devices must be unique and fit the allocated count")
        return self


class AllocationEntry(ProviderModel):
    """Display/copy instructions only; the control plane never executes argv."""

    kind: Literal["ssh", "allocation"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    workspace_path: str = Field(min_length=1, max_length=2000, pattern=r"^/")
    argv: list[str] = Field(default_factory=list, max_length=64)
    instructions: str = Field(max_length=1000)
    verified: bool

    @field_validator("argv")
    @classmethod
    def bounded_arguments(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 2000 or any(c in item for c in "\x00\n\r") for item in value):
            raise ValueError("entry argv must contain bounded single-line arguments")
        return value

    @model_validator(mode="after")
    def entry_scope(self) -> AllocationEntry:
        if self.kind == "allocation" and not self.argv:
            raise ValueError("an allocation entry requires its provider's entry arguments")
        if any(c in self.host + self.workspace_path for c in "\x00\n\r"):
            raise ValueError("entry address and workspace must be single-line")
        return self


class JobAccounting(ProviderModel):
    cpu_time_seconds: float | None = Field(ge=0)
    rss_mib: float | None = Field(ge=0)
    observed_at: str

    @field_validator("observed_at")
    @classmethod
    def timestamp(cls, value: str) -> str:
        if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("accounting timestamps require a timezone")
        return value


class AllocationFact(ProviderModel):
    name: str | None = Field(default=None, max_length=255)
    owner: str | None = Field(default=None, max_length=120)
    accounting: JobAccounting | None = None
    allocation_ref: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    state: Literal["active", "released", "expired", "failed", "unknown"]
    locations: list[AllocationLocation]
    entry: AllocationEntry | None
    expires_at: str | None
    observed_at: str
    workload_observed_at: str | None

    @field_validator("expires_at", "observed_at", "workload_observed_at")
    @classmethod
    def timestamp(cls, value: str | None) -> str | None:
        if value is not None:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("provider timestamps require a timezone")
        return value

    @model_validator(mode="after")
    def locations_unique(self) -> AllocationFact:
        nodes = [location.node for location in self.locations]
        if len(nodes) != len(set(nodes)):
            raise ValueError("allocation nodes must be unique")
        return self


class ConnectionFact(ProviderModel):
    state: Literal["connected", "disconnected", "network_unavailable", "action_required"]
    error_code: Literal["auth_failed", "network_unreachable", "session_missing", "connection_failed"] | None
    message: str = Field(max_length=500)


class ProviderConnectionUpdate(ProviderModel):
    enabled: bool
