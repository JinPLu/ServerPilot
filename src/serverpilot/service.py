"""Single-owner domain service for inventory, telemetry, leases and audit events."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import re
import secrets
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import and_, delete, func, or_, select, text
from sqlalchemy.orm import Session

from serverpilot import SCHEMA_VERSION, __version__
from serverpilot.config import RESERVED_SYSTEM_ID, EndpointConfig, InventoryConfig
from serverpilot.database import Database
from serverpilot.models import (
    Actor,
    ActorProject,
    Alert,
    AllocationRequest,
    AuditEvent,
    Endpoint,
    EndpointDeletion,
    EndpointTelemetryCurrent,
    EndpointTelemetrySnapshot,
    GPUDevice,
    IdempotencyRecord,
    KeepaliveCurrent,
    Lease,
    LeaseEndpointCommitment,
    LeaseResource,
    MaintenanceWindow,
    ProcessObservation,
    Project,
    ProviderState,
    Reservation,
    Revision,
    RuntimeSetting,
    ServerGroup,
    TelemetryCurrent,
    TelemetrySnapshot,
    WorkloadBinding,
)
from serverpilot.schemas import (
    CollectorSettingsUpdate,
    EndpointCreate,
    EndpointObservation,
    EndpointUpdate,
    EndpointUpsert,
    LeaseBind,
    LeaseObservedBind,
    MaintenanceCreate,
    RequestCreate,
    ReservationCreate,
    ResourceConstraints,
    RetentionPrune,
    ServerGroupCreate,
    ServerGroupUpdate,
)
from serverpilot.timeutil import ensure_utc, json_dump, json_load, utcnow

ACTIVE_LEASE_STATES = {"HELD", "ACTIVE", "ORPHANED_BUSY", "CONFLICT"}
SYSTEM_ACTOR_ID = RESERVED_SYSTEM_ID
SYSTEM_PROJECT_ID = RESERVED_SYSTEM_ID
TERMINAL_LEASE_STATES = {"RELEASED", "EXPIRED_EMPTY"}
# Alerts that describe one lease and become meaningless once that lease can no
# longer own resources. `idle_lease` belongs here: a released lease is not idle,
# it is gone, and nothing else was closing its warning.
LEASE_SCOPED_ALERT_TYPES = {"lease_process_conflict", "orphaned_busy", "idle_lease"}
TELEMETRY_HISTORY_INTERVAL_SECONDS = 60
TELEMETRY_HISTORY_RETENTION_SECONDS = 24 * 60 * 60
TELEMETRY_RECENT_AVERAGE_WINDOW_SECONDS = 10 * 60
# The collector derives a process start time from `ps etimes`, which has
# one-second precision and is sampled after the endpoint observation begins.
# Preserve the already-observed identity across this bounded measurement
# jitter; otherwise a healthy long-running process can look new on every
# collection and lose its workload attribution.
PROCESS_START_TIME_JITTER_SECONDS = 2
MUTATING_ROLES = {"allocator", "operator", "admin"}
COLLECTOR_INTERVAL_PRESETS = {5, 10, 30}
COLLECTOR_INTERVAL_SETTING_KEY = "collector_interval_seconds"
# How long to leave an endpoint whose collection keeps failing out of the
# regular cycle. Once its telemetry is stale the endpoint is already
# fail-closed, so probing it every interval changes no answer; what it does
# spend is a failed-collection write and a revision bump every cycle, which
# makes every reader think something changed. Kept close to the interval so a
# host someone has just repaired rejoins without anyone having to ask.
DEGRADED_ENDPOINT_PROBE_SECONDS = 60
PLUGIN_CAPACITY_SETTING_PREFIX = "pc:"
COLLECTOR_VERSION_SETTING_PREFIX = "civ:"
REMOTE_COLLECTOR_PROFILES = frozenset({"server-script-v1"})
# Routine coordination is cooperative, not an administrative workflow.
# Endpoint inventory is shared on loopback; owner_project_id is attribution,
# not an endpoint-management permission boundary. Lease/request ownership still
# prevents one actor from changing another actor's active resource contract.
OPERATOR_ROLES = MUTATING_ROLES
ADMIN_ROLES = {"admin"}
CUDA_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
T = TypeVar("T")


class BrokerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True, slots=True)
class ActorContext:
    id: str
    role: str
    project_ids: frozenset[str]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _gpu_history_series_label(*, gpu_index: int, gpu_uuid: str, index_count: int) -> str:
    """Keep the usual ``GPU N`` label unless two present cards share an index."""

    label = f"GPU {gpu_index}"
    if index_count > 1:
        return f"{label} ({gpu_uuid})"
    return label


def _external_datetime(value: str | None) -> datetime | None:
    if not value or value in {"Unknown", "N/A", "None"}:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return ensure_utc(parsed)


def resolve_effective_workspace(
    *,
    override: str | None,
    group_workspace: str | None,
) -> str | None:
    """Resolve the remote working directory: endpoint override, then group default."""

    if override:
        return override
    return group_workspace


class BrokerService:
    """The only component allowed to mutate the broker database.

    The service deliberately contains scheduling decisions, derived availability
    and audit writes together so REST, CLI, MCP and GUI cannot drift.
    """

    def __init__(
        self,
        database: Database,
        inventory: InventoryConfig,
    ) -> None:
        self.database = database
        self.inventory = inventory
        self._collector_settings_lock = threading.Lock()
        # One worker, created on first use. This is not a throughput
        # compromise: it is the `single_writer` contract this control plane
        # already publishes, kept inside the process instead of being fought
        # out in SQLite's busy handler by whichever threads happen to arrive.
        self._domain_executor: ThreadPoolExecutor | None = None

    # ---- initialization, identity and transaction primitives -----------------

    async def in_domain(self, call: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
        """Run one domain call off the event loop.

        Every domain method is synchronous SQLite. Run on the event loop it
        stops every other request for the length of the transaction, and a
        `BEGIN IMMEDIATE` that has to wait can stop it for the whole busy
        timeout. Coroutines enter the domain through here and nowhere else.
        """

        if self._domain_executor is None:
            self._domain_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="serverpilot-domain"
            )
        return await asyncio.get_running_loop().run_in_executor(
            self._domain_executor, functools.partial(call, *args, **kwargs)
        )

    def shutdown(self) -> None:
        """Release the domain worker; safe to call more than once."""

        executor, self._domain_executor = self._domain_executor, None
        if executor is not None:
            executor.shutdown(wait=True)

    def initialize(self, *, sync_inventory: bool = False) -> None:
        """Initialize the loopback control-plane state."""
        self.database.migrate()

        def operation(session: Session) -> None:
            now = utcnow()
            revision = session.get(Revision, 1)
            if revision is None:
                session.add(Revision(id=1, value=0, updated_at=now))
            self._load_runtime_collector_settings(session)
            has_inventory = (session.scalar(select(func.count()).select_from(Endpoint)) or 0) > 0
            has_unowned_endpoints = (
                session.scalar(
                    select(func.count())
                    .select_from(Endpoint)
                    .where(Endpoint.owner_project_id.is_(None))
                )
                or 0
            ) > 0
            if sync_inventory or not has_inventory or has_unowned_endpoints:
                self._upsert_inventory(session, now)
            self._ensure_system_identity(session, now)
            self._normalize_legacy_workload_conflicts(
                session,
                now,
                actor_id=SYSTEM_ACTOR_ID,
            )
            self._resolve_orphaned_alerts(session, now)
            self._bump_revision(session, now)

        self._write(operation)

    def _load_runtime_collector_settings(self, session: Session) -> None:
        setting = session.get(RuntimeSetting, COLLECTOR_INTERVAL_SETTING_KEY)
        if setting is None:
            return
        try:
            interval_seconds = int(setting.value)
        except ValueError:
            return
        if interval_seconds not in COLLECTOR_INTERVAL_PRESETS:
            return
        self.inventory.collector.interval_seconds = interval_seconds
        self.inventory.collector.stale_after_seconds = interval_seconds * 3

    def collector_interval_seconds(self) -> int:
        return self.inventory.collector.interval_seconds

    def collector_settings(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, {"viewer", "allocator", "operator", "admin"})

        def operation(session: Session) -> dict[str, Any]:
            return self.envelope(
                session,
                {
                    "interval_seconds": self.inventory.collector.interval_seconds,
                    "stale_after_seconds": self.inventory.collector.stale_after_seconds,
                    "allowed_intervals": sorted(COLLECTOR_INTERVAL_PRESETS),
                },
            )

        return self._read(operation)

    def update_collector_settings(
        self,
        actor: ActorContext,
        settings: CollectorSettingsUpdate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> tuple[dict[str, Any], bool]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="collector.settings.updated",
                key=idempotency_key,
            )
            if existing is not None:
                return existing, False
            now = utcnow()
            before = {
                "interval_seconds": self.inventory.collector.interval_seconds,
                "stale_after_seconds": self.inventory.collector.stale_after_seconds,
            }
            setting = session.get(RuntimeSetting, COLLECTOR_INTERVAL_SETTING_KEY)
            if setting is None:
                setting = RuntimeSetting(
                    key=COLLECTOR_INTERVAL_SETTING_KEY,
                    value=str(settings.interval_seconds),
                    updated_at=now,
                )
                session.add(setting)
            else:
                setting.value = str(settings.interval_seconds)
                setting.updated_at = now
            revision = self._bump_revision(session, now)
            after = {
                "interval_seconds": settings.interval_seconds,
                "stale_after_seconds": settings.interval_seconds * 3,
                "allowed_intervals": sorted(COLLECTOR_INTERVAL_PRESETS),
            }
            event = self._audit(
                session,
                actor_id=actor.id,
                action="collector.settings.updated",
                resource_type="runtime_setting",
                resource_id=COLLECTOR_INTERVAL_SETTING_KEY,
                result="success",
                before=before,
                after=after,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "settings": after,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="collector.settings.updated",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result, True

        with self._collector_settings_lock:
            result, applied = self._write(operation)
            if applied:
                self.inventory.collector.interval_seconds = settings.interval_seconds
                self.inventory.collector.stale_after_seconds = settings.interval_seconds * 3
            return result

    def _upsert_server_groups(self, session: Session, now: datetime) -> None:
        for configured in self.inventory.server_groups:
            group = session.get(ServerGroup, configured.id)
            if group is None:
                session.add(
                    ServerGroup(
                        id=configured.id,
                        display_name=configured.display_name,
                        workspace_path=configured.workspace_path,
                        environment_notes=configured.environment_notes,
                        description=configured.description,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            if group.workspace_path != configured.workspace_path:
                self._reject_group_workspace_change_if_keepalive(
                    session, group.id, fields=["workspace_path"]
                )
            group.display_name = configured.display_name
            group.workspace_path = configured.workspace_path
            group.environment_notes = configured.environment_notes
            group.description = configured.description
            group.updated_at = now
        session.flush()

    def _reject_group_workspace_change_if_keepalive(
        self,
        session: Session,
        group_id: str,
        *,
        fields: list[str],
    ) -> None:
        inheriting = session.scalars(
            select(Endpoint).where(
                Endpoint.server_group_id == group_id,
                Endpoint.workspace_path.is_(None),
            )
        ).all()
        blocked = [
            endpoint.id
            for endpoint in inheriting
            if self._active_keepalive_for_endpoint(session, endpoint.id) is not None
        ]
        if blocked:
            raise BrokerError(
                "keepalive_endpoint_connection_in_use",
                "stop active keepalive on inheriting members before changing the group workspace",
                status_code=409,
                details={"fields": fields, "endpoint_ids": blocked},
            )

    def _upsert_inventory(self, session: Session, now: datetime) -> None:
        for configured_project in self.inventory.projects:
            project = session.get(Project, configured_project.id)
            if project is None:
                session.add(
                    Project(
                        id=configured_project.id,
                        display_name=configured_project.display_name,
                        weight=configured_project.weight,
                        quota_gpus=configured_project.quota_gpus,
                        concurrency_limit=configured_project.concurrency_limit,
                        enabled=True,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                project.display_name = configured_project.display_name
                project.weight = configured_project.weight
                project.quota_gpus = configured_project.quota_gpus
                project.concurrency_limit = configured_project.concurrency_limit
                project.updated_at = now

        session.flush()
        tombstoned_ids = set(session.scalars(select(EndpointDeletion.endpoint_id)))
        self._upsert_server_groups(session, now)
        for configured_endpoint in self.inventory.endpoints:
            if configured_endpoint.id in tombstoned_ids:
                continue
            endpoint = session.get(Endpoint, configured_endpoint.id)
            if endpoint is None:
                assigned_group_id = configured_endpoint.server_group_id
                if assigned_group_id is not None and session.get(ServerGroup, assigned_group_id) is None:
                    raise BrokerError(
                        "server_group_not_found",
                        f"endpoint {configured_endpoint.id} references unknown server_group_id",
                        status_code=422,
                    )
                self._require_effective_workspace(
                    override=configured_endpoint.workspace_path,
                    group=session.get(ServerGroup, assigned_group_id)
                    if assigned_group_id is not None
                    else None,
                )
                endpoint = Endpoint(
                    id=configured_endpoint.id,
                    host=configured_endpoint.host,
                    port=configured_endpoint.port,
                    ssh_user=configured_endpoint.ssh_user,
                    ssh_alias=configured_endpoint.ssh_alias,
                    workspace_path=configured_endpoint.workspace_path,
                    observation_profile=configured_endpoint.observation_profile,
                    keepalive_adapter_id=configured_endpoint.keepalive_adapter_id,
                    keepalive_policy=configured_endpoint.keepalive_policy,
                    labels_json=json_dump(configured_endpoint.labels),
                    storage_group=configured_endpoint.storage_group,
                    server_group_id=assigned_group_id,
                    expected_gpu_count=configured_endpoint.expected_gpu_count,
                    expected_gpu_total_vram_mib=configured_endpoint.expected_gpu_total_vram_mib,
                    resource_kind="unknown",
                    owner_project_id=(
                        configured_endpoint.project_ids[0]
                        if configured_endpoint.project_ids
                        else None
                    ),
                    lifecycle_state="active",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
            else:
                if (endpoint.host, endpoint.port) != (
                    configured_endpoint.host,
                    configured_endpoint.port,
                ):
                    raise BrokerError(
                        "endpoint_identity_immutable",
                        f"endpoint {endpoint.id} cannot change host:port; create a new immutable endpoint id",
                        status_code=409,
                    )
                next_group_id = (
                    configured_endpoint.server_group_id
                    if "server_group_id" in configured_endpoint.model_fields_set
                    else endpoint.server_group_id
                )
                next_override = (
                    configured_endpoint.workspace_path
                    if "workspace_path" in configured_endpoint.model_fields_set
                    else endpoint.workspace_path
                )
                next_group = (
                    session.get(ServerGroup, next_group_id) if next_group_id is not None else None
                )
                if next_group_id is not None and next_group is None:
                    raise BrokerError(
                        "server_group_not_found",
                        f"endpoint {endpoint.id} references unknown server_group_id",
                        status_code=422,
                    )
                effective_before = self._endpoint_effective_workspace(session, endpoint)
                effective_after = resolve_effective_workspace(
                    override=next_override,
                    group_workspace=next_group.workspace_path if next_group is not None else None,
                )
                if effective_after is None:
                    raise BrokerError(
                        "endpoint_workspace_required",
                        "endpoint requires workspace_path or a server group default",
                        status_code=422,
                    )
                protected_values = {
                    "ssh_user": configured_endpoint.ssh_user,
                    "ssh_alias": configured_endpoint.ssh_alias,
                    "workspace_path": effective_after,
                    "observation_profile": configured_endpoint.observation_profile,
                    "keepalive_adapter_id": configured_endpoint.keepalive_adapter_id,
                }
                current_protected = {
                    "ssh_user": endpoint.ssh_user,
                    "ssh_alias": endpoint.ssh_alias,
                    "workspace_path": effective_before,
                    "observation_profile": endpoint.observation_profile,
                    "keepalive_adapter_id": endpoint.keepalive_adapter_id,
                }
                changed_protected_fields = sorted(
                    field
                    for field, value in protected_values.items()
                    if value != current_protected[field]
                )
                if (
                    changed_protected_fields
                    and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
                ):
                    raise BrokerError(
                        "keepalive_endpoint_connection_in_use",
                        "stop the active endpoint keepalive before synchronizing changed connection or verification settings",
                        status_code=409,
                        details={"fields": changed_protected_fields},
                    )
                endpoint.ssh_user = configured_endpoint.ssh_user
                endpoint.ssh_alias = configured_endpoint.ssh_alias
                if "workspace_path" in configured_endpoint.model_fields_set:
                    endpoint.workspace_path = configured_endpoint.workspace_path
                endpoint.observation_profile = configured_endpoint.observation_profile
                if (
                    endpoint.keepalive_policy == "idle_keepalive"
                    and configured_endpoint.keepalive_adapter_id is None
                ):
                    raise BrokerError(
                        "keepalive_adapter_required",
                        "disable idle keepalive before removing its sealed endpoint adapter",
                        status_code=409,
                    )
                endpoint.keepalive_adapter_id = configured_endpoint.keepalive_adapter_id
                # Inventory's default starts new endpoints safely OFF. It must
                # not silently undo a policy written by the dedicated endpoint
                # control when the inventory did not name a policy.
                if "keepalive_policy" in configured_endpoint.model_fields_set:
                    endpoint.keepalive_policy = configured_endpoint.keepalive_policy
                endpoint.labels_json = json_dump(configured_endpoint.labels)
                endpoint.storage_group = configured_endpoint.storage_group
                if "server_group_id" in configured_endpoint.model_fields_set:
                    endpoint.server_group_id = configured_endpoint.server_group_id
                endpoint.expected_gpu_count = configured_endpoint.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = (
                    configured_endpoint.expected_gpu_total_vram_mib
                )
                if endpoint.owner_project_id is None and configured_endpoint.project_ids:
                    endpoint.owner_project_id = configured_endpoint.project_ids[0]
                endpoint.updated_at = now
            session.flush()

    @staticmethod
    def _ensure_system_identity(session: Session, now: datetime) -> tuple[Actor, Project]:
        """Create the tokenless identity reserved for internal keepalive records."""

        project = session.get(Project, SYSTEM_PROJECT_ID)
        actor = session.get(Actor, SYSTEM_ACTOR_ID)
        if project is not None or actor is not None:
            actor_project_ids = (
                set(
                    session.scalars(
                        select(ActorProject.project_id).where(
                            ActorProject.actor_id == SYSTEM_ACTOR_ID
                        )
                    ).all()
                )
                if actor is not None
                else set()
            )
            foreign_system_membership = session.scalar(
                select(ActorProject.actor_id)
                .where(
                    ActorProject.project_id == SYSTEM_PROJECT_ID,
                    ActorProject.actor_id != SYSTEM_ACTOR_ID,
                )
                .limit(1)
            )
            identity_is_exact = (
                project is not None
                and actor is not None
                and project.display_name == "ServerPilot internal"
                and project.weight == 1
                and project.quota_gpus is None
                and project.concurrency_limit is None
                and project.enabled
                and actor.display_name == "ServerPilot internal"
                and actor.role == "operator"
                and actor.enabled
                and actor_project_ids.issubset({SYSTEM_PROJECT_ID})
                and foreign_system_membership is None
            )
            if not identity_is_exact:
                raise BrokerError(
                    "reserved_system_identity_conflict",
                    "existing data conflicts with the tokenless ServerPilot internal identity",
                    status_code=409,
                )
        if project is None:
            project = Project(
                id=SYSTEM_PROJECT_ID,
                display_name="ServerPilot internal",
                weight=1,
                quota_gpus=None,
                concurrency_limit=None,
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(project)
        if actor is None:
            actor = Actor(
                id=SYSTEM_ACTOR_ID,
                display_name="ServerPilot internal",
                role="operator",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(actor)
        session.flush()
        membership = session.get(
            ActorProject,
            {"actor_id": SYSTEM_ACTOR_ID, "project_id": SYSTEM_PROJECT_ID},
        )
        if membership is None:
            session.add(ActorProject(actor_id=SYSTEM_ACTOR_ID, project_id=SYSTEM_PROJECT_ID))
        return actor, project

    @staticmethod
    def _server_groups_by_id(session: Session) -> dict[str, ServerGroup]:
        return {
            group.id: group
            for group in session.scalars(select(ServerGroup).order_by(ServerGroup.id)).all()
        }

    @staticmethod
    def _server_group_dict(group: ServerGroup) -> dict[str, Any]:
        return {
            "id": group.id,
            "display_name": group.display_name,
            "workspace_path": group.workspace_path,
            "environment_notes": group.environment_notes,
            "description": group.description,
            "created_at": _iso(group.created_at),
            "updated_at": _iso(group.updated_at),
        }

    def _project_server_group(
        self,
        group: ServerGroup,
        *,
        endpoints: list[Endpoint],
        gpu_payloads: list[dict[str, Any]],
        endpoint_payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload = self._server_group_dict(group)
        members = [endpoint for endpoint in endpoints if endpoint.server_group_id == group.id]
        payload.update(
            self._server_group_allocation_fields(
                members,
                gpu_payloads=gpu_payloads,
                endpoint_payloads=endpoint_payloads,
            )
        )
        return payload

    def _server_group_allocation_fields(
        self,
        members: list[Endpoint],
        *,
        gpu_payloads: list[dict[str, Any]],
        endpoint_payloads: list[dict[str, Any]],
    ) -> dict[str, Any]:
        from serverpilot.adapters import direct_claim_budget_seconds
        from serverpilot.plugins import DIRECT_PROFILE_LIMITS, get_plugin, is_plugin_profile

        delegated_endpoint: Endpoint | None = None
        delegated_plugin = None
        for endpoint in members:
            if not is_plugin_profile(endpoint.observation_profile):
                continue
            plugin = get_plugin(endpoint.observation_profile)
            if plugin is not None and "apply" in plugin.capabilities:
                delegated_endpoint = endpoint
                delegated_plugin = plugin
                break
        if delegated_plugin is not None and delegated_endpoint is not None:
            limits = {
                "max_gpus_per_lease": None,
                "max_lease_seconds": delegated_plugin.limits.get("max_lease_seconds"),
                "lease_ends": delegated_plugin.limits.get("lease_ends"),
                "cpu_cores_per_gpu": None,
                "memory_mib_per_gpu": None,
                "apply_max_seconds": delegated_plugin.limits.get("apply_max_seconds"),
                "queues": delegated_plugin.limits.get("queues", False),
            }
            capacity = next(
                (
                    item.get("scheduler_capacity")
                    for item in endpoint_payloads
                    if item.get("id") == delegated_endpoint.id
                ),
                None,
            )
            largest: int | None = None
            if isinstance(capacity, dict):
                for key in ("max_gpus_per_lease", "cpu_cores_per_gpu", "memory_mib_per_gpu"):
                    if key in capacity:
                        limits[key] = capacity[key]
                largest = capacity.get("largest_free_block")
                if largest is None:
                    free = capacity.get("free_gpu_count")
                    cap = limits.get("max_gpus_per_lease")
                    if isinstance(free, int) and isinstance(cap, int):
                        largest = min(free, cap)
            return {
                "allocation": "delegated",
                "limits": limits,
                "largest_allocatable_block": largest,
            }

        registered_by_host: dict[str, int] = defaultdict(int)
        allocatable_by_host: dict[str, int] = defaultdict(int)
        member_ids = {endpoint.id for endpoint in members}
        for gpu in gpu_payloads:
            endpoint_id = gpu.get("endpoint_id")
            if endpoint_id not in member_ids or gpu.get("present") is False:
                continue
            registered_by_host[endpoint_id] += 1
            if gpu.get("publicly_available") is True:
                allocatable_by_host[endpoint_id] += 1
        max_registered = max(registered_by_host.values(), default=0)
        max_allocatable = max(allocatable_by_host.values(), default=0)
        limits = {
            **dict(DIRECT_PROFILE_LIMITS),
            "max_gpus_per_lease": max_registered,
            "cpu_cores_per_gpu": None,
            "memory_mib_per_gpu": None,
            # A direct claim's cost is one host's stop plus one observation, so
            # the group can publish it instead of leaving the caller to guess.
            "apply_max_seconds": direct_claim_budget_seconds(
                self.inventory.collector.ssh_connect_timeout_seconds
            ),
        }
        return {
            "allocation": "direct",
            "limits": limits,
            "largest_allocatable_block": max_allocatable,
        }

    def _endpoint_effective_workspace(
        self,
        session: Session,
        endpoint: Endpoint,
        *,
        groups: Mapping[str, ServerGroup] | None = None,
    ) -> str | None:
        group = None
        if endpoint.server_group_id is not None:
            if groups is not None:
                group = groups.get(endpoint.server_group_id)
            else:
                group = session.get(ServerGroup, endpoint.server_group_id)
        return resolve_effective_workspace(
            override=endpoint.workspace_path,
            group_workspace=group.workspace_path if group is not None else None,
        )

    @staticmethod
    def _require_effective_workspace(
        *,
        override: str | None,
        group: ServerGroup | None,
    ) -> str:
        value = resolve_effective_workspace(
            override=override,
            group_workspace=group.workspace_path if group is not None else None,
        )
        if value is None:
            raise BrokerError(
                "endpoint_workspace_required",
                "endpoint requires workspace_path or a server group default",
                status_code=422,
            )
        return value

    def collector_endpoints(self) -> list[EndpointConfig]:
        """Read current control-plane endpoint inventory for fixed-command collection."""

        def operation(session: Session) -> list[EndpointConfig]:
            values: list[EndpointConfig] = []
            endpoints = session.scalars(
                select(Endpoint)
                .where(Endpoint.lifecycle_state.in_({"active", "draining"}))
                .order_by(Endpoint.id)
            ).all()
            groups = self._server_groups_by_id(session)
            for endpoint in endpoints:
                values.append(
                    EndpointConfig(
                        id=endpoint.id,
                        host=endpoint.host,
                        port=endpoint.port,
                        ssh_user=endpoint.ssh_user,
                        ssh_alias=endpoint.ssh_alias,
                        workspace_path=self._endpoint_effective_workspace(
                            session, endpoint, groups=groups
                        ),
                        observation_profile=endpoint.observation_profile,
                        keepalive_adapter_id=endpoint.keepalive_adapter_id,
                        keepalive_policy=endpoint.keepalive_policy,
                        labels=json_load(endpoint.labels_json),
                        storage_group=endpoint.storage_group,
                        server_group_id=endpoint.server_group_id,
                        expected_gpu_count=endpoint.expected_gpu_count,
                        expected_gpu_total_vram_mib=endpoint.expected_gpu_total_vram_mib,
                        project_ids=[],
                    )
                )
            return values

        return self._read(operation)

    def collector_endpoints_due(self) -> list[EndpointConfig]:
        """Return the endpoints to probe this cycle.

        A healthy endpoint is probed every cycle. One whose collection keeps
        failing is backed off so the cycle stays as short as the hosts that do
        answer; it rejoins every cycle on its first success. Backoff changes
        only the probe rhythm, never admission: a degraded endpoint is already
        fail-closed through stale telemetry.
        """

        def operation(session: Session) -> set[str]:
            now = utcnow()
            stale_after = timedelta(seconds=self.inventory.collector.stale_after_seconds)
            backoff = timedelta(seconds=DEGRADED_ENDPOINT_PROBE_SECONDS)
            deferred: set[str] = set()
            for state in session.scalars(select(ProviderState)).all():
                if state.endpoint_id is None:
                    continue
                last_success = _as_utc(state.last_success_at)
                if last_success is not None and now - last_success <= stale_after:
                    continue
                last_attempt = _as_utc(state.last_attempt_at)
                if last_attempt is not None and now - last_attempt < backoff:
                    deferred.add(state.endpoint_id)
            return deferred

        deferred = self._read(operation)
        return [item for item in self.collector_endpoints() if item.id not in deferred]

    def collector_endpoint(self, endpoint_id: str) -> EndpointConfig:
        """Return one sealed endpoint configuration for targeted verification.

        A stop verification must remain possible after an endpoint is paused,
        so this single-endpoint path does not apply the regular collector filter.
        """

        def operation(session: Session) -> EndpointConfig:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_collectable", "endpoint does not exist", status_code=404
                )
            return EndpointConfig(
                id=endpoint.id,
                host=endpoint.host,
                port=endpoint.port,
                ssh_user=endpoint.ssh_user,
                ssh_alias=endpoint.ssh_alias,
                workspace_path=self._endpoint_effective_workspace(session, endpoint),
                observation_profile=endpoint.observation_profile,
                keepalive_adapter_id=endpoint.keepalive_adapter_id,
                keepalive_policy=endpoint.keepalive_policy,
                labels=json_load(endpoint.labels_json),
                storage_group=endpoint.storage_group,
                server_group_id=endpoint.server_group_id,
                expected_gpu_count=endpoint.expected_gpu_count,
                expected_gpu_total_vram_mib=endpoint.expected_gpu_total_vram_mib,
                project_ids=[],
            )

        return self._read(operation)

    def endpoint_reachability(self, endpoint_id: str) -> dict[str, Any]:
        """Report whether this endpoint has ever been observed, and what failed.

        Registering a host and reaching one are different events, and until
        now only the first had an answer. A registration whose SSH never
        succeeds -- an unknown host key, a withdrawn port, the wrong profile --
        was accepted, reported as created, and then sat as a red row nobody
        was told about. This is the second event, read from what collection
        already records.
        """

        def operation(session: Session) -> dict[str, Any]:
            if session.get(Endpoint, endpoint_id) is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == "raw-ssh",
                    ProviderState.endpoint_id == endpoint_id,
                )
            )
            observed_at = _as_utc(state.last_success_at) if state is not None else None
            gpu_count = (
                session.scalar(
                    select(func.count())
                    .select_from(GPUDevice)
                    .where(
                        GPUDevice.endpoint_id == endpoint_id,
                        GPUDevice.present.is_(True),
                    )
                )
                or 0
            )
            return {
                "observed": observed_at is not None,
                "observed_at": _iso(observed_at),
                "gpu_count": gpu_count,
                "error": state.last_error if state is not None else None,
            }

        return self._read(operation)

    def _write(self, operation: Callable[[Session], T]) -> T:
        """Execute one serialized SQLite write."""

        with self.database.session() as session:
            try:
                session.execute(text("BEGIN IMMEDIATE"))
                result = operation(session)
                session.commit()
                return result
            except Exception:
                session.rollback()
                raise

    def _read(self, operation: Callable[[Session], T]) -> T:
        with self.database.session() as session:
            return operation(session)

    @staticmethod
    def _bump_revision(session: Session, now: datetime) -> int:
        revision = session.get(Revision, 1)
        if revision is None:
            revision = Revision(id=1, value=0, updated_at=now)
            session.add(revision)
            session.flush()
        revision.value += 1
        revision.updated_at = now
        return revision.value

    @staticmethod
    def _revision(session: Session) -> int:
        revision = session.get(Revision, 1)
        return revision.value if revision is not None else 0

    def context_for_actor(self, actor_id: str) -> ActorContext:
        """Resolve a previously registered loopback actor."""

        def operation(session: Session) -> ActorContext:
            if actor_id == SYSTEM_ACTOR_ID:
                raise BrokerError(
                    "reserved_system_identity",
                    "the ServerPilot internal identity cannot be resolved for public use",
                    status_code=403,
                )
            actor = session.get(Actor, actor_id)
            if actor is None or not actor.enabled:
                raise BrokerError("actor_disabled", "actor is disabled", status_code=403)
            project_ids = frozenset(
                session.scalars(
                    select(ActorProject.project_id).where(ActorProject.actor_id == actor.id)
                ).all()
            )
            return ActorContext(id=actor.id, role=actor.role, project_ids=project_ids)

        return self._read(operation)

    def local_actor(self, actor_id: str) -> ActorContext:
        """Resolve a cooperative loopback label, never an administrator.

        The label is intentionally not a credential.  It records ownership so
        one local actor cannot mutate another actor's lease, request, or Slurm
        job.  Existing inventory projects are visible as cooperative project
        memberships; authenticated actors retain their explicit memberships.
        """

        normalized = actor_id.strip()
        if normalized == SYSTEM_ACTOR_ID:
            raise BrokerError(
                "reserved_system_identity",
                "the ServerPilot internal identity cannot be used as a local actor",
                status_code=403,
            )
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", normalized):
            raise BrokerError(
                "invalid_actor_name",
                "actor name must start with a letter and contain 2-128 letters, numbers, '.', '_' or '-'",
                status_code=422,
            )

        def resolve(session: Session) -> ActorContext | None:
            stored_actor = session.get(Actor, normalized)
            if stored_actor is None:
                return None
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="allocator", project_ids=project_ids)

        existing = self._read(resolve)
        if existing is not None:
            return existing

        def create(session: Session) -> ActorContext:
            stored_actor = session.get(Actor, normalized)
            if stored_actor is None:
                now = utcnow()
                stored_actor = Actor(
                    id=normalized,
                    display_name=normalized,
                    role="allocator",
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(stored_actor)
                session.flush()
            project_ids = frozenset(session.scalars(select(Project.id)).all())
            return ActorContext(id=normalized, role="allocator", project_ids=project_ids)

        return self._write(create)

    @staticmethod
    def _require_role(actor: ActorContext, allowed: set[str]) -> None:
        if actor.role not in allowed:
            raise BrokerError(
                "forbidden_role",
                f"role {actor.role} is not allowed for this operation",
                status_code=403,
            )

    @staticmethod
    def _can_manage_lease(actor: ActorContext, lease: Lease) -> bool:
        return lease.actor_id == actor.id

    @classmethod
    def _require_lease_reassignment_authorization(
        cls,
        actor: ActorContext,
        lease: Lease,
        *,
        operator_override: bool,
    ) -> None:
        """Authorize an owner move or an explicit human operator correction."""

        if operator_override:
            if actor.role not in {"operator", "admin"}:
                raise BrokerError(
                    "operator_role_required",
                    "human lease correction requires an operator role",
                    status_code=403,
                )
            return
        if not cls._can_manage_lease(actor, lease):
            raise BrokerError(
                "lease_forbidden",
                "cannot reassign another actor's GPU lease",
                status_code=403,
            )

    @staticmethod
    def _reject_generic_keepalive_mutation(lease: Lease) -> None:
        if lease.kind == "keepalive":
            raise BrokerError(
                "keepalive_requires_dedicated_operation",
                "internal keepalive ownership can only be changed by the dedicated keepalive operation",
                status_code=409,
            )

    @staticmethod
    def _active_keepalive_for_endpoint(session: Session, endpoint_id: str) -> str | None:
        return session.scalar(
            select(Lease.id)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                Lease.kind == "keepalive",
                Lease.state.in_(ACTIVE_LEASE_STATES),
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
            .limit(1)
        )

    def _endpoint_is_unreachable(
        self, session: Session, endpoint_id: str, now: datetime
    ) -> bool:
        """True when ServerPilot can no longer learn anything about this host.

        Every recovery path here asks the machine itself for proof, which is
        right for as long as the machine answers. When it stops -- a container
        destroyed from outside, a forwarded port withdrawn -- the same demand
        becomes a deadlock: the cards cannot be proved free, so the lease
        cannot be released, so the endpoint cannot be deleted, and the ledger
        goes on holding cards ServerPilot can neither hand out nor let go.

        Holding them protects nothing. ServerPilot never stops a workload, and
        it certainly cannot stop one it cannot reach; a stale ledger row keeps
        nobody off the card either. The only thing left to protect is the
        operator's ability to say what they already know. So the ledger can be
        settled, and the audit records that this is what happened rather than
        claiming the cards were observed empty.
        """

        state = session.scalar(
            select(ProviderState).where(
                ProviderState.provider == "raw-ssh",
                ProviderState.endpoint_id == endpoint_id,
            )
        )
        if state is None or state.last_error is None:
            return False
        last_success = _as_utc(state.last_success_at)
        if last_success is None:
            return True
        return now - last_success > timedelta(
            seconds=self.inventory.collector.stale_after_seconds
        )

    @staticmethod
    def _endpoint_has_active_leases(session: Session, endpoint_id: str) -> bool:
        gpu_lease_id = session.scalar(
            select(Lease.id)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                GPUDevice.endpoint_id == endpoint_id,
                Lease.state.in_(ACTIVE_LEASE_STATES),
                LeaseResource.active.is_(True),
            )
            .limit(1)
        )
        if gpu_lease_id is not None:
            return True
        commitment_lease_id = session.scalar(
            select(Lease.id)
            .join(LeaseEndpointCommitment, LeaseEndpointCommitment.lease_id == Lease.id)
            .where(
                LeaseEndpointCommitment.endpoint_id == endpoint_id,
                Lease.state.in_(ACTIVE_LEASE_STATES),
            )
            .limit(1)
        )
        return commitment_lease_id is not None

    def _settle_leases_for_deleted_endpoint(
        self,
        session: Session,
        endpoint_id: str,
        actor: ActorContext,
        *,
        now: datetime,
    ) -> list[str]:
        """Close every live lease on a server being deleted while unreachable."""

        leases = (
            session.scalars(
                select(Lease)
                .join(LeaseResource, LeaseResource.lease_id == Lease.id)
                .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
                .where(
                    Lease.state.in_(ACTIVE_LEASE_STATES),
                    LeaseResource.active.is_(True),
                    GPUDevice.endpoint_id == endpoint_id,
                )
            )
            .unique()
            .all()
        )
        settled: list[str] = []
        for lease in leases:
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = "server deleted while unreachable"
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all():
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            self._audit(
                session,
                actor_id=actor.id,
                action="lease.released",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"reason": lease.release_reason, "endpoint_id": endpoint_id},
                now=now,
            )
            settled.append(lease.id)
        if settled:
            session.flush()
        return settled

    @staticmethod
    def _purge_endpoint_restrict_history(
        session: Session, endpoint_id: str, *, now: datetime
    ) -> None:
        gpu_ids = list(
            session.scalars(select(GPUDevice.id).where(GPUDevice.endpoint_id == endpoint_id))
        )
        session.execute(
            delete(LeaseEndpointCommitment).where(LeaseEndpointCommitment.endpoint_id == endpoint_id)
        )
        if gpu_ids:
            session.execute(delete(LeaseResource).where(LeaseResource.gpu_id.in_(gpu_ids)))
        # An alert describes something that existed. Close them here so a
        # deleted server leaves no warning behind for the next reader.
        for alert in session.scalars(
            select(Alert).where(
                Alert.active.is_(True),
                or_(
                    (Alert.resource_type == "endpoint") & (Alert.resource_id == endpoint_id),
                    (Alert.resource_type == "gpu") & (Alert.resource_id.in_(gpu_ids or [""])),
                ),
            )
        ).all():
            alert.active = False
            alert.last_seen_at = now
        session.flush()

    @staticmethod
    def _can_manage_endpoint(actor: ActorContext, _endpoint: Endpoint) -> bool:
        # Endpoint inventory is a shared loopback control-plane resource.  The
        # actor label records who made the change for audit, but endpoint
        # lifecycle operations are intentionally not project-permission gated.
        return actor.role in MUTATING_ROLES

    @classmethod
    def _require_endpoint_manager(cls, actor: ActorContext, endpoint: Endpoint) -> None:
        if not cls._can_manage_endpoint(actor, endpoint):
            raise BrokerError(
                "endpoint_forbidden",
                "this actor cannot manage server inventory",
                status_code=403,
            )

    # ---- audit, idempotency and serialisation ---------------------------------

    def _audit(
        self,
        session: Session,
        *,
        actor_id: str | None,
        action: str,
        resource_type: str,
        resource_id: str,
        result: str,
        before: Any = None,
        after: Any = None,
        summary: dict[str, Any] | None = None,
        now: datetime,
    ) -> AuditEvent:
        event = AuditEvent(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            before_json=json_dump(before) if before is not None else None,
            after_json=json_dump(after) if after is not None else None,
            summary_json=json_dump(summary or {}),
            created_at=now,
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _resolve_lease_alerts(session: Session, lease_id: str, now: datetime) -> int:
        """Close a lease's alerts once it can no longer own resources."""

        alerts = session.scalars(
            select(Alert).where(
                Alert.alert_type.in_(LEASE_SCOPED_ALERT_TYPES),
                Alert.resource_type == "lease",
                Alert.resource_id == lease_id,
                Alert.active.is_(True),
            )
        ).all()
        for alert in alerts:
            alert.active = False
            alert.last_seen_at = now
        return len(alerts)

    @staticmethod
    def _resolve_idle_lease_alert(session: Session, lease_id: str, now: datetime) -> int:
        """Close the idle warning as soon as the lease is working again.

        Kept separate from the blocking lease alerts: an idle lease still holds
        valid resources and never blocks admission, so it must not be swept by
        the terminal-lease repair path.
        """

        alerts = session.scalars(
            select(Alert).where(
                Alert.alert_type == "idle_lease",
                Alert.resource_type == "lease",
                Alert.resource_id == lease_id,
                Alert.active.is_(True),
            )
        ).all()
        for alert in alerts:
            alert.active = False
            alert.last_seen_at = now
        return len(alerts)

    @classmethod
    def _resolve_orphaned_alerts(cls, session: Session, now: datetime) -> int:
        """Close alerts whose subject no longer exists.

        An alert outlives what it describes when a lease ends or a server is
        deleted. Nothing else closes those, so the state page accumulates
        warnings about resources that are gone and the real ones stop standing
        out. The subject's own existence is the rule; no alert type is special.
        """

        resolved = cls._resolve_deleted_resource_alerts(session, now)
        alerts = session.scalars(
            select(Alert).where(
                Alert.alert_type.in_(LEASE_SCOPED_ALERT_TYPES),
                Alert.resource_type == "lease",
                Alert.active.is_(True),
            )
        ).all()
        if not alerts:
            return resolved
        lease_ids = {alert.resource_id for alert in alerts}
        leases = {
            lease.id: lease
            for lease in session.scalars(select(Lease).where(Lease.id.in_(lease_ids))).all()
        }
        live_resource_lease_ids = set(
            session.scalars(
                select(LeaseResource.lease_id).where(
                    LeaseResource.lease_id.in_(lease_ids),
                    LeaseResource.active.is_(True),
                )
            ).all()
        )
        for lease_id in lease_ids:
            lease = leases.get(lease_id)
            if (
                lease is None
                or lease.state in TERMINAL_LEASE_STATES
                or lease_id not in live_resource_lease_ids
            ):
                resolved += cls._resolve_lease_alerts(session, lease_id, now)
        return resolved

    @staticmethod
    def _resolve_deleted_resource_alerts(session: Session, now: datetime) -> int:
        """Close alerts about an endpoint or GPU row that no longer exists."""

        resolved = 0
        for resource_type, model in (("endpoint", Endpoint), ("gpu", GPUDevice)):
            alerts = session.scalars(
                select(Alert).where(
                    Alert.resource_type == resource_type,
                    Alert.active.is_(True),
                )
            ).all()
            if not alerts:
                continue
            resource_ids = {alert.resource_id for alert in alerts}
            live = set(
                session.scalars(select(model.id).where(model.id.in_(resource_ids))).all()
            )
            for alert in alerts:
                if alert.resource_id in live:
                    continue
                alert.active = False
                alert.last_seen_at = now
                resolved += 1
        return resolved

    def _idempotent(
        self,
        session: Session,
        *,
        actor: ActorContext,
        action: str,
        key: str,
    ) -> dict[str, Any] | None:
        if not key or len(key) > 255:
            raise BrokerError(
                "idempotency_key_required",
                "a non-empty Idempotency-Key of at most 255 characters is required",
                status_code=422,
            )
        prior = session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.actor_id == actor.id,
                IdempotencyRecord.action == action,
                IdempotencyRecord.key == key,
            )
        )
        return (
            self._with_committed_revision(json_load(prior.response_json))
            if prior is not None
            else None
        )

    @staticmethod
    def _with_committed_revision(response: dict[str, Any]) -> dict[str, Any]:
        revision = response.get("snapshot_revision")
        if revision is not None:
            response.setdefault("committed", {"snapshot_revision": revision})
        return response

    def _remember_idempotency(
        self,
        session: Session,
        *,
        actor: ActorContext,
        action: str,
        key: str,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        self._with_committed_revision(response)
        session.add(
            IdempotencyRecord(
                actor_id=actor.id,
                action=action,
                key=key,
                response_json=json_dump(response),
                created_at=now,
            )
        )

    @staticmethod
    def _plugin_capacity_key(endpoint_id: str) -> str:
        digest = hashlib.sha256(endpoint_id.encode("utf-8")).hexdigest()[:60]
        return f"{PLUGIN_CAPACITY_SETTING_PREFIX}{digest}"

    @staticmethod
    def _collector_version_key(endpoint_id: str) -> str:
        digest = hashlib.sha256(endpoint_id.encode("utf-8")).hexdigest()[:60]
        return f"{COLLECTOR_VERSION_SETTING_PREFIX}{digest}"

    def _persist_collector_implementation_version(
        self,
        session: Session,
        endpoint_id: str,
        version: str | None,
        *,
        now: datetime,
    ) -> None:
        key = self._collector_version_key(endpoint_id)
        setting = session.get(RuntimeSetting, key)
        if version is None:
            if setting is not None:
                session.delete(setting)
            return
        if setting is None:
            session.add(RuntimeSetting(key=key, value=version, updated_at=now))
            return
        setting.value = version
        setting.updated_at = now

    @staticmethod
    def _encode_plugin_capacity(capacity: Mapping[str, Any]) -> str:
        payload: dict[str, Any] = {}
        for key in (
            "free_gpu_count",
            "gpu_name",
            "largest_free_block",
            "vram_mib",
            "max_gpus_per_lease",
            "cpu_cores_per_gpu",
            "memory_mib_per_gpu",
            "note",
        ):
            if key in capacity:
                payload[key] = capacity[key]
        return json_dump(payload)

    @staticmethod
    def _decode_plugin_capacity(value: str | None) -> dict[str, Any] | None:
        if not value:
            return None
        if value.startswith("{"):
            try:
                payload = json_load(value)
            except ValueError:
                return None
            if not isinstance(payload, dict):
                return None
            return BrokerService._validated_plugin_capacity(payload)
        if "|" not in value:
            return None
        count_text, name = value.split("|", 1)
        try:
            count = int(count_text)
        except ValueError:
            return None
        if count < 0 or not name:
            return None
        return {"free_gpu_count": count, "gpu_name": name}

    @staticmethod
    def _validated_plugin_capacity(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "free_gpu_count",
            "gpu_name",
            "largest_free_block",
            "vram_mib",
            "max_gpus_per_lease",
            "cpu_cores_per_gpu",
            "memory_mib_per_gpu",
            "note",
        }
        if set(payload) - allowed:
            return None
        count = payload.get("free_gpu_count")
        name = payload.get("gpu_name")
        if type(count) is not int or isinstance(count, bool) or count < 0:
            return None
        if not isinstance(name, str) or not name:
            return None
        result: dict[str, Any] = {"free_gpu_count": count, "gpu_name": name}
        if "largest_free_block" in payload:
            largest = payload["largest_free_block"]
            if type(largest) is not int or isinstance(largest, bool) or largest < 0 or largest > count:
                return None
            result["largest_free_block"] = largest
        for key, minimum in (
            ("vram_mib", 1),
            ("max_gpus_per_lease", 1),
            ("cpu_cores_per_gpu", 1),
            ("memory_mib_per_gpu", 1),
        ):
            if key not in payload:
                continue
            value = payload[key]
            if type(value) is not int or isinstance(value, bool) or value < minimum:
                return None
            result[key] = value
        if "note" in payload:
            note = payload["note"]
            if not isinstance(note, str) or not note:
                return None
            result["note"] = note
        return result

    def _persist_plugin_capacity(
        self,
        session: Session,
        endpoint: Endpoint,
        capacity: Mapping[str, Any] | None,
        *,
        now: datetime,
    ) -> None:
        key = self._plugin_capacity_key(endpoint.id)
        setting = session.get(RuntimeSetting, key)
        if capacity is None:
            from serverpilot.plugins import get_plugin, is_plugin_profile

            if setting is None:
                return
            if not is_plugin_profile(endpoint.observation_profile):
                return
            plugin = get_plugin(endpoint.observation_profile)
            if plugin is None or "apply" not in plugin.capabilities:
                return
            session.delete(setting)
            return
        value = self._encode_plugin_capacity(capacity)
        if setting is None:
            session.add(RuntimeSetting(key=key, value=value, updated_at=now))
            return
        setting.value = value
        setting.updated_at = now

    @staticmethod
    def _request_resource_constraints(request: AllocationRequest) -> ResourceConstraints:
        payload = json_load(request.constraints_json)
        if isinstance(payload, dict):
            payload = {
                key: value for key, value in payload.items() if key != "plugin_allocation"
            }
        return ResourceConstraints.model_validate(payload)

    @staticmethod
    def _plugin_allocation_payload(request: AllocationRequest | None) -> dict[str, str] | None:
        if request is None:
            return None
        constraints = json_load(request.constraints_json)
        if not isinstance(constraints, dict):
            return None
        allocation = constraints.get("plugin_allocation")
        if not isinstance(allocation, dict):
            return None
        plugin_id = allocation.get("plugin_id")
        allocation_ref = allocation.get("allocation_ref")
        if isinstance(plugin_id, str) and isinstance(allocation_ref, str):
            return {"plugin_id": plugin_id, "allocation_ref": allocation_ref}
        return None

    def _release_plugin_allocations(self, allocations: list[dict[str, str]]) -> None:
        """Release cluster allocations, then record any the plugin would not give up.

        This is the automatic path: idle reclaim and purging during observation,
        where no caller is waiting to be told. An explicit release refuses with
        ``plugin_release_failed`` instead, but reclaim cannot refuse, so a
        failure here drops the last local reference to a job still running
        against the user's cluster quota. There is no logger in this service, so
        the audit trail is the only place a human or an agent can find the leak.

        Every entry is still attempted: one uncooperative plugin must not keep
        the other allocations alive.
        """

        from serverpilot.plugins import PluginError, release_plugin

        seen: set[tuple[str, str]] = set()
        failures: list[tuple[str, str, str]] = []
        for allocation in allocations:
            plugin_id = allocation.get("plugin_id")
            allocation_ref = allocation.get("allocation_ref")
            if not isinstance(plugin_id, str) or not isinstance(allocation_ref, str):
                continue
            key = (plugin_id, allocation_ref)
            if key in seen:
                continue
            seen.add(key)
            try:
                release_plugin(plugin_id, allocation_ref=allocation_ref)
            except PluginError as error:
                failures.append((plugin_id, allocation_ref, str(error)))
        if failures:
            self._record_plugin_release_failures(failures)

    def _record_plugin_release_failures(self, failures: list[tuple[str, str, str]]) -> None:
        def operation(session: Session) -> None:
            now = utcnow()
            for plugin_id, allocation_ref, reason in failures:
                self._audit(
                    session,
                    actor_id=SYSTEM_ACTOR_ID,
                    action="plugin.release_failed",
                    resource_type="cluster",
                    resource_id=allocation_ref,
                    result="failure",
                    summary={"plugin_id": plugin_id, "reason": reason},
                    now=now,
                )

        self._write(operation)

    def _purge_unobserved_plugin_gpus(
        self,
        session: Session,
        *,
        endpoint: Endpoint,
        observed_gpu_ids: set[str],
        now: datetime,
    ) -> int:
        from serverpilot.plugins import get_plugin, is_plugin_profile

        if not is_plugin_profile(endpoint.observation_profile):
            return 0
        plugin = get_plugin(endpoint.observation_profile)
        if plugin is None or "apply" not in plugin.capabilities:
            return 0
        prior_gpus = session.scalars(
            select(GPUDevice).where(GPUDevice.endpoint_id == endpoint.id)
        ).all()
        removed = 0
        for gpu in prior_gpus:
            if gpu.id in observed_gpu_ids:
                continue
            resources = session.scalars(
                select(LeaseResource).where(LeaseResource.gpu_id == gpu.id)
            ).all()
            if any(resource.active for resource in resources):
                if gpu.present:
                    gpu.present = False
                    gpu.absent_at = now
                continue
            for resource in resources:
                session.delete(resource)
            session.flush()
            session.delete(gpu)
            removed += 1
        return removed

    @staticmethod
    def _endpoint_dict(
        endpoint: Endpoint,
        *,
        scheduler_capacity: dict[str, Any] | None = None,
        server_groups: Mapping[str, ServerGroup] | None = None,
    ) -> dict[str, Any]:
        group = None
        if endpoint.server_group_id is not None and server_groups is not None:
            group = server_groups.get(endpoint.server_group_id)
        effective = resolve_effective_workspace(
            override=endpoint.workspace_path,
            group_workspace=group.workspace_path if group is not None else None,
        )
        return {
            "id": endpoint.id,
            "host": endpoint.host,
            "port": endpoint.port,
            "ssh_user": endpoint.ssh_user,
            "ssh_alias": endpoint.ssh_alias,
            "workspace_path": effective,
            "workspace_path_override": endpoint.workspace_path,
            "server_group_id": endpoint.server_group_id,
            "observation_profile": endpoint.observation_profile,
            "keepalive_adapter_id": endpoint.keepalive_adapter_id,
            "keepalive_policy": endpoint.keepalive_policy,
            "labels": json_load(endpoint.labels_json),
            "storage_group": endpoint.storage_group,
            "expected_gpu_count": endpoint.expected_gpu_count,
            "expected_gpu_total_vram_mib": endpoint.expected_gpu_total_vram_mib,
            "resource_kind": endpoint.resource_kind,
            "scheduler_capacity": scheduler_capacity,
            "owner_project_id": endpoint.owner_project_id,
            "lifecycle_state": endpoint.lifecycle_state,
            "enabled": endpoint.lifecycle_state == "active",
            "created_at": _iso(endpoint.created_at),
            "updated_at": _iso(endpoint.updated_at),
        }

    def _project_endpoint(
        self,
        session: Session,
        endpoint: Endpoint,
        *,
        scheduler_capacity: dict[str, Any] | None = None,
        server_groups: Mapping[str, ServerGroup] | None = None,
    ) -> dict[str, Any]:
        groups = server_groups if server_groups is not None else self._server_groups_by_id(session)
        return self._endpoint_dict(
            endpoint,
            scheduler_capacity=scheduler_capacity,
            server_groups=groups,
        )

    @staticmethod
    def _request_dict(request: AllocationRequest) -> dict[str, Any]:
        return {
            "id": request.id,
            "actor_id": request.actor_id,
            "project_id": request.project_id,
            "task_ref": request.task_ref,
            "purpose": request.purpose,
            "constraints": json_load(request.constraints_json),
            "duration_seconds": request.duration_seconds,
            "start_after": _iso(request.start_after),
            "deadline": _iso(request.deadline),
            "approval_ref": request.approval_ref,
            "state": request.state,
            "priority_class": request.priority_class,
            "blocked_reason": request.blocked_reason,
            "created_at": _iso(request.created_at),
            "updated_at": _iso(request.updated_at),
        }

    def _lease_dict(
        self,
        session: Session,
        lease: Lease,
        *,
        resources: list[LeaseResource] | None = None,
        bindings: list[WorkloadBinding] | None = None,
        request: AllocationRequest | None = None,
    ) -> dict[str, Any]:
        if resources is None:
            resources = session.scalars(
                select(LeaseResource)
                .where(LeaseResource.lease_id == lease.id)
                .order_by(LeaseResource.gpu_id)
            ).all()
        if bindings is None:
            bindings = session.scalars(
                select(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id)
            ).all()
        if request is None:
            request = session.get(AllocationRequest, lease.request_id)
        active_resources = [resource for resource in resources if resource.active]
        gpu_by_id = (
            {
                gpu.id: gpu
                for gpu in session.scalars(
                    select(GPUDevice).where(
                        GPUDevice.id.in_([resource.gpu_id for resource in active_resources])
                    )
                ).all()
            }
            if active_resources
            else {}
        )
        endpoints = (
            {
                endpoint.id: endpoint
                for endpoint in session.scalars(
                    select(Endpoint).where(
                        Endpoint.id.in_({gpu.endpoint_id for gpu in gpu_by_id.values()})
                    )
                ).all()
            }
            if gpu_by_id
            else {}
        )
        commitments = {
            commitment.endpoint_id: commitment
            for commitment in session.scalars(
                select(LeaseEndpointCommitment).where(LeaseEndpointCommitment.lease_id == lease.id)
            ).all()
        }
        by_endpoint: dict[str, list[GPUDevice]] = defaultdict(list)
        absent_gpu_ids: list[str] = []
        for resource in active_resources:
            gpu = gpu_by_id.get(resource.gpu_id)
            if gpu is not None:
                if gpu.present:
                    by_endpoint[gpu.endpoint_id].append(gpu)
                else:
                    absent_gpu_ids.append(gpu.id)
        executable_resources: list[dict[str, Any]] = []
        if not absent_gpu_ids:
            for endpoint_id, gpus in sorted(by_endpoint.items()):
                endpoint = endpoints.get(endpoint_id)
                if endpoint is None:
                    continue
                gpus.sort(
                    key=lambda item: (
                        item.cuda_ordinal if item.cuda_ordinal is not None else 1025,
                        item.gpu_index,
                    )
                )
                if any(gpu.cuda_ordinal is None for gpu in gpus):
                    continue
                selectors = [str(gpu.cuda_ordinal) for gpu in gpus]
                # Admission rejects unsafe selectors, but never emit an unvalidated
                # value should historical telemetry predate that invariant.
                if not all(CUDA_SELECTOR_RE.fullmatch(selector) for selector in selectors):
                    continue
                commitment = commitments.get(endpoint_id)
                executable_resources.append(
                    {
                        "endpoint": {
                            "id": endpoint.id,
                            "host": endpoint.host,
                            "port": endpoint.port,
                            "ssh_user": endpoint.ssh_user,
                            "workspace_path": self._endpoint_effective_workspace(
                                session, endpoint
                            ),
                        },
                        "gpus": [
                            {
                                "id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "gpu_index": gpu.gpu_index,
                                "cuda_ordinal": gpu.cuda_ordinal,
                            }
                            for gpu in gpus
                        ],
                        "cuda_visible_devices": ",".join(selectors),
                        "cuda_device_order": "PCI_BUS_ID",
                        "commitment": {
                            "cpu_cores": commitment.cpu_cores if commitment else 0.0,
                            "memory_mib": commitment.memory_mib if commitment else 0,
                        },
                    }
                )
        allocation = None
        if request is not None:
            constraints = json_load(request.constraints_json)
            if isinstance(constraints, dict) and isinstance(constraints.get("plugin_allocation"), dict):
                allocation = constraints["plugin_allocation"]
        if allocation:
            ssh = allocation.get("ssh") if isinstance(allocation.get("ssh"), dict) else {}
            for resource in executable_resources:
                endpoint_payload = resource.get("endpoint")
                if isinstance(endpoint_payload, dict):
                    if isinstance(allocation.get("workspace_path"), str):
                        endpoint_payload["workspace_path"] = allocation["workspace_path"]
                    if isinstance(ssh.get("host"), str):
                        endpoint_payload["host"] = ssh["host"]
                    if type(ssh.get("port")) is int:
                        endpoint_payload["port"] = ssh["port"]
                    if isinstance(ssh.get("user"), str):
                        endpoint_payload["ssh_user"] = ssh["user"]
                if isinstance(allocation.get("cuda_visible_devices"), str):
                    resource["cuda_visible_devices"] = allocation["cuda_visible_devices"]
        return {
            "id": lease.id,
            "request_id": lease.request_id,
            "actor_id": lease.actor_id,
            "project_id": lease.project_id,
            "kind": lease.kind,
            "state": lease.state,
            "gpu_ids": [resource.gpu_id for resource in active_resources],
            "absent_gpu_ids": absent_gpu_ids,
            "resources": executable_resources,
            "issued_at": _iso(lease.issued_at),
            "expires_at": _iso(lease.expires_at),
            "last_heartbeat_at": _iso(lease.last_heartbeat_at),
            "activated_at": _iso(lease.activated_at),
            "released_at": _iso(lease.released_at),
            "release_reason": lease.release_reason,
            "issued_revision": lease.issued_revision,
            "task_ref": request.task_ref if request else None,
            "purpose": request.purpose if request else None,
            "workloads": (
                []
                if lease.kind == "keepalive"
                else [
                    {"run_id": binding.run_id, "process_keys": json_load(binding.process_keys_json)}
                    for binding in bindings
                ]
            ),
        }

    @staticmethod
    def _alert_dict(alert: Alert) -> dict[str, Any]:
        return {
            "id": alert.id,
            "type": alert.alert_type,
            "severity": alert.severity,
            "resource_type": alert.resource_type,
            "resource_id": alert.resource_id,
            "message": alert.message,
            "active": alert.active,
            "first_seen_at": _iso(alert.first_seen_at),
            "last_seen_at": _iso(alert.last_seen_at),
            "acknowledged_at": _iso(alert.acknowledged_at),
            "acknowledged_by": alert.acknowledged_by,
        }

    @staticmethod
    def _reservation_dict(reservation: Reservation) -> dict[str, Any]:
        return {
            "id": reservation.id,
            "actor_id": reservation.actor_id,
            "project_id": reservation.project_id,
            "gpu_ids": json_load(reservation.gpu_ids_json),
            "constraints": json_load(reservation.constraints_json),
            "start_at": _iso(reservation.start_at),
            "end_at": _iso(reservation.end_at),
            "reason": reservation.reason,
            "state": reservation.state,
            "created_at": _iso(reservation.created_at),
        }

    @staticmethod
    def _maintenance_dict(window: MaintenanceWindow) -> dict[str, Any]:
        return {
            "id": window.id,
            "endpoint_id": window.endpoint_id,
            "gpu_id": window.gpu_id,
            "actor_id": window.actor_id,
            "start_at": _iso(window.start_at),
            "end_at": _iso(window.end_at),
            "reason": window.reason,
            "state": window.state,
            "created_at": _iso(window.created_at),
        }

    # ---- read models and derived GPU state ------------------------------------

    def _latest_telemetry(
        self, session: Session, gpu_id: str
    ) -> TelemetryCurrent | TelemetrySnapshot | None:
        current = session.get(TelemetryCurrent, gpu_id)
        if current is not None:
            return current
        return session.scalar(
            select(TelemetrySnapshot)
            .where(TelemetrySnapshot.gpu_id == gpu_id)
            .order_by(TelemetrySnapshot.observed_at.desc(), TelemetrySnapshot.id.desc())
            .limit(1)
        )

    @staticmethod
    def _telemetry_dict(
        telemetry: TelemetryCurrent | TelemetrySnapshot | None,
    ) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "observed_at": _iso(telemetry.observed_at),
            "collected_at": _iso(telemetry.collected_at),
            "memory_used_mib": telemetry.memory_used_mib,
            "memory_free_mib": telemetry.memory_free_mib,
            "gpu_utilization_pct": telemetry.gpu_utilization_pct,
            "memory_utilization_pct": telemetry.memory_utilization_pct,
            "temperature_c": telemetry.temperature_c,
            "power_watts": telemetry.power_watts,
            "pstate": telemetry.pstate,
            "health": telemetry.health,
            "provider": telemetry.provider,
        }

    @staticmethod
    def _recent_telemetry_average(
        gpu: GPUDevice,
        samples: list[TelemetryCurrent | TelemetrySnapshot],
    ) -> dict[str, Any] | None:
        """Summarize the recent observed history for one GPU.

        The current sample is included only when it is newer than the last
        persisted history point, matching :meth:`endpoint_history` and
        avoiding a duplicate observation in the rolling average.
        """

        if not samples:
            return None

        def average(name: str) -> float | None:
            values = [getattr(sample, name) for sample in samples]
            present = [float(value) for value in values if value is not None]
            return round(sum(present) / len(present), 2) if present else None

        memory_used_mib = average("memory_used_mib")
        return {
            "window_seconds": TELEMETRY_RECENT_AVERAGE_WINDOW_SECONDS,
            "sample_count": len(samples),
            "first_observed_at": _iso(samples[0].observed_at),
            "last_observed_at": _iso(samples[-1].observed_at),
            "memory_used_mib": memory_used_mib,
            "memory_free_mib": average("memory_free_mib"),
            "memory_used_pct": (
                round(memory_used_mib * 100 / gpu.total_vram_mib, 2)
                if memory_used_mib is not None and gpu.total_vram_mib > 0
                else None
            ),
            "gpu_utilization_pct": average("gpu_utilization_pct"),
            "memory_utilization_pct": average("memory_utilization_pct"),
            "temperature_c": average("temperature_c"),
        }

    @staticmethod
    def _recent_host_telemetry_average(
        samples: list[EndpointTelemetryCurrent | EndpointTelemetrySnapshot],
    ) -> dict[str, Any] | None:
        """Summarize one endpoint's host pressure over the standard 10-minute window."""

        if not samples:
            return None

        def average(values: list[float | None]) -> float | None:
            present = [value for value in values if value is not None]
            return round(sum(present) / len(present), 2) if present else None

        cpu_utilization_pct = average(
            [sample.cpu_utilization_pct for sample in samples]
        )
        cpu_load_fraction = (
            round(cpu_utilization_pct / 100, 2) if cpu_utilization_pct is not None else None
        )
        memory_used_pct = average(
            [BrokerService._host_memory_used_pct(sample) for sample in samples]
        )
        return {
            "window_seconds": TELEMETRY_RECENT_AVERAGE_WINDOW_SECONDS,
            "sample_count": len(samples),
            "first_observed_at": _iso(samples[0].observed_at),
            "last_observed_at": _iso(samples[-1].observed_at),
            "cpu_utilization_pct": cpu_utilization_pct,
            "cpu_load_fraction": cpu_load_fraction,
            "memory_used_pct": memory_used_pct,
        }

    @staticmethod
    def _host_capacity(
        sample: EndpointTelemetryCurrent | EndpointTelemetrySnapshot,
    ) -> dict[str, Any]:
        """Resolve how much CPU and memory this endpoint may actually use.

        A container reports the whole node's CPU count and MemTotal, so those
        readings describe the machine rather than the share this endpoint owns.
        The cgroup budget is that share; the host figures stand in only where no
        cgroup limit is in force.  Every surface reads capacity from here so the
        machine-wide reading is never mistaken for the endpoint's own.
        """

        quota = sample.cpu_quota_usec
        period = sample.cpu_period_usec
        if quota is not None and quota > 0 and period is not None and period > 0:
            cpu_scope = "container"
            cpu_cores = round(quota / period, 2)
        else:
            cpu_scope = "host"
            cpu_cores = float(sample.cpu_count)
        utilization = sample.cpu_utilization_pct
        if utilization is not None:
            cpu_available_cores = round(max(cpu_cores * (1 - utilization / 100), 0.0), 2)
        elif cpu_scope == "host":
            # The 1-minute load average is machine-wide, so it only describes an
            # endpoint that owns the whole machine.
            cpu_available_cores = round(max(cpu_cores - sample.load_1m, 0.0), 2)
        else:
            cpu_available_cores = None

        limit = sample.memory_limit_mib
        current = sample.memory_current_mib
        if limit is not None and limit > 0 and current is not None:
            memory_scope = "container"
            memory_total_mib = limit
            memory_used_mib = min(current, limit)
        else:
            memory_scope = "host"
            memory_total_mib = sample.memory_total_mib
            memory_used_mib = max(sample.memory_total_mib - sample.memory_available_mib, 0)
        return {
            "cpu_scope": cpu_scope,
            "cpu_cores": cpu_cores,
            "cpu_available_cores": cpu_available_cores,
            "memory_scope": memory_scope,
            "memory_total_mib": memory_total_mib,
            "memory_used_mib": memory_used_mib,
            "memory_available_mib": memory_total_mib - memory_used_mib,
        }

    @staticmethod
    def _host_memory_used_pct(
        sample: EndpointTelemetryCurrent | EndpointTelemetrySnapshot,
    ) -> float | None:
        capacity = BrokerService._host_capacity(sample)
        total = capacity["memory_total_mib"]
        if total > 0:
            return capacity["memory_used_mib"] * 100 / total
        return None

    @staticmethod
    def _host_telemetry_dict(
        telemetry: EndpointTelemetryCurrent | EndpointTelemetrySnapshot | None,
    ) -> dict[str, Any] | None:
        if telemetry is None:
            return None
        return {
            "observed_at": _iso(telemetry.observed_at),
            "collected_at": _iso(telemetry.collected_at),
            "cpu_count": telemetry.cpu_count,
            "load_1m": telemetry.load_1m,
            "cpu_utilization_pct": telemetry.cpu_utilization_pct,
            "cpu_total_ticks": telemetry.cpu_total_ticks,
            "cpu_idle_ticks": telemetry.cpu_idle_ticks,
            "cpu_usage_usec": telemetry.cpu_usage_usec,
            "cpu_quota_usec": telemetry.cpu_quota_usec,
            "cpu_period_usec": telemetry.cpu_period_usec,
            "memory_total_mib": telemetry.memory_total_mib,
            "memory_available_mib": telemetry.memory_available_mib,
            "memory_limit_mib": telemetry.memory_limit_mib,
            "memory_current_mib": telemetry.memory_current_mib,
            "capacity": BrokerService._host_capacity(telemetry),
            "provider": telemetry.provider,
        }

    @staticmethod
    def _host_cpu_utilization_pct(
        previous: EndpointTelemetryCurrent | None,
        *,
        observed_at: datetime,
        cpu_count: int,
        cpu_usage_usec: int | None,
        cpu_quota_usec: int | None,
        cpu_period_usec: int | None,
    ) -> float | None:
        previous_observed_at = _as_utc(previous.observed_at) if previous is not None else None
        if (
            previous is None
            or previous_observed_at is None
            or previous.cpu_usage_usec is None
            or cpu_usage_usec is None
        ):
            return None
        elapsed_seconds = (ensure_utc(observed_at) - previous_observed_at).total_seconds()
        usage_delta = cpu_usage_usec - previous.cpu_usage_usec
        if elapsed_seconds <= 0 or usage_delta < 0:
            return None
        if cpu_quota_usec is None:
            quota_cores = float(cpu_count)
        else:
            if cpu_period_usec is None or cpu_period_usec <= 0:
                return None
            quota_cores = cpu_quota_usec / cpu_period_usec
        if quota_cores <= 0:
            return None
        used_cores = usage_delta / 1_000_000 / elapsed_seconds
        return round(used_cores / quota_cores * 100, 2)

    def _current_processes(
        self, session: Session, gpu_id: str, now: datetime
    ) -> list[ProcessObservation]:
        cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
        return session.scalars(
            select(ProcessObservation)
            .where(
                ProcessObservation.gpu_id == gpu_id,
                ProcessObservation.active.is_(True),
                ProcessObservation.last_seen_at >= cutoff,
            )
            .order_by(ProcessObservation.pid)
        ).all()

    def _resources_have_fresh_telemetry(
        self, session: Session, resources: Iterable[LeaseResource], now: datetime
    ) -> bool:
        cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
        for resource in resources:
            gpu = session.get(GPUDevice, resource.gpu_id)
            if gpu is None or not gpu.present:
                return False
            telemetry = session.get(TelemetryCurrent, resource.gpu_id)
            observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
            if observed_at is None or observed_at < cutoff:
                return False
        return True

    def _keepalive_ttl_seconds(self) -> int:
        """Return the compatibility duration stored on the internal request.

        The request schema still requires a positive duration, but keepalive
        ownership itself is persistent and therefore has no lease expiry.  A
        missed collection may change the current process state; it must never
        erase the fact that ServerPilot owns this worker.
        """

        return max(60, self.inventory.collector.stale_after_seconds * 3)

    @staticmethod
    def _keepalive_summary(lease: Lease, endpoint_id: str) -> dict[str, Any]:
        return {
            "endpoint_id": endpoint_id,
            "enabled": lease.state == "ACTIVE",
            "lease_id": lease.id,
            "state": lease.state,
        }

    def _validate_keepalive_observation(
        self,
        session: Session,
        *,
        endpoint_id: str,
        gpu_ids: Iterable[str],
        observation_not_before: datetime,
    ) -> list[ProcessObservation]:
        """Require one post-operation endpoint snapshot covering every leased GPU."""

        threshold = ensure_utc(observation_not_before)
        host = session.get(EndpointTelemetryCurrent, endpoint_id)
        host_observed_at = _as_utc(host.observed_at) if host is not None else None
        host_collected_at = _as_utc(host.collected_at) if host is not None else None
        provider_state = session.scalar(
            select(ProviderState).where(
                ProviderState.provider == "raw-ssh",
                ProviderState.endpoint_id == endpoint_id,
            )
        )
        last_success_at = (
            _as_utc(provider_state.last_success_at) if provider_state is not None else None
        )
        if (
            host_observed_at is None
            or host_collected_at is None
            or host_collected_at < threshold
            or provider_state is None
            or last_success_at is None
            or last_success_at < threshold
        ):
            raise BrokerError(
                "keepalive_observation_stale",
                "a complete endpoint observation after the keepalive operation is required",
                status_code=409,
            )
        processes: list[ProcessObservation] = []
        for gpu_id in gpu_ids:
            gpu = session.get(GPUDevice, gpu_id)
            telemetry = session.get(TelemetryCurrent, gpu_id)
            telemetry_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
            telemetry_collected_at = (
                _as_utc(telemetry.collected_at) if telemetry is not None else None
            )
            if gpu is None or gpu.endpoint_id != endpoint_id:
                raise BrokerError(
                    "keepalive_observation_incomplete",
                    "the post-operation observation did not cover every endpoint GPU",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )
            if not gpu.present:
                # A vanished GPU cannot still be running this endpoint's
                # processes. The host snapshot above already proved the
                # observation is complete and fresh.
                continue
            if (
                telemetry_at is None
                or telemetry_at != host_observed_at
                or telemetry_collected_at is None
                or telemetry_collected_at < threshold
            ):
                raise BrokerError(
                    "keepalive_observation_incomplete",
                    "the post-operation observation did not cover every endpoint GPU",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )
            processes.extend(self._current_processes(session, gpu_id, utcnow()))
        return processes

    @staticmethod
    def _validate_keepalive_worker_confirmation(
        *,
        gpu_ids: Iterable[str],
        processes_by_gpu: Mapping[str, list[ProcessObservation]],
        confirmed_worker_identities: Mapping[str, tuple[int, str]],
    ) -> None:
        """Match sealed-helper worker evidence to one fresh observed process per GPU.

        The service deliberately accepts only the narrow, internal result of
        adapter attestation: ``gpu_id -> (pid, boot_id)``.  It never treats
        this as enough by itself.  The caller must have collected after the
        helper operation, and this method requires that collection to show
        exactly one current process on every target and that it matches the
        attested identity.  The authoritative process start time remains in
        the Broker's own observation before it is persisted as expected
        keepalive identity.
        """

        target_ids = list(gpu_ids)
        if set(confirmed_worker_identities) != set(target_ids):
            raise BrokerError(
                "keepalive_confirmation_scope_mismatch",
                "sealed keepalive confirmation must cover exactly the target GPUs",
                status_code=409,
            )
        for gpu_id in target_ids:
            identity = confirmed_worker_identities[gpu_id]
            if (
                not isinstance(identity, tuple)
                or len(identity) != 2
                or type(identity[0]) is not int
                or identity[0] <= 0
                or not isinstance(identity[1], str)
                or not identity[1]
            ):
                raise BrokerError(
                    "keepalive_confirmation_invalid",
                    "sealed keepalive confirmation identity is invalid",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )
            processes = processes_by_gpu[gpu_id]
            if len(processes) != 1:
                raise BrokerError(
                    "keepalive_confirmation_conflict",
                    "fresh collection must show exactly one process per confirmed GPU",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )
            process = processes[0]
            if (process.pid, process.boot_id) != identity:
                raise BrokerError(
                    "keepalive_confirmation_mismatch",
                    "sealed keepalive worker identity does not match fresh collection",
                    status_code=409,
                    details={"gpu_id": gpu_id},
                )

    def _record_observed_keepalive(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observation_complete: bool,
        now: datetime,
    ) -> None:
        """Record liveness without turning it into expiring ownership."""

        if not observation_complete:
            return
        leases = session.scalars(
            select(Lease)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                Lease.kind == "keepalive",
                Lease.state == "ACTIVE",
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
            .distinct()
        ).all()
        for lease in leases:
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            if len(resources) != 1:
                continue
            processes = [
                process
                for resource in resources
                for process in self._current_processes(session, resource.gpu_id, now)
            ]
            resource = resources[0]
            current = session.get(KeepaliveCurrent, resource.gpu_id)
            expected_key = self._keepalive_expected_process_key(current)
            observed_keys = {self._process_key(process) for process in processes}
            if expected_key is not None and observed_keys == {expected_key}:
                lease.last_heartbeat_at = now
                lease.expires_at = None
                self._set_keepalive_current(session, resource.gpu_id, "ON", now=now)
            elif processes:
                self._set_keepalive_current(
                    session,
                    resource.gpu_id,
                    "ERROR",
                    error_reason=(
                        "占卡进程身份尚未建立"
                        if expected_key is None
                        else "检测到不属于占卡程序的进程"
                    ),
                    now=now,
                )
            else:
                self._set_keepalive_current(session, resource.gpu_id, "OFF", now=now)

    def _clear_keepalive_errors_for_assigned_workloads(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observation_complete: bool,
        now: datetime,
    ) -> None:
        """Forget only stale keepalive errors superseded by an assigned workload.

        A failed keepalive transition can leave a ``KeepaliveCurrent`` ERROR
        row after its internal lease is gone.  Once a complete, current
        endpoint observation proves that every active GPU in this endpoint's
        portion of a workload lease has an observed compute process, that
        historical display state no longer describes those GPUs. Process
        identities are telemetry facts, not workload-ownership proof, so a
        normal worker restart must not retain the obsolete keepalive error.
        Clear the current rows only; workload ownership and observed processes
        are deliberately untouched.
        """

        if not observation_complete:
            return
        session.flush()
        leases = session.scalars(
            select(Lease)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                Lease.kind == "workload",
                Lease.state == "ACTIVE",
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
        ).unique().all()
        for lease in leases:
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            gpu_by_id = {
                gpu.id: gpu
                for gpu in session.scalars(
                    select(GPUDevice).where(
                        GPUDevice.id.in_([resource.gpu_id for resource in resources])
                    )
                ).all()
            }
            endpoint_resources = [
                resource
                for resource in resources
                if gpu_by_id.get(resource.gpu_id) is not None
                and gpu_by_id[resource.gpu_id].endpoint_id == endpoint_id
            ]
            active_leases_by_gpu = {
                resource.gpu_id: self._active_lease_for_gpu(session, resource.gpu_id)
                for resource in endpoint_resources
            }
            if (
                not endpoint_resources
                or any(
                    gpu_by_id.get(resource.gpu_id) is None
                    or gpu_by_id[resource.gpu_id].endpoint_id != endpoint_id
                    or active_leases_by_gpu[resource.gpu_id] is None
                    or active_leases_by_gpu[resource.gpu_id].id != lease.id
                    for resource in endpoint_resources
                )
            ):
                continue
            if not self._resources_have_fresh_telemetry(session, endpoint_resources, now):
                continue
            processes_by_gpu = {
                resource.gpu_id: self._current_processes(session, resource.gpu_id, now)
                for resource in endpoint_resources
            }
            if any(not processes for processes in processes_by_gpu.values()):
                continue
            for resource in endpoint_resources:
                current = session.get(KeepaliveCurrent, resource.gpu_id)
                if current is not None and current.actual == "ERROR":
                    self._set_keepalive_current(session, resource.gpu_id, "OFF", now=now)

    # ---- per-GPU keepalive policy and ownership --------------------------------

    @staticmethod
    def _keepalive_resources(session: Session, lease_id: str) -> list[LeaseResource]:
        return session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease_id,
                LeaseResource.active.is_(True),
            )
        ).all()

    def _keepalive_gpu_status(
        self,
        session: Session,
        gpu: GPUDevice,
        lease: Lease | None,
        now: datetime,
    ) -> tuple[str, str | None]:
        """Describe keepalive ownership without exposing the system actor.

        Freshness is the first rule. A card the collector cannot currently see
        says nothing about whether our keeper is still running, so the honest
        answer is ERROR rather than replaying the last recorded value.
        """

        telemetry = session.get(TelemetryCurrent, gpu.id)
        observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
        cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
        if not gpu.present or observed_at is None or observed_at < cutoff:
            return "ERROR", "采集数据已过期，无法确认占卡进程是否还在运行"
        current = session.get(KeepaliveCurrent, gpu.id)
        if lease is None or lease.kind != "keepalive":
            if current is not None:
                return current.actual, current.error_reason
            return "OFF", None
        resources = self._keepalive_resources(session, lease.id)
        if len(resources) != 1 or resources[0].gpu_id != gpu.id:
            return "ERROR", "占卡记录没有准确对应这一张 GPU"
        if lease.state != "ACTIVE":
            return "ERROR", "占卡记录未处于可用状态"
        if current is not None:
            return current.actual, current.error_reason
        if self._current_processes(session, gpu.id, now):
            return "ERROR", "占卡进程身份尚未建立"
        return "OFF", None

    @staticmethod
    def _set_keepalive_current(
        session: Session,
        gpu_id: str,
        actual: str,
        *,
        error_reason: str | None = None,
        expected_process: ProcessObservation | None = None,
        clear_expected_process: bool = False,
        now: datetime,
    ) -> None:
        if actual not in {"ON", "OFF", "ERROR"}:
            raise ValueError("invalid keepalive actual state")
        current = session.get(KeepaliveCurrent, gpu_id)
        if current is None:
            session.add(
                KeepaliveCurrent(
                    gpu_id=gpu_id,
                    actual=actual,
                    error_reason=error_reason,
                    expected_pid=(expected_process.pid if expected_process is not None else None),
                    expected_boot_id=(
                        expected_process.boot_id if expected_process is not None else None
                    ),
                    expected_process_started_at=(
                        expected_process.process_started_at
                        if expected_process is not None
                        else None
                    ),
                    updated_at=now,
                )
            )
            return
        current.actual = actual
        current.error_reason = error_reason
        if clear_expected_process:
            current.expected_pid = None
            current.expected_boot_id = None
            current.expected_process_started_at = None
        elif expected_process is not None:
            current.expected_pid = expected_process.pid
            current.expected_boot_id = expected_process.boot_id
            current.expected_process_started_at = expected_process.process_started_at
        current.updated_at = now

    @staticmethod
    def _keepalive_expected_process_key(current: KeepaliveCurrent | None) -> str | None:
        if (
            current is None
            or current.expected_pid is None
            or current.expected_boot_id is None
            or current.expected_process_started_at is None
        ):
            return None
        started = _as_utc(current.expected_process_started_at)
        assert started is not None
        return (
            f"{current.gpu_id}:{current.expected_pid}:"
            f"{current.expected_boot_id}:{int(started.timestamp())}"
        )

    @staticmethod
    def _policy_keepalive_status(
        endpoint: Endpoint,
        gpu_state: str,
        state: str,
        reason: str | None,
    ) -> tuple[str, str | None]:
        """Keep desired policy independent from the current worker state."""

        return state, reason

    @staticmethod
    def _gpu_payload_is_publicly_available(gpu: dict[str, Any]) -> bool:
        return gpu.get("state") in {"AVAILABLE", "KEEPALIVE"}

    @classmethod
    def _gpu_public_projection(
        cls,
        gpu: dict[str, Any],
        *,
        monitor_status: str,
    ) -> dict[str, Any]:
        """Return the canonical short capacity and Chinese status projection."""

        available = cls._gpu_payload_is_publicly_available(gpu)
        keepalive = gpu.get("keepalive")
        keepalive = keepalive if isinstance(keepalive, dict) else {}
        if monitor_status in {"ERROR", "STALE"}:
            status = "连接失败"
        elif available and (gpu.get("state") == "KEEPALIVE" or keepalive.get("actual") == "ON"):
            status = "可用 · 空闲占卡"
        elif available and keepalive.get("actual") == "ERROR":
            status = f"可用 · 占卡异常：{keepalive.get('reason') or '未知原因'}"
        elif available and keepalive.get("desired") == "ON":
            status = "可用 · 占卡未运行"
        elif available:
            status = "可用 · 未开启占卡"
        elif keepalive.get("actual") == "ERROR":
            status = "占卡校验失败，暂不可申请"
        elif gpu.get("state") == "CONFLICT":
            status = "归属冲突"
        elif gpu.get("state") in {"BUSY_UNMANAGED", "ORPHANED_BUSY"}:
            status = "未归属占用"
        elif gpu.get("state") == "RUNNING_MANAGED":
            status = "任务占用"
        elif gpu.get("state") in {"HELD", "LEASED_IDLE"}:
            # Claimed, but no compute process has been observed.  The design
            # contract keeps this distinct from a running task so an idle claim
            # is visible instead of reading as work in progress.
            status = "占卡"
        elif gpu.get("lease") is not None:
            status = "任务占用"
        else:
            status = "连接失败"
        return {
            "publicly_available": available,
            "public_status": status,
        }

    @staticmethod
    def _keepalive_aggregate(
        endpoint: Endpoint,
        gpu_keepalive: Iterable[dict[str, Any]],
        *,
        eligible_idle_gpu_count: int,
    ) -> dict[str, Any]:
        values = list(gpu_keepalive)
        status_counts: dict[str, int] = defaultdict(int)
        reasons: list[dict[str, str]] = []
        for item in values:
            status = str(item["state"])
            status_counts[status] += 1
            reason = item.get("reason")
            if reason:
                reasons.append({"gpu_id": str(item["gpu_id"]), "reason": str(reason)})

        active = status_counts["ON"]
        errors = status_counts["ERROR"]
        if errors:
            state = "ERROR"
        elif active:
            state = "ON"
        else:
            state = "OFF"
        desired = "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF"
        return {
            "configured": endpoint.keepalive_adapter_id is not None,
            "policy": endpoint.keepalive_policy,
            "desired": desired,
            "actual": state,
            "state": state,
            "active_gpu_count": active,
            "error_gpu_count": errors,
            "eligible_idle_gpu_count": eligible_idle_gpu_count,
            "reasons": reasons,
        }

    def _endpoint_keepalive_summary(
        self, session: Session, endpoint: Endpoint, now: datetime
    ) -> dict[str, Any]:
        gpus = session.scalars(
            select(GPUDevice)
            .where(GPUDevice.endpoint_id == endpoint.id)
            .order_by(GPUDevice.gpu_index)
        ).all()
        statuses = []
        eligible_idle_gpu_count = 0
        for gpu in gpus:
            lease = self._active_lease_for_gpu(session, gpu.id)
            gpu_state, _gpu_reason = self._gpu_state(session, gpu, now)
            state, reason = self._keepalive_gpu_status(session, gpu, lease, now)
            state, reason = self._policy_keepalive_status(
                endpoint,
                gpu_state,
                state,
                reason,
            )
            statuses.append(
                {
                    "gpu_id": gpu.id,
                    "state": state,
                    "reason": reason,
                }
            )
            if (
                endpoint.keepalive_policy == "idle_keepalive"
                and endpoint.keepalive_adapter_id is not None
                and gpu_state == "AVAILABLE"
            ):
                eligible_idle_gpu_count += 1
        return self._keepalive_aggregate(
            endpoint,
            statuses,
            eligible_idle_gpu_count=eligible_idle_gpu_count,
        )

    def configure_keepalive_policy(
        self,
        actor: ActorContext,
        endpoint_id: str,
        policy: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist desired per-GPU policy; remote execution is deliberately external.

        Disabling does not release leases or signal helpers. Callers must first
        use the stop transitions and fresh collector evidence, preventing a
        policy write from making an unobserved helper allocatable.
        """

        self._require_role(actor, MUTATING_ROLES)
        if policy not in {"disabled", "idle_keepalive"}:
            raise BrokerError(
                "invalid_keepalive_policy",
                "keepalive policy must be disabled or idle_keepalive",
                status_code=422,
            )

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.policy.configure", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = endpoint.keepalive_policy
            before_adapter = endpoint.keepalive_adapter_id
            # ``server-script-v1`` is a sealed, code-owned helper.  Enabling
            # the user-facing occupancy action is the explicit authorization
            # to attach that helper; callers never supply an adapter, command,
            # path, or remote arguments.  This also repairs endpoints created
            # by older GUI builds that selected server-script-v1 observation
            # but left the adapter column empty.
            adapter_changed = policy == "idle_keepalive" and endpoint.keepalive_adapter_id is None
            if adapter_changed:
                endpoint.keepalive_adapter_id = "server-script-v1"
            changed = before != policy or adapter_changed
            if changed:
                endpoint.keepalive_policy = policy
                endpoint.updated_at = now
                if policy == "disabled":
                    for gpu in session.scalars(
                        select(GPUDevice).where(GPUDevice.endpoint_id == endpoint.id)
                    ).all():
                        lease = self._active_lease_for_gpu(session, gpu.id)
                        if lease is None or lease.kind != "keepalive":
                            self._set_keepalive_current(session, gpu.id, "OFF", now=now)
                revision = self._bump_revision(session, now)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="keepalive.policy_configured",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before={"policy": before, "keepalive_adapter_id": before_adapter},
                    after={
                        "policy": policy,
                        "keepalive_adapter_id": endpoint.keepalive_adapter_id,
                    },
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "changed": changed,
                "keepalive": self._endpoint_keepalive_summary(session, endpoint, now),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.policy.configure",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def list_keepalive_transitions(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Return policy-driven start/stop work without starting remote processes.

        The result is a point-in-time work list. A start is written only after
        the helper starts and a fresh observation sees its process, so this
        planner never creates a persistent half-started keepalive lease.
        """

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            endpoints = (
                [session.get(Endpoint, endpoint_id)]
                if endpoint_id is not None
                else session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
            )
            if endpoint_id is not None and endpoints[0] is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            transitions: list[dict[str, Any]] = []
            for endpoint in endpoints:
                assert endpoint is not None
                gpus = session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id == endpoint.id)
                    .order_by(GPUDevice.gpu_index)
                ).all()
                by_id = {gpu.id: gpu for gpu in gpus}
                active_leases = session.scalars(
                    select(Lease)
                    .join(LeaseResource, LeaseResource.lease_id == Lease.id)
                    .where(
                        Lease.kind == "keepalive",
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                        LeaseResource.active.is_(True),
                        LeaseResource.gpu_id.in_(by_id) if by_id else text("0 = 1"),
                    )
                    .distinct()
                ).all()
                for lease in active_leases:
                    resources = self._keepalive_resources(session, lease.id)
                    owned = [by_id[item.gpu_id] for item in resources if item.gpu_id in by_id]
                    if len(owned) != 1 or len(resources) != 1:
                        continue
                    gpu = owned[0]
                    if endpoint.keepalive_policy == "disabled":
                        transitions.append(
                            {
                                "action": "stop",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "lease_id": lease.id,
                                "state": lease.state,
                            }
                        )
                if endpoint.keepalive_policy != "idle_keepalive":
                    continue
                if endpoint.keepalive_adapter_id is None:
                    for gpu in gpus:
                        transitions.append(
                            {
                                "action": "ineligible",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "reason": "endpoint has no sealed keepalive adapter configured",
                            }
                        )
                    continue
                for gpu in gpus:
                    lease = self._active_lease_for_gpu(session, gpu.id)
                    if lease is not None:
                        current = session.get(KeepaliveCurrent, gpu.id)
                        processes = self._current_processes(session, gpu.id, now)
                        expected_key = self._keepalive_expected_process_key(current)
                        observed_keys = {self._process_key(process) for process in processes}
                        needs_confirmation = (
                            lease.kind == "keepalive"
                            and bool(processes)
                            and (
                                current is None
                                or current.actual == "ERROR"
                                or expected_key is None
                                or observed_keys != {expected_key}
                            )
                        )
                        if needs_confirmation and len(processes) == 1:
                            # A live worker with an unknown/replaced identity
                            # is never normal free capacity.  The API can only
                            # execute this after sealed helper attestation and
                            # a matching fresh collection.
                            transitions.append(
                                {
                                    "action": "recover",
                                    "endpoint_id": endpoint.id,
                                    "gpu_id": gpu.id,
                                    "gpu_uuid": gpu.gpu_uuid,
                                    "reason": "requires sealed helper attestation",
                                }
                            )
                        elif needs_confirmation:
                            transitions.append(
                                {
                                    "action": "ineligible",
                                    "endpoint_id": endpoint.id,
                                    "gpu_id": gpu.id,
                                    "gpu_uuid": gpu.gpu_uuid,
                                    "reason": "occupancy has additional processes; automatic recovery is blocked",
                                }
                            )
                        elif lease.kind == "keepalive" and (
                            lease.state != "ACTIVE" or not processes
                        ):
                            transitions.append(
                                {
                                    "action": "start",
                                    "endpoint_id": endpoint.id,
                                    "gpu_id": gpu.id,
                                    "gpu_uuid": gpu.gpu_uuid,
                                }
                            )
                        continue
                    state, reason = self._gpu_state(session, gpu, now)
                    if state == "AVAILABLE":
                        transitions.append(
                            {
                                "action": "start",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                            }
                        )
                    elif reason:
                        # Rejections are useful to a reconciliation loop but
                        # do not become executable work.
                        transitions.append(
                            {
                                "action": "ineligible",
                                "endpoint_id": endpoint.id,
                                "gpu_id": gpu.id,
                                "gpu_uuid": gpu.gpu_uuid,
                                "state": state,
                                "reason": reason,
                            }
                        )
            return {"snapshot_revision": self._revision(session), "transitions": transitions}

        return self._read(operation)

    def keepalive_transition_plan(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Compatibility alias for the daemon/API reconciliation hook."""

        return self.list_keepalive_transitions(endpoint_id)

    def desired_keepalive_candidates(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Return only currently eligible idle GPU start candidates."""

        plan = self.list_keepalive_transitions(endpoint_id)
        return {
            "snapshot_revision": plan["snapshot_revision"],
            "candidates": [item for item in plan["transitions"] if item["action"] == "start"],
        }

    def get_endpoint_keepalive_summary(self, endpoint_id: str) -> dict[str, Any]:
        """Return the public no-actor aggregate after a transition batch."""

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            return {
                "snapshot_revision": self._revision(session),
                "keepalive": self._endpoint_keepalive_summary(session, endpoint, utcnow()),
            }

        return self._read(operation)

    def set_keepalive_error(
        self,
        endpoint_id: str,
        gpu_ids: Iterable[str],
        reason: str,
    ) -> None:
        """Persist an operation failure as current state without changing policy."""

        def operation(session: Session) -> None:
            now = utcnow()
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is not None and gpu.endpoint_id == endpoint_id:
                    self._set_keepalive_current(
                        session,
                        gpu.id,
                        "ERROR",
                        error_reason=reason,
                        now=now,
                    )

        self._write(operation)

    def activate_keepalive(
        self,
        actor: ActorContext,
        endpoint_id: str,
        gpu_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Record one keepalive only after its process is freshly observed."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.activate_gpu", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            if endpoint.keepalive_policy != "idle_keepalive":
                raise BrokerError(
                    "keepalive_policy_disabled",
                    "per-GPU keepalive may start only under idle_keepalive policy",
                    status_code=409,
                )
            if endpoint.keepalive_adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint has no sealed keepalive adapter configured",
                    status_code=409,
                )
            if endpoint.lifecycle_state != "active" or not endpoint.enabled:
                raise BrokerError(
                    "endpoint_not_active",
                    "keepalive requires an active, enabled endpoint",
                    status_code=409,
                )
            gpu = session.get(GPUDevice, gpu_id)
            if gpu is None or gpu.endpoint_id != endpoint.id:
                raise BrokerError(
                    "keepalive_gpu_mismatch",
                    "GPU does not belong to the endpoint",
                    status_code=409,
                )
            existing_lease = self._active_lease_for_gpu(session, gpu.id)
            if existing_lease is not None and existing_lease.kind != "keepalive":
                raise BrokerError(
                    "keepalive_gpu_ineligible",
                    "a workload already owns the target GPU",
                    status_code=409,
                    details={"gpu_id": gpu.id},
                )
            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint_id,
                gpu_ids=[gpu.id],
                observation_not_before=observation_not_before,
            )
            if not processes:
                raise BrokerError(
                    "keepalive_process_missing",
                    "fresh collection did not find the occupancy process",
                    status_code=409,
                )
            if len(processes) != 1:
                raise BrokerError(
                    "keepalive_process_conflict",
                    "fresh collection found additional processes on the occupancy GPU",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            if existing_lease is None:
                request = AllocationRequest(
                    id=secrets.token_hex(16),
                    actor_id=SYSTEM_ACTOR_ID,
                    project_id=SYSTEM_PROJECT_ID,
                    auto_activate=False,
                    task_ref=f"keepalive:{endpoint.id}:{gpu.id}",
                    purpose="ServerPilot idle GPU keepalive",
                    constraints_json=json_dump(
                        {"gpu_count": 1, "endpoint_ids": [endpoint.id], "gpu_ids": [gpu.id]}
                    ),
                    duration_seconds=self._keepalive_ttl_seconds(),
                    expected_duration_seconds=None,
                    start_after=None,
                    deadline=None,
                    approval_ref=None,
                    state="ACTIVE",
                    priority_class="keepalive",
                    blocked_reason=None,
                    created_at=now,
                    updated_at=now,
                )
                session.add(request)
                lease = Lease(
                    id=secrets.token_hex(16),
                    request_id=request.id,
                    actor_id=SYSTEM_ACTOR_ID,
                    project_id=SYSTEM_PROJECT_ID,
                    kind="keepalive",
                    state="ACTIVE",
                    issued_at=now,
                    expires_at=None,
                    last_heartbeat_at=now,
                    activated_at=now,
                    released_at=None,
                    release_reason=None,
                    issued_revision=revision,
                )
                session.add(lease)
                session.flush()
                session.add(
                    LeaseResource(lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None)
                )
            else:
                lease = existing_lease
                resources = self._keepalive_resources(session, lease.id)
                if len(resources) != 1 or resources[0].gpu_id != gpu.id:
                    raise BrokerError(
                        "keepalive_gpu_invalid",
                        "per-GPU keepalive record does not match the target GPU",
                        status_code=409,
                    )
                lease.state = "ACTIVE"
                lease.activated_at = lease.activated_at or now
                lease.last_heartbeat_at = now
                lease.expires_at = None
                request = session.get(AllocationRequest, lease.request_id)
                if request is not None:
                    request.state = "ACTIVE"
                    request.updated_at = now
            self._set_keepalive_current(
                session,
                gpu.id,
                "ON",
                expected_process=processes[0],
                now=now,
            )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="keepalive.gpu_activated",
                resource_type="gpu",
                resource_id=gpu.id,
                result="success",
                summary={"lease_id": lease.id, "endpoint_id": endpoint.id},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "keepalive": self._keepalive_lease_summary(lease, endpoint.id, gpu.id),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.activate_gpu",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def activate_keepalives(
        self,
        actor: ActorContext,
        endpoint_id: str,
        gpu_ids: list[str],
        *,
        observation_not_before: datetime,
        idempotency_key: str,
        confirmed_worker_identities: Mapping[str, tuple[int, str]] | None = None,
    ) -> dict[str, Any]:
        """Atomically record one freshly observed keepalive for every target GPU."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="keepalive.activate_gpus", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            if endpoint.keepalive_policy != "idle_keepalive":
                raise BrokerError(
                    "keepalive_policy_disabled",
                    "per-GPU keepalive may start only under idle_keepalive policy",
                    status_code=409,
                )
            if endpoint.keepalive_adapter_id is None:
                raise BrokerError(
                    "keepalive_not_configured",
                    "endpoint has no sealed keepalive adapter configured",
                    status_code=409,
                )
            if endpoint.lifecycle_state != "active" or not endpoint.enabled:
                raise BrokerError(
                    "endpoint_not_active",
                    "keepalive requires an active, enabled endpoint",
                    status_code=409,
                )
            if not gpu_ids or len(gpu_ids) != len(set(gpu_ids)):
                raise BrokerError(
                    "keepalive_gpu_invalid",
                    "keepalive targets must be a non-empty set of GPUs",
                    status_code=409,
                )

            targets: list[tuple[GPUDevice, Lease | None]] = []
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None or gpu.endpoint_id != endpoint.id:
                    raise BrokerError(
                        "keepalive_gpu_mismatch",
                        "GPU does not belong to the endpoint",
                        status_code=409,
                    )
                existing_lease = self._active_lease_for_gpu(session, gpu.id)
                if existing_lease is not None and existing_lease.kind != "keepalive":
                    raise BrokerError(
                        "keepalive_gpu_ineligible",
                        "a workload already owns the target GPU",
                        status_code=409,
                        details={"gpu_id": gpu.id},
                    )
                if existing_lease is not None:
                    resources = self._keepalive_resources(session, existing_lease.id)
                    if len(resources) != 1 or resources[0].gpu_id != gpu.id:
                        raise BrokerError(
                            "keepalive_gpu_invalid",
                            "per-GPU keepalive record does not match the target GPU",
                            status_code=409,
                        )
                targets.append((gpu, existing_lease))

            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint_id,
                gpu_ids=gpu_ids,
                observation_not_before=observation_not_before,
            )
            observed_gpu_ids = {process.gpu_id for process in processes}
            missing_gpu_ids = [gpu_id for gpu_id in gpu_ids if gpu_id not in observed_gpu_ids]
            if missing_gpu_ids:
                raise BrokerError(
                    "keepalive_process_missing",
                    "fresh collection did not find every occupancy process",
                    status_code=409,
                    details={"gpu_ids": missing_gpu_ids},
                )
            processes_by_gpu: dict[str, list[ProcessObservation]] = defaultdict(list)
            for process in processes:
                processes_by_gpu[process.gpu_id].append(process)
            conflicted_gpu_ids = [
                gpu_id for gpu_id in gpu_ids if len(processes_by_gpu[gpu_id]) != 1
            ]
            if conflicted_gpu_ids:
                raise BrokerError(
                    "keepalive_process_conflict",
                    "fresh collection found additional processes on an occupancy GPU",
                    status_code=409,
                    details={"gpu_ids": conflicted_gpu_ids},
                )
            if confirmed_worker_identities is not None:
                self._validate_keepalive_worker_confirmation(
                    gpu_ids=gpu_ids,
                    processes_by_gpu=processes_by_gpu,
                    confirmed_worker_identities=confirmed_worker_identities,
                )

            now = utcnow()
            revision = self._bump_revision(session, now)
            keepalives: list[dict[str, Any]] = []
            event_ids: list[int] = []
            for gpu, existing_lease in targets:
                if existing_lease is None:
                    request = AllocationRequest(
                        id=secrets.token_hex(16),
                        actor_id=SYSTEM_ACTOR_ID,
                        project_id=SYSTEM_PROJECT_ID,
                        auto_activate=False,
                        task_ref=f"keepalive:{endpoint.id}:{gpu.id}",
                        purpose="ServerPilot idle GPU keepalive",
                        constraints_json=json_dump(
                            {"gpu_count": 1, "endpoint_ids": [endpoint.id], "gpu_ids": [gpu.id]}
                        ),
                        duration_seconds=self._keepalive_ttl_seconds(),
                        expected_duration_seconds=None,
                        start_after=None,
                        deadline=None,
                        approval_ref=None,
                        state="ACTIVE",
                        priority_class="keepalive",
                        blocked_reason=None,
                        created_at=now,
                        updated_at=now,
                    )
                    session.add(request)
                    lease = Lease(
                        id=secrets.token_hex(16),
                        request_id=request.id,
                        actor_id=SYSTEM_ACTOR_ID,
                        project_id=SYSTEM_PROJECT_ID,
                        kind="keepalive",
                        state="ACTIVE",
                        issued_at=now,
                        expires_at=None,
                        last_heartbeat_at=now,
                        activated_at=now,
                        released_at=None,
                        release_reason=None,
                        issued_revision=revision,
                    )
                    session.add(lease)
                    session.flush()
                    session.add(
                        LeaseResource(
                            lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None
                        )
                    )
                else:
                    lease = existing_lease
                    lease.state = "ACTIVE"
                    lease.activated_at = lease.activated_at or now
                    lease.last_heartbeat_at = now
                    lease.expires_at = None
                    request = session.get(AllocationRequest, lease.request_id)
                    if request is not None:
                        request.state = "ACTIVE"
                        request.updated_at = now
                self._set_keepalive_current(
                    session,
                    gpu.id,
                    "ON",
                    expected_process=processes_by_gpu[gpu.id][0],
                    now=now,
                )
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="keepalive.gpu_activated",
                    resource_type="gpu",
                    resource_id=gpu.id,
                    result="success",
                    summary={"lease_id": lease.id, "endpoint_id": endpoint.id},
                    now=now,
                )
                event_ids.append(event.id)
                keepalives.append(self._keepalive_lease_summary(lease, endpoint.id, gpu.id))
            result = {
                "event_ids": event_ids,
                "snapshot_revision": revision,
                "keepalives": keepalives,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="keepalive.activate_gpus",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def confirm_keepalive_workers(
        self,
        actor: ActorContext,
        endpoint_id: str,
        gpu_ids: list[str],
        *,
        confirmed_worker_identities: Mapping[str, tuple[int, str]],
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Persist a sealed-helper confirmation after matching fresh collection.

        This is the only path that may rebind a desired keepalive whose live
        worker no longer matches the last stored PID/start identity.  The API
        obtains the identities from the fixed helper protocol and must collect
        after that proof.  This service method then verifies the observed
        process itself before it can rebind the keepalive lease, so an ordinary
        process observation alone can never make a foreign workload available.

        It intentionally shares the activation transaction: a just-started
        worker and a recovered worker both become ``actual=ON`` only after the
        same exact observation checks.
        """

        return self.activate_keepalives(
            actor,
            endpoint_id,
            gpu_ids,
            observation_not_before=observation_not_before,
            idempotency_key=idempotency_key,
            confirmed_worker_identities=confirmed_worker_identities,
        )

    @staticmethod
    def _keepalive_lease_summary(lease: Lease, endpoint_id: str, gpu_id: str) -> dict[str, Any]:
        return {
            "endpoint_id": endpoint_id,
            "gpu_id": gpu_id,
            "enabled": lease.state == "ACTIVE",
            "lease_id": lease.id,
            "state": lease.state,
        }

    def prepare_keepalive_stop(
        self, actor: ActorContext, endpoint_id: str, gpu_id: str
    ) -> dict[str, Any]:
        """Resolve one per-GPU occupancy stop target."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            gpu = session.get(GPUDevice, gpu_id)
            if gpu is None or gpu.endpoint_id != endpoint.id:
                raise BrokerError(
                    "keepalive_gpu_mismatch", "GPU does not belong to the endpoint", status_code=409
                )
            lease = self._active_lease_for_gpu(session, gpu.id)
            if lease is None or lease.kind != "keepalive":
                return {
                    "event_id": None,
                    "snapshot_revision": self._revision(session),
                    "keepalive": {
                        "endpoint_id": endpoint.id,
                        "gpu_id": gpu.id,
                        "enabled": False,
                        "lease_id": None,
                        "state": "OFF",
                    },
                }
            return {
                "event_id": None,
                "snapshot_revision": self._revision(session),
                "keepalive": self._keepalive_lease_summary(lease, endpoint.id, gpu.id),
            }

        return self._read(operation)

    def finalize_keepalive_stop(
        self,
        actor: ActorContext,
        endpoint_id: str,
        lease_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Release one stopped GPU occupancy worker."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            if idempotency_key is not None:
                existing = self._idempotent(
                    session,
                    actor=actor,
                    action="keepalive.finalize_stop_gpu",
                    key=idempotency_key,
                )
                if existing is not None:
                    return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            lease = session.get(Lease, lease_id)
            if lease is None or lease.kind != "keepalive":
                raise BrokerError(
                    "keepalive_not_found", "keepalive lease does not exist", status_code=404
                )
            if lease.state not in {"HELD", "ACTIVE", "CONFLICT", "ORPHANED_BUSY"}:
                raise BrokerError(
                    "keepalive_not_stoppable",
                    "keepalive lease is already terminal",
                    status_code=409,
                )
            resources = self._keepalive_resources(session, lease.id)
            if len(resources) != 1:
                raise BrokerError(
                    "keepalive_gpu_invalid",
                    "keepalive lease has an invalid active resource scope",
                    status_code=409,
                )
            gpus = [session.get(GPUDevice, resource.gpu_id) for resource in resources]
            if any(gpu is None or gpu.endpoint_id != endpoint.id for gpu in gpus):
                raise BrokerError(
                    "keepalive_endpoint_mismatch",
                    "keepalive lease does not belong to this endpoint",
                    status_code=409,
                )
            gpu_ids = [resource.gpu_id for resource in resources]
            processes = self._validate_keepalive_observation(
                session,
                endpoint_id=endpoint.id,
                gpu_ids=gpu_ids,
                observation_not_before=observation_not_before,
            )
            if processes:
                raise BrokerError(
                    "keepalive_process_still_running",
                    "keepalive ownership remains held while target processes are observed",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = "keepalive stopped and target GPU observed empty"
            for resource in resources:
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            # This is the one validated stop boundary: fresh collection proved
            # the target empty, so retaining the old PID/start identity would
            # make a later keeper restart look foreign.
            self._set_keepalive_current(
                session,
                gpu_ids[0],
                "OFF",
                clear_expected_process=True,
                now=now,
            )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="keepalive.gpu_stopped",
                resource_type="gpu",
                resource_id=gpu_ids[0],
                result="success",
                summary={"lease_id": lease.id},
                now=now,
            )
            self._allocate_queued(session, now, revision)
            keepalive = self._keepalive_lease_summary(lease, endpoint.id, gpu_ids[0])
            result = {"event_id": event.id, "snapshot_revision": revision, "keepalive": keepalive}
            if idempotency_key is not None:
                self._remember_idempotency(
                    session,
                    actor=actor,
                    action="keepalive.finalize_stop_gpu",
                    key=idempotency_key,
                    response=result,
                    now=now,
                )
            return result

        return self._write(operation)

    def _active_lease_for_gpu(self, session: Session, gpu_id: str) -> Lease | None:
        return session.scalar(
            select(Lease)
            .join(LeaseResource, LeaseResource.lease_id == Lease.id)
            .where(LeaseResource.gpu_id == gpu_id, LeaseResource.active.is_(True))
            .order_by(Lease.issued_at.desc())
            .limit(1)
        )

    def _maintenance_for_gpu(
        self, session: Session, gpu: GPUDevice, now: datetime
    ) -> MaintenanceWindow | None:
        return session.scalar(
            select(MaintenanceWindow)
            .where(
                MaintenanceWindow.state == "ACTIVE",
                MaintenanceWindow.start_at <= now,
                MaintenanceWindow.end_at > now,
                or_(
                    MaintenanceWindow.gpu_id == gpu.id,
                    MaintenanceWindow.endpoint_id == gpu.endpoint_id,
                ),
            )
            .order_by(MaintenanceWindow.start_at.desc())
            .limit(1)
        )

    def _current_reservation_for_gpu(
        self, session: Session, gpu_id: str, now: datetime
    ) -> Reservation | None:
        reservations = session.scalars(
            select(Reservation).where(
                Reservation.state == "ACTIVE",
                Reservation.start_at <= now,
                Reservation.end_at > now,
            )
        ).all()
        return next((item for item in reservations if gpu_id in json_load(item.gpu_ids_json)), None)

    def _gpu_state(self, session: Session, gpu: GPUDevice, now: datetime) -> tuple[str, str | None]:
        endpoint = session.get(Endpoint, gpu.endpoint_id)
        if endpoint is None or not gpu.enabled:
            return "DISABLED", "endpoint or GPU is disabled"
        if not gpu.present:
            return "UNKNOWN_STALE", "GPU absent from latest complete endpoint observation"
        if endpoint.lifecycle_state == "draining":
            return "DRAINING", "endpoint is draining and blocks new claims"
        if not endpoint.enabled:
            return "DISABLED", "endpoint is disabled"
        maintenance = self._maintenance_for_gpu(session, gpu, now)
        if maintenance is not None:
            return "MAINTENANCE", maintenance.reason
        telemetry = self._latest_telemetry(session, gpu.id)
        if telemetry is None:
            return "UNKNOWN_RECOVERING", "no fresh telemetry after service start"
        age = (now - (_as_utc(telemetry.observed_at) or now)).total_seconds()
        if age > self.inventory.collector.stale_after_seconds:
            return "UNKNOWN_STALE", f"telemetry age {age:.1f}s exceeds stale threshold"
        if telemetry.health.upper() not in {"OK", "HEALTHY"} or gpu.health.upper() not in {
            "OK",
            "HEALTHY",
        }:
            return "UNHEALTHY", telemetry.health
        lease = self._active_lease_for_gpu(session, gpu.id)
        if lease is not None and lease.kind != "workload" and lease.state == "CONFLICT":
            return "CONFLICT", "lease/process attribution conflict"
        if lease is not None and lease.state == "ORPHANED_BUSY":
            return "ORPHANED_BUSY", "lease expired while a compute process remains"
        if lease is not None and lease.kind == "keepalive":
            keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
            if keepalive_state == "ON":
                return "KEEPALIVE", "per-GPU keepalive is active"
            if keepalive_state == "OFF" and not self._current_processes(session, gpu.id, now):
                return "AVAILABLE", None
            return "CONFLICT", keepalive_reason
        processes = self._current_processes(session, gpu.id, now)
        if processes:
            if lease is not None and lease.kind == "workload":
                return "RUNNING_MANAGED", "compute process observed on assigned GPU"
            return "BUSY_UNMANAGED", "compute process observed; admission blocked"
        if lease is not None:
            return ("HELD" if lease.state == "HELD" else "LEASED_IDLE"), "exclusive lease active"
        reservation = self._current_reservation_for_gpu(session, gpu.id, now)
        if reservation is not None:
            return "RESERVED", f"reservation {reservation.id} is active"
        return "AVAILABLE", None

    @staticmethod
    def _process_key(process: ProcessObservation) -> str:
        started = _as_utc(process.process_started_at)
        assert started is not None
        return f"{process.gpu_id}:{process.pid}:{process.boot_id}:{int(started.timestamp())}"

    @classmethod
    def _process_dict(cls, process: ProcessObservation) -> dict[str, Any]:
        return {
            "pid": process.pid,
            "boot_id": process.boot_id,
            "process_started_at": _iso(process.process_started_at),
            "process_key": cls._process_key(process),
            "username": process.username,
            "executable": process.executable,
            "used_memory_mib": process.used_memory_mib,
            "observations": process.observations,
            "first_seen_at": _iso(process.first_seen_at),
            "last_seen_at": _iso(process.last_seen_at),
        }

    def _gpu_dict(self, session: Session, gpu: GPUDevice, now: datetime) -> dict[str, Any]:
        telemetry = self._latest_telemetry(session, gpu.id)
        processes = self._current_processes(session, gpu.id, now)
        lease = self._active_lease_for_gpu(session, gpu.id)
        state, reason = self._gpu_state(session, gpu, now)
        endpoint = session.get(Endpoint, gpu.endpoint_id)
        assert endpoint is not None
        keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
        keepalive_state, keepalive_reason = self._policy_keepalive_status(
            endpoint,
            state,
            keepalive_state,
            keepalive_reason,
        )
        return {
            "id": gpu.id,
            "endpoint_id": gpu.endpoint_id,
            "gpu_uuid": gpu.gpu_uuid,
            "gpu_index": gpu.gpu_index,
            "cuda_ordinal": gpu.cuda_ordinal,
            "name": gpu.name,
            "total_vram_mib": gpu.total_vram_mib,
            "labels": json_load(gpu.labels_json),
            "health": gpu.health,
            "enabled": gpu.enabled,
            "present": gpu.present,
            "state": state,
            "state_reason": reason,
            "first_seen_at": _iso(gpu.first_seen_at),
            "last_seen_at": _iso(gpu.last_seen_at),
            "absent_at": _iso(gpu.absent_at),
            "telemetry": self._telemetry_dict(telemetry),
            "processes": [self._process_dict(process) for process in processes],
            "lease": (
                self._lease_dict(session, lease)
                if lease is not None and lease.kind != "keepalive"
                else None
            ),
            "keepalive": {
                "configured": endpoint.keepalive_adapter_id is not None,
                "policy": endpoint.keepalive_policy,
                "desired": "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF",
                "actual": keepalive_state,
                "state": keepalive_state,
                "reason": keepalive_reason,
                "lease_id": lease.id if lease is not None and lease.kind == "keepalive" else None,
            },
        }

    def envelope(self, session: Session, data: Any) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshot_revision": self._revision(session),
            "server_time": _iso(utcnow()),
            "data": data,
        }

    def snapshot(
        self,
        actor: ActorContext,
        *,
        compact: bool = False,
        endpoint_id: str | None = None,
        state: str | None = None,
        only_available: bool = False,
    ) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            groups = self._server_groups_by_id(session)
            endpoints = session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
            provider_states = {
                provider_state.endpoint_id: provider_state
                for provider_state in session.scalars(
                    select(ProviderState).where(ProviderState.provider == "raw-ssh")
                ).all()
                if provider_state.endpoint_id is not None
            }
            visible_endpoints = [
                endpoint
                for endpoint in endpoints
                if endpoint_id is None or endpoint.id == endpoint_id
            ]
            visible_ids = {endpoint.id for endpoint in visible_endpoints}
            host_telemetry_by_endpoint = (
                {
                    item.endpoint_id: item
                    for item in session.scalars(
                        select(EndpointTelemetryCurrent).where(
                            EndpointTelemetryCurrent.endpoint_id.in_(visible_ids)
                        )
                    ).all()
                }
                if visible_ids
                else {}
            )
            recent_host_telemetry_samples_by_endpoint: dict[
                str, list[EndpointTelemetryCurrent | EndpointTelemetrySnapshot]
            ] = defaultdict(list)
            if visible_ids:
                recent_cutoff = now - timedelta(seconds=TELEMETRY_RECENT_AVERAGE_WINDOW_SECONDS)
                for sample in session.scalars(
                    select(EndpointTelemetrySnapshot)
                    .where(
                        EndpointTelemetrySnapshot.endpoint_id.in_(visible_ids),
                        EndpointTelemetrySnapshot.observed_at >= recent_cutoff,
                    )
                    .order_by(
                        EndpointTelemetrySnapshot.endpoint_id,
                        EndpointTelemetrySnapshot.observed_at,
                        EndpointTelemetrySnapshot.id,
                    )
                ).all():
                    recent_host_telemetry_samples_by_endpoint[sample.endpoint_id].append(sample)
                for host_endpoint_id, current in host_telemetry_by_endpoint.items():
                    observed_at = _as_utc(current.observed_at)
                    samples = recent_host_telemetry_samples_by_endpoint[host_endpoint_id]
                    last_observed_at = _as_utc(samples[-1].observed_at) if samples else None
                    if (
                        observed_at is not None
                        and observed_at >= recent_cutoff
                        and (last_observed_at is None or observed_at > last_observed_at)
                    ):
                        samples.append(current)
            endpoint_gpus = (
                session.scalars(
                    select(GPUDevice)
                    .where(GPUDevice.endpoint_id.in_(visible_ids))
                    .order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
                ).all()
                if visible_ids
                else []
            )
            gpus = [gpu for gpu in endpoint_gpus if gpu.present]
            absent_gpu_ids = [gpu.id for gpu in endpoint_gpus if not gpu.present]
            gpu_ids = {gpu.id for gpu in gpus}
            gpu_counts: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                gpu_counts[gpu.endpoint_id] += 1

            telemetry_by_gpu: dict[str, TelemetryCurrent | TelemetrySnapshot] = {}
            current_telemetry_by_gpu: dict[str, TelemetryCurrent] = {}
            if gpu_ids:
                current_telemetry_by_gpu = {
                    item.gpu_id: item
                    for item in session.scalars(
                        select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(gpu_ids))
                    ).all()
                }
                telemetry_by_gpu.update(current_telemetry_by_gpu)
                missing = gpu_ids.difference(telemetry_by_gpu)
                if missing:
                    latest_ids = (
                        select(func.max(TelemetrySnapshot.id))
                        .where(TelemetrySnapshot.gpu_id.in_(missing))
                        .group_by(TelemetrySnapshot.gpu_id)
                    )
                    telemetry_by_gpu.update(
                        {
                            item.gpu_id: item
                            for item in session.scalars(
                                select(TelemetrySnapshot).where(
                                    TelemetrySnapshot.id.in_(latest_ids)
                                )
                            ).all()
                        }
                    )

            recent_telemetry_samples_by_gpu: dict[
                str, list[TelemetryCurrent | TelemetrySnapshot]
            ] = defaultdict(list)
            if gpu_ids:
                recent_cutoff = now - timedelta(seconds=TELEMETRY_RECENT_AVERAGE_WINDOW_SECONDS)
                for sample in session.scalars(
                    select(TelemetrySnapshot)
                    .where(
                        TelemetrySnapshot.gpu_id.in_(gpu_ids),
                        TelemetrySnapshot.observed_at >= recent_cutoff,
                    )
                    .order_by(
                        TelemetrySnapshot.gpu_id,
                        TelemetrySnapshot.observed_at,
                        TelemetrySnapshot.id,
                    )
                ).all():
                    recent_telemetry_samples_by_gpu[sample.gpu_id].append(sample)
                for gpu_id, current in current_telemetry_by_gpu.items():
                    observed_at = _as_utc(current.observed_at)
                    samples = recent_telemetry_samples_by_gpu[gpu_id]
                    last_observed_at = _as_utc(samples[-1].observed_at) if samples else None
                    if (
                        observed_at is not None
                        and observed_at >= recent_cutoff
                        and (last_observed_at is None or observed_at > last_observed_at)
                    ):
                        samples.append(current)

            process_cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            processes_by_gpu: dict[str, list[ProcessObservation]] = defaultdict(list)
            if gpu_ids:
                for process in session.scalars(
                    select(ProcessObservation)
                    .where(
                        ProcessObservation.gpu_id.in_(gpu_ids),
                        ProcessObservation.active.is_(True),
                        ProcessObservation.last_seen_at >= process_cutoff,
                    )
                    .order_by(ProcessObservation.gpu_id, ProcessObservation.pid)
                ).all():
                    processes_by_gpu[process.gpu_id].append(process)

            all_leases = session.scalars(
                select(Lease)
                .where(Lease.state.in_(ACTIVE_LEASE_STATES))
                .order_by(Lease.issued_at.desc())
            ).all()
            visible_leases = [lease for lease in all_leases if lease.kind != "keepalive"]
            lease_by_id = {lease.id: lease for lease in all_leases}
            lease_ids = set(lease_by_id)
            resources_by_lease: dict[str, list[LeaseResource]] = defaultdict(list)
            lease_by_gpu: dict[str, Lease] = {}
            bindings_by_lease: dict[str, list[WorkloadBinding]] = defaultdict(list)
            if lease_ids:
                for resource in session.scalars(
                    select(LeaseResource)
                    .where(LeaseResource.lease_id.in_(lease_ids))
                    .order_by(LeaseResource.gpu_id)
                ).all():
                    resources_by_lease[resource.lease_id].append(resource)
                    if resource.active:
                        lease_by_gpu[resource.gpu_id] = lease_by_id[resource.lease_id]
                for binding in session.scalars(
                    select(WorkloadBinding).where(WorkloadBinding.lease_id.in_(lease_ids))
                ).all():
                    bindings_by_lease[binding.lease_id].append(binding)

            active_request_ids = {lease.request_id for lease in all_leases}
            requests_by_id = (
                {
                    request.id: request
                    for request in session.scalars(
                        select(AllocationRequest).where(
                            AllocationRequest.id.in_(active_request_ids)
                        )
                    ).all()
                }
                if active_request_ids
                else {}
            )
            lease_payloads = {
                lease.id: self._lease_dict(
                    session,
                    lease,
                    resources=resources_by_lease[lease.id],
                    bindings=bindings_by_lease[lease.id],
                    request=requests_by_id.get(lease.request_id),
                )
                for lease in all_leases
            }
            maintenance_by_gpu: dict[str, MaintenanceWindow] = {}
            maintenance_by_endpoint: dict[str, MaintenanceWindow] = {}
            current_reservation_by_gpu: dict[str, Reservation] = {}
            endpoint_payloads: list[dict[str, Any]] = []
            plugin_capacity_values = {
                item.key: item.value
                for item in session.scalars(
                    select(RuntimeSetting).where(
                        RuntimeSetting.key.like(f"{PLUGIN_CAPACITY_SETTING_PREFIX}%")
                    )
                ).all()
            }

            def endpoint_snapshot(endpoint: Endpoint) -> dict[str, Any]:
                provider_state = provider_states.get(endpoint.id)
                last_success = _as_utc(provider_state.last_success_at) if provider_state else None
                if endpoint.lifecycle_state == "draining":
                    monitor_status = "DRAINING"
                elif not endpoint.enabled:
                    monitor_status = "DISABLED"
                elif provider_state is None:
                    monitor_status = "PENDING"
                elif (
                    provider_state.last_error == "incomplete endpoint observation"
                    and gpu_counts[endpoint.id] == 0
                    and endpoint.expected_gpu_count is None
                    and host_telemetry_by_endpoint.get(endpoint.id) is not None
                ):
                    # A host-only probe remains useful and healthy even when
                    # NVIDIA runtime data is unavailable.  Keepalive-enabled
                    # GPU endpoints still require a complete observation.
                    monitor_status = "ONLINE"
                elif last_success is not None and now - last_success <= timedelta(
                    seconds=self.inventory.collector.stale_after_seconds
                ):
                    # Connectivity has a single criterion: how old the last
                    # successful observation is, the same one
                    # `_endpoint_is_unreachable` uses.  `last_error` describes
                    # the most recent attempt, not the host, so one failed
                    # probe against a host we heard from seconds ago leaves it
                    # ONLINE.
                    monitor_status = "ONLINE"
                elif provider_state.last_error:
                    monitor_status = "ERROR"
                else:
                    monitor_status = "STALE"
                host_telemetry = host_telemetry_by_endpoint.get(endpoint.id)
                host_telemetry_payload = self._host_telemetry_dict(host_telemetry)
                if host_telemetry_payload is not None:
                    host_telemetry_payload["recent_average"] = self._recent_host_telemetry_average(
                        recent_host_telemetry_samples_by_endpoint[endpoint.id]
                    )
                return {
                    **self._endpoint_dict(
                        endpoint,
                        scheduler_capacity=self._decode_plugin_capacity(
                            plugin_capacity_values.get(self._plugin_capacity_key(endpoint.id))
                        ),
                        server_groups=groups,
                    ),
                    # Filled from the final per-GPU projection below, so a
                    # busy sibling GPU never invalidates an independent
                    # keepalive worker.
                    "keepalive": self._keepalive_aggregate(endpoint, (), eligible_idle_gpu_count=0),
                    "host_telemetry": host_telemetry_payload,
                    "monitor": {
                        "status": monitor_status,
                        "gpu_count": gpu_counts[endpoint.id],
                        "absent_gpu_count": sum(
                            1
                            for gpu in endpoint_gpus
                            if gpu.endpoint_id == endpoint.id and not gpu.present
                        ),
                        "last_success_at": _iso(provider_state.last_success_at)
                        if provider_state
                        else None,
                        "last_attempt_at": _iso(provider_state.last_attempt_at)
                        if provider_state
                        else None,
                        "last_error": provider_state.last_error if provider_state else None,
                    },
                }

            endpoint_payloads = [endpoint_snapshot(endpoint) for endpoint in visible_endpoints]

            def derive_state(gpu: GPUDevice) -> tuple[str, str | None]:
                endpoint = next(item for item in visible_endpoints if item.id == gpu.endpoint_id)
                if not gpu.enabled:
                    return "DISABLED", "endpoint or GPU is disabled"
                if not gpu.present:
                    return "UNKNOWN_STALE", "GPU absent from latest complete endpoint observation"
                if endpoint.lifecycle_state == "draining":
                    return "DRAINING", "endpoint is draining and blocks new claims"
                if not endpoint.enabled:
                    return "DISABLED", "endpoint is disabled"
                maintenance = maintenance_by_gpu.get(gpu.id) or maintenance_by_endpoint.get(
                    gpu.endpoint_id
                )
                if maintenance is not None:
                    return "MAINTENANCE", maintenance.reason
                telemetry = telemetry_by_gpu.get(gpu.id)
                if telemetry is None:
                    return "UNKNOWN_RECOVERING", "no fresh telemetry after service start"
                age = (now - (_as_utc(telemetry.observed_at) or now)).total_seconds()
                if age > self.inventory.collector.stale_after_seconds:
                    return "UNKNOWN_STALE", f"telemetry age {age:.1f}s exceeds stale threshold"
                if telemetry.health.upper() not in {"OK", "HEALTHY"} or gpu.health.upper() not in {
                    "OK",
                    "HEALTHY",
                }:
                    return "UNHEALTHY", telemetry.health
                lease = lease_by_gpu.get(gpu.id)
                if lease is not None and lease.kind != "workload" and lease.state == "CONFLICT":
                    return "CONFLICT", "lease/process attribution conflict"
                if lease is not None and lease.state == "ORPHANED_BUSY":
                    return "ORPHANED_BUSY", "lease expired while a compute process remains"
                if lease is not None and lease.kind == "keepalive":
                    keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
                    if keepalive_state == "ON":
                        return "KEEPALIVE", "occupancy is active"
                    if keepalive_state == "OFF" and not processes_by_gpu[gpu.id]:
                        return "AVAILABLE", None
                    return "CONFLICT", keepalive_reason
                processes = processes_by_gpu[gpu.id]
                if processes:
                    if lease is not None and lease.kind == "workload":
                        return "RUNNING_MANAGED", "compute process observed on assigned GPU"
                    return "BUSY_UNMANAGED", "compute process observed; admission blocked"
                if lease is not None:
                    return (
                        "HELD" if lease.state == "HELD" else "LEASED_IDLE"
                    ), "exclusive lease active"
                reservation = current_reservation_by_gpu.get(gpu.id)
                if reservation is not None:
                    return "RESERVED", f"reservation {reservation.id} is active"
                return "AVAILABLE", None

            gpu_payloads: list[dict[str, Any]] = []
            for gpu in gpus:
                telemetry = telemetry_by_gpu.get(gpu.id)
                telemetry_payload = self._telemetry_dict(telemetry)
                if telemetry_payload is not None:
                    telemetry_payload["recent_average"] = self._recent_telemetry_average(
                        gpu,
                        recent_telemetry_samples_by_gpu[gpu.id],
                    )
                processes = processes_by_gpu[gpu.id]
                lease = lease_by_gpu.get(gpu.id)
                if telemetry_payload is not None and lease is not None and lease.kind == "workload":
                    # A holder may only be shown its own load. The plain window
                    # starts ten minutes ago, so a card claimed a moment ago
                    # reported the previous holder's work — or ServerPilot's own
                    # keepalive hold — as the caller's, and an agent sizing a
                    # batch from it would size it against somebody else's job.
                    lease_started = _as_utc(lease.issued_at) or recent_cutoff
                    telemetry_payload["lease_recent_average"] = self._recent_telemetry_average(
                        gpu,
                        [
                            sample
                            for sample in recent_telemetry_samples_by_gpu[gpu.id]
                            if (_as_utc(sample.observed_at) or now) >= lease_started
                        ],
                    )
                gpu_state, reason = derive_state(gpu)
                keepalive_state, keepalive_reason = self._keepalive_gpu_status(session, gpu, lease, now)
                endpoint = next(item for item in visible_endpoints if item.id == gpu.endpoint_id)
                keepalive_state, keepalive_reason = self._policy_keepalive_status(
                    endpoint,
                    gpu_state,
                    keepalive_state,
                    keepalive_reason,
                )
                payload = {
                    "id": gpu.id,
                    "endpoint_id": gpu.endpoint_id,
                    "gpu_uuid": gpu.gpu_uuid,
                    "gpu_index": gpu.gpu_index,
                    "cuda_ordinal": gpu.cuda_ordinal,
                    "name": gpu.name,
                    "total_vram_mib": gpu.total_vram_mib,
                    "labels": json_load(gpu.labels_json),
                    "health": gpu.health,
                    "enabled": gpu.enabled,
                    "present": gpu.present,
                    "state": gpu_state,
                    "state_reason": reason,
                    "first_seen_at": _iso(gpu.first_seen_at),
                    "last_seen_at": _iso(gpu.last_seen_at),
                    "absent_at": _iso(gpu.absent_at),
                    "telemetry": telemetry_payload,
                    "processes": [self._process_dict(process) for process in processes],
                    "lease": (
                        lease_payloads.get(lease.id)
                        if lease is not None and lease.kind != "keepalive"
                        else None
                    ),
                    "keepalive": {
                        "configured": endpoint.keepalive_adapter_id is not None,
                        "policy": endpoint.keepalive_policy,
                        "desired": (
                            "ON" if endpoint.keepalive_policy == "idle_keepalive" else "OFF"
                        ),
                        "actual": keepalive_state,
                        "state": keepalive_state,
                        "reason": keepalive_reason,
                        "lease_id": (
                            lease.id if lease is not None and lease.kind == "keepalive" else None
                        ),
                    },
                }
                gpu_payloads.append(payload)

            endpoint_monitor_by_id = {
                endpoint["id"]: endpoint["monitor"]["status"] for endpoint in endpoint_payloads
            }
            for payload in gpu_payloads:
                payload.update(
                    self._gpu_public_projection(
                        payload,
                        monitor_status=endpoint_monitor_by_id[payload["endpoint_id"]],
                    )
                )

            endpoint_by_id = {endpoint.id: endpoint for endpoint in visible_endpoints}
            for endpoint_payload in endpoint_payloads:
                endpoint_id_value = endpoint_payload["id"]
                endpoint = endpoint_by_id[endpoint_id_value]
                endpoint_gpu_payloads = [
                    item for item in gpu_payloads if item["endpoint_id"] == endpoint_id_value
                ]
                endpoint_payload["keepalive"] = self._keepalive_aggregate(
                    endpoint,
                    [
                        {
                            "gpu_id": item["id"],
                            "state": item["keepalive"]["state"],
                            "reason": item["keepalive"]["reason"],
                        }
                        for item in endpoint_gpu_payloads
                    ],
                    eligible_idle_gpu_count=(
                        sum(item["state"] == "AVAILABLE" for item in endpoint_gpu_payloads)
                        if (
                            endpoint.keepalive_policy == "idle_keepalive"
                            and endpoint.keepalive_adapter_id is not None
                        )
                        else 0
                    ),
                )

            running_lease_ids = {
                item["lease"]["id"]
                for item in gpu_payloads
                if item["state"] == "RUNNING_MANAGED" and item["lease"] is not None
            }
            # "No process right now" is what release-empty already proves, and
            # it is not the same as "this lease is finished": a job between two
            # batches looks exactly like one that ended. Publishing when a
            # process was last seen lets whoever is about to clear a lease see
            # that difference. It is read-only and never gates the release --
            # a gate here would wedge the very leases this path exists to
            # recover, whose resources stop being timestamped once they leave
            # the states that update them.
            last_process_by_lease: dict[str, datetime | None] = defaultdict(lambda: None)
            # Deliberately not the current-process set: the moment this matters
            # is the moment a lease looks empty, and a process that has gone is
            # exactly the one whose timing has to survive. The bound is when
            # each process *started*, not when it was last seen: only a process
            # begun after the cards were handed over can be this holder's, and
            # a card is routinely re-let seconds after its previous job -- or
            # after ServerPilot's own keepalive worker -- stopped.
            own_processes = [
                and_(
                    ProcessObservation.gpu_id == gpu_id,
                    ProcessObservation.process_started_at >= started,
                )
                for gpu_id, lease in lease_by_gpu.items()
                if (started := _as_utc(lease.issued_at)) is not None
            ]
            if own_processes:
                for gpu_id, last_seen in session.execute(
                    select(
                        ProcessObservation.gpu_id,
                        func.max(ProcessObservation.last_seen_at),
                    )
                    .where(or_(*own_processes))
                    .group_by(ProcessObservation.gpu_id)
                ).all():
                    lease = lease_by_gpu.get(gpu_id)
                    seen = _as_utc(last_seen)
                    if lease is None or seen is None:
                        continue
                    known = last_process_by_lease[lease.id]
                    if known is None or seen > known:
                        last_process_by_lease[lease.id] = seen
            for lease_id, payload in lease_payloads.items():
                payload["runtime_state"] = (
                    "RUNNING" if lease_id in running_lease_ids else "ASSIGNED"
                )
                payload["last_process_observed_at"] = _iso(last_process_by_lease[lease_id])

            all_gpu_payloads = gpu_payloads
            counts = defaultdict(int)
            for gpu in all_gpu_payloads:
                counts[gpu["state"]] += 1
            workload_claimed_gpu_count = sum(item["lease"] is not None for item in all_gpu_payloads)
            keepalive_owned_gpu_count = sum(
                item["keepalive"]["lease_id"] is not None for item in all_gpu_payloads
            )
            claimed_states = {
                "HELD",
                "LEASED_IDLE",
                "RUNNING_MANAGED",
                "ORPHANED_BUSY",
                "CONFLICT",
            }
            abnormal_states = {
                "UNKNOWN_RECOVERING",
                "UNKNOWN_STALE",
                "UNHEALTHY",
                "CONFLICT",
                "ORPHANED_BUSY",
            }
            endpoint_attention_statuses = {"ERROR", "STALE"}
            endpoint_attention_status_counts: dict[str, int] = defaultdict(int)
            for endpoint in endpoint_payloads:
                status = endpoint["monitor"]["status"]
                if status in endpoint_attention_statuses:
                    endpoint_attention_status_counts[status] += 1
            gpu_attention_states = abnormal_states | {"BUSY_UNMANAGED"}
            gpu_attention_state_counts = {
                state: counts[state] for state in sorted(gpu_attention_states) if counts[state]
            }
            attention_endpoint_count = sum(endpoint_attention_status_counts.values())
            attention_gpu_count = sum(gpu_attention_state_counts.values())
            summary = {
                "online_servers": sum(
                    endpoint["monitor"]["status"] == "ONLINE" for endpoint in endpoint_payloads
                ),
                "total_servers": len(endpoint_payloads),
                "total_gpus": len(all_gpu_payloads),
                "available_gpus": sum(
                    self._gpu_payload_is_publicly_available(item) for item in all_gpu_payloads
                ),
                "busy_gpus": counts["BUSY_UNMANAGED"] + counts["RUNNING_MANAGED"],
                "claimed_gpus": sum(
                    item["state"] in claimed_states
                    and not self._gpu_payload_is_publicly_available(item)
                    for item in all_gpu_payloads
                ),
                "workload_claimed_gpus": workload_claimed_gpu_count,
                "keepalive_owned_gpus": keepalive_owned_gpu_count,
                "verified_keepalive_gpus": counts["KEEPALIVE"],
                "abnormal_gpus": sum(counts[item] for item in abnormal_states),
                "attention": {
                    "endpoint_count": attention_endpoint_count,
                    "endpoint_status_counts": dict(
                        sorted(endpoint_attention_status_counts.items())
                    ),
                    "gpu_count": attention_gpu_count,
                    "absent_gpu_count": len(absent_gpu_ids),
                    "gpu_state_counts": gpu_attention_state_counts,
                    "unmanaged_gpu_count": counts["BUSY_UNMANAGED"],
                    "total_resource_count": attention_endpoint_count
                    + attention_gpu_count
                    + len(absent_gpu_ids),
                },
            }
            if not absent_gpu_ids:
                summary["attention"].pop("absent_gpu_count")
            ages = [
                max(0.0, (now - (_as_utc(item.observed_at) or now)).total_seconds())
                for item in telemetry_by_gpu.values()
            ]

            requested_state = "AVAILABLE" if only_available else state.upper() if state else None
            if requested_state == "AVAILABLE":
                gpu_payloads = [
                    item for item in gpu_payloads if self._gpu_payload_is_publicly_available(item)
                ]
            elif requested_state:
                gpu_payloads = [item for item in gpu_payloads if item["state"] == requested_state]
            if compact:
                gpu_payloads = [
                    {
                        "id": item["id"],
                        "endpoint_id": item["endpoint_id"],
                        "gpu_index": item["gpu_index"],
                        "name": item["name"],
                        "total_vram_mib": item["total_vram_mib"],
                        "state": item["state"],
                        "state_reason": item["state_reason"],
                        "publicly_available": item["publicly_available"],
                        "public_status": item["public_status"],
                        "telemetry": (
                            {
                                "observed_at": item["telemetry"]["observed_at"],
                                "memory_used_mib": item["telemetry"]["memory_used_mib"],
                                "memory_free_mib": item["telemetry"]["memory_free_mib"],
                                "gpu_utilization_pct": item["telemetry"]["gpu_utilization_pct"],
                                "temperature_c": item["telemetry"]["temperature_c"],
                            }
                            if item["telemetry"]
                            else None
                        ),
                        "process_count": len(item["processes"]),
                        "owner": item["lease"]["actor_id"] if item["lease"] else None,
                        "task_ref": item["lease"]["task_ref"] if item["lease"] else None,
                        "expires_at": item["lease"]["expires_at"] if item["lease"] else None,
                    }
                    for item in gpu_payloads
                ]

            visible_gpu_ids = {gpu.id for gpu in endpoint_gpus}

            alert_payloads = [
                self._alert_dict(alert)
                for alert in session.scalars(
                    select(Alert)
                    .where(Alert.active.is_(True))
                    .order_by(Alert.severity, Alert.last_seen_at.desc(), Alert.id)
                ).all()
            ]
            endpoint_payload_by_id = {item["id"]: item for item in endpoint_payloads}
            direct_commitments_by_endpoint = self._endpoint_commitment_usage(session)
            host_capacity_payloads: list[dict[str, Any]] = []
            for endpoint in visible_endpoints:
                telemetry = host_telemetry_by_endpoint.get(endpoint.id)
                endpoint_payload = endpoint_payload_by_id[endpoint.id]
                monitor_status = endpoint_payload["monitor"]["status"]
                observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
                telemetry_stale = (
                    telemetry is None
                    or observed_at is None
                    or now - observed_at
                    > timedelta(seconds=self.inventory.collector.stale_after_seconds)
                )
                direct_cpu, direct_memory = direct_commitments_by_endpoint.get(
                    endpoint.id, (0.0, 0)
                )
                # `_host_capacity` owns what this endpoint may actually use.
                # Reading `cpu_count`/`load_1m` here would describe the whole
                # node, which is a different machine than a container endpoint.
                capacity = self._host_capacity(telemetry) if telemetry is not None else None
                if capacity is None:
                    available_cpu = None
                    available_memory = None
                else:
                    observed_cpu = capacity["cpu_available_cores"]
                    available_cpu = (
                        None if observed_cpu is None else max(0.0, observed_cpu - direct_cpu)
                    )
                    available_memory = max(
                        0,
                        capacity["memory_available_mib"] - direct_memory,
                    )
                monitor_reason = {
                    "DRAINING": "endpoint is draining and blocks new claims",
                    "DISABLED": "endpoint is disabled",
                    "PENDING": "no successful collector observation",
                    "STALE": "collector success is stale",
                }.get(monitor_status)
                if monitor_status == "ERROR":
                    monitor_reason = (
                        endpoint_payload["monitor"].get("last_error")
                        or "collector has no successful observation"
                    )
                admission_state = "available"
                admission_reason = None
                if monitor_status != "ONLINE":
                    admission_state = "blocked"
                    admission_reason = monitor_reason or monitor_status.lower()
                elif telemetry_stale:
                    admission_state = "blocked"
                    admission_reason = "host telemetry is stale"
                elif available_cpu is None:
                    admission_state = "blocked"
                    admission_reason = "this endpoint's own CPU share is not observable yet"
                elif available_cpu == 0 and available_memory == 0:
                    admission_state = "blocked"
                    admission_reason = "no uncommitted CPU or memory capacity"
                host_capacity_payloads.append(
                    {
                        "provider": None,
                        "unit": None,
                        "endpoint": self._endpoint_dict(endpoint, server_groups=groups),
                        "monitor_status": monitor_status,
                        "admission_state": admission_state,
                        "admission_reason": admission_reason,
                        "telemetry": self._host_telemetry_dict(telemetry),
                        "capacity": {
                            "total_cpu_cores": capacity["cpu_cores"] if capacity else None,
                            "observed_available_cpu_cores": round(
                                capacity["cpu_available_cores"], 1
                            )
                            if capacity and capacity["cpu_available_cores"] is not None
                            else None,
                            "available_cpu_cores": round(available_cpu, 1)
                            if available_cpu is not None
                            else None,
                            "total_memory_mib": capacity["memory_total_mib"] if capacity else None,
                            "observed_available_memory_mib": capacity["memory_available_mib"]
                            if capacity
                            else None,
                            "available_memory_mib": available_memory,
                            "committed_cpu_cores": round(direct_cpu, 1),
                            "committed_memory_mib": direct_memory,
                            "direct_lease_cpu_cores": round(direct_cpu, 1),
                            "direct_lease_memory_mib": direct_memory,
                        },
                    }
                )

            visible_lease_payloads = [
                lease_payloads[lease.id]
                for lease in visible_leases
                if any(
                    resource.active and resource.gpu_id in visible_gpu_ids
                    for resource in resources_by_lease[lease.id]
                )
            ]
            resource_usage_revision = str(self._revision(session))
            active_host_capacity = [
                card for card in host_capacity_payloads if card["admission_state"] == "available"
            ]
            resource_projection = {
                "capacity": {
                    "gpu_count": len(all_gpu_payloads),
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["total_cpu_cores"] or 0.0
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["total_memory_mib"] or 0 for card in host_capacity_payloads
                    ),
                    "vram_mib": sum(gpu["total_vram_mib"] for gpu in all_gpu_payloads),
                },
                "used": {
                    "gpu_count": counts["BUSY_UNMANAGED"] + counts["RUNNING_MANAGED"],
                    # Used is total minus what the resolver says is still free,
                    # so it stays on the same scope as the capacity above; the
                    # machine-wide load average would not.  An endpoint whose
                    # free share is not observable counts as fully used.
                    "cpu_cores": round(
                        sum(
                            max(
                                0.0,
                                (card["capacity"]["total_cpu_cores"] or 0.0)
                                - (card["capacity"]["observed_available_cpu_cores"] or 0.0),
                            )
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        max(
                            0,
                            (card["capacity"]["total_memory_mib"] or 0)
                            - (card["capacity"]["observed_available_memory_mib"] or 0),
                        )
                        for card in host_capacity_payloads
                    ),
                    "vram_mib": sum(
                        (gpu["telemetry"] or {}).get("memory_used_mib") or 0
                        for gpu in all_gpu_payloads
                    ),
                },
                "claimed": {
                    "gpu_count": summary["claimed_gpus"],
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["committed_cpu_cores"] or 0.0
                            for card in host_capacity_payloads
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["committed_memory_mib"] or 0
                        for card in host_capacity_payloads
                    ),
                    "lease_count": len(visible_lease_payloads),
                    "claim_count": 0,
                },
                "available": {
                    "gpu_count": summary["available_gpus"],
                    "cpu_cores": round(
                        sum(
                            card["capacity"]["available_cpu_cores"] or 0.0
                            for card in active_host_capacity
                        ),
                        1,
                    ),
                    "memory_mib": sum(
                        card["capacity"]["available_memory_mib"] or 0
                        for card in active_host_capacity
                    ),
                    "host_capacity_units": len(active_host_capacity),
                },
                "attention": summary["attention"],
                "semantics": {
                    "available_is_authoritative": True,
                    "used_and_claimed_may_overlap": True,
                    "fail_closed_states": [
                        "BUSY_UNMANAGED",
                        "CONFLICT",
                        "MAINTENANCE",
                        "ORPHANED_BUSY",
                        "UNKNOWN_RECOVERING",
                        "UNKNOWN_STALE",
                        "UNHEALTHY",
                    ],
                },
            }

            data = {
                "summary": summary,
                "resource_projection": resource_projection,
                "data_age_seconds": round(max(ages), 1) if ages else None,
                "endpoints": endpoint_payloads,
                "server_groups": [
                    self._project_server_group(
                        group,
                        endpoints=visible_endpoints,
                        gpu_payloads=all_gpu_payloads,
                        endpoint_payloads=endpoint_payloads,
                    )
                    for group in groups.values()
                    if endpoint_id is None
                    or any(
                        endpoint.server_group_id == group.id for endpoint in visible_endpoints
                    )
                ],
                "gpus": gpu_payloads,
                "absent_gpu_ids": absent_gpu_ids,
                "leases": visible_lease_payloads,
                "requests": [],
                "reservations": [],
                "maintenance": [],
                "alerts": alert_payloads,
                "host_capacity": host_capacity_payloads,
                "resource_usage_revision": resource_usage_revision,
                "freshness_seconds": self.inventory.collector.stale_after_seconds,
                "admission_boundary": "ServerPilot 只记录资源分配，不会在服务器上启动或停止任务。",
            }
            return self.envelope(session, data)

        return self._read(operation)

    def control_plane_state(
        self,
        actor: ActorContext,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(actor)
        data = snapshot["data"]
        current_keys = (
            "summary",
            "resource_projection",
            "data_age_seconds",
            "freshness_seconds",
            "endpoints",
            "server_groups",
            "gpus",
            "absent_gpu_ids",
            "leases",
            "requests",
            "reservations",
            "maintenance",
            "alerts",
            "host_capacity",
            "admission_boundary",
        )
        current = {key: data[key] for key in current_keys}
        if not actor.is_admin:

            def visible_project_item(item: dict[str, Any]) -> bool:
                return (
                    item.get("actor_id") == actor.id or item.get("project_id") in actor.project_ids
                )

            current["leases"] = [
                lease for lease in current["leases"] if visible_project_item(lease)
            ]
            current["requests"] = [
                request for request in current["requests"] if visible_project_item(request)
            ]
            current["reservations"] = [
                reservation
                for reservation in current["reservations"]
                if visible_project_item(reservation)
            ]
            visible_lease_ids = {lease["id"] for lease in current["leases"]}
            current["gpus"] = [
                {
                    **gpu,
                    "lease": (
                        gpu["lease"]
                        if gpu.get("lease") is None or gpu["lease"]["id"] in visible_lease_ids
                        else None
                    ),
                }
                for gpu in current["gpus"]
            ]
        return {
            "schema_version": snapshot["schema_version"],
            "snapshot_revision": snapshot["snapshot_revision"],
            "server_time": snapshot["server_time"],
            "data": {
                "current": current,
                "history": {},
            },
        }

    # Public resource contracts are thin projections over the one domain-owned
    # monitor.  Keeping them here avoids a second scheduler state path in the
    # REST/MCP adapters while retaining the compatibility GPU endpoints.
    def list_endpoints(self, actor: ActorContext) -> dict[str, Any]:
        snapshot = self.snapshot(actor)
        return {**snapshot, "data": snapshot["data"]["endpoints"]}

    def list_gpus(
        self,
        actor: ActorContext,
        *,
        state: str | None = None,
        endpoint_id: str | None = None,
        only_available: bool = False,
        compact: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.snapshot(
            actor,
            state=state,
            endpoint_id=endpoint_id,
            only_available=only_available,
            compact=compact,
        )
        values = snapshot["data"]["gpus"]
        return {**snapshot, "data": values}

    def endpoint_history(
        self,
        actor: ActorContext,
        endpoint_id: str,
        *,
        window_seconds: int = 3600,
        max_points: int = 120,
    ) -> dict[str, Any]:
        """Return bounded host CPU/load/memory history for one endpoint detail view."""

        if window_seconds not in {3600, 21_600, TELEMETRY_HISTORY_RETENTION_SECONDS}:
            raise BrokerError(
                "invalid_history_window",
                "endpoint history window must be one of 1h, 6h or 24h",
                status_code=422,
            )
        if not 10 <= max_points <= 120:
            raise BrokerError(
                "invalid_history_points",
                "history points must be between 10 and 120",
                status_code=422,
            )

        def operation(session: Session) -> dict[str, Any]:
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_found",
                    "endpoint is not visible or does not exist",
                    status_code=404,
                )
            now = utcnow()
            cutoff = now - timedelta(seconds=window_seconds)
            samples: list[EndpointTelemetryCurrent | EndpointTelemetrySnapshot] = list(
                session.scalars(
                    select(EndpointTelemetrySnapshot)
                    .where(
                        EndpointTelemetrySnapshot.endpoint_id == endpoint_id,
                        EndpointTelemetrySnapshot.observed_at >= cutoff,
                    )
                    .order_by(
                        EndpointTelemetrySnapshot.observed_at,
                        EndpointTelemetrySnapshot.id,
                    )
                ).all()
            )
            current = session.get(EndpointTelemetryCurrent, endpoint_id)
            current_observed_at = _as_utc(current.observed_at) if current is not None else None
            if (
                current is not None
                and current_observed_at is not None
                and current_observed_at >= cutoff
                and (not samples or current_observed_at > (_as_utc(samples[-1].observed_at) or now))
            ):
                samples.append(current)

            def buckets_for(values: list[Any]) -> list[list[Any]]:
                if len(values) <= max_points:
                    return [[value] for value in values]
                return [
                    values[
                        index * len(values) // max_points : (index + 1) * len(values) // max_points
                    ]
                    for index in range(max_points)
                ]

            buckets = buckets_for(samples)

            def average(bucket: list[Any], name: str) -> float | None:
                values = [getattr(sample, name) for sample in bucket]
                present = [float(value) for value in values if value is not None]
                return round(sum(present) / len(present), 2) if present else None

            points = []
            for bucket in buckets:
                memory_total = average(bucket, "memory_total_mib")
                memory_available = average(bucket, "memory_available_mib")
                memory_used_values = [
                    BrokerService._host_memory_used_pct(sample) for sample in bucket
                ]
                present_used = [value for value in memory_used_values if value is not None]
                points.append(
                    {
                        "observed_at": _iso(bucket[-1].observed_at),
                        "cpu_count": average(bucket, "cpu_count"),
                        "load_1m": average(bucket, "load_1m"),
                        "cpu_utilization_pct": average(bucket, "cpu_utilization_pct"),
                        "memory_total_mib": memory_total,
                        "memory_available_mib": memory_available,
                        "memory_limit_mib": average(bucket, "memory_limit_mib"),
                        "memory_current_mib": average(bucket, "memory_current_mib"),
                        "memory_used_pct": (
                            round(sum(present_used) / len(present_used), 2)
                            if present_used
                            else None
                        ),
                    }
                )

            # GPU history is deliberately returned as endpoint-scoped series rather
            # than asking the desktop to fan out into one request per GPU.  The
            # identity and historical source remain the canonical GPU device and
            # telemetry snapshot tables; this is only a bounded read projection.
            devices = list(
                session.scalars(
                    select(GPUDevice)
                    .where(
                        GPUDevice.endpoint_id == endpoint.id,
                        GPUDevice.present.is_(True),
                    )
                    .order_by(GPUDevice.gpu_index, GPUDevice.id)
                ).all()
            )
            device_ids = [device.id for device in devices]
            readings_by_gpu: dict[str, list[TelemetryCurrent | TelemetrySnapshot]] = {
                device.id: [] for device in devices
            }
            if device_ids:
                historical_readings = session.scalars(
                    select(TelemetrySnapshot)
                    .where(
                        TelemetrySnapshot.gpu_id.in_(device_ids),
                        TelemetrySnapshot.observed_at >= cutoff,
                    )
                    .order_by(
                        TelemetrySnapshot.gpu_id,
                        TelemetrySnapshot.observed_at,
                        TelemetrySnapshot.id,
                    )
                ).all()
                for reading in historical_readings:
                    readings_by_gpu[reading.gpu_id].append(reading)

                current_readings = session.scalars(
                    select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(device_ids))
                ).all()
                for reading in current_readings:
                    observed_at = _as_utc(reading.observed_at)
                    history = readings_by_gpu[reading.gpu_id]
                    if (
                        observed_at is not None
                        and observed_at >= cutoff
                        and (not history or observed_at > (_as_utc(history[-1].observed_at) or now))
                    ):
                        history.append(reading)

            sampled = [
                (device, readings_by_gpu[device.id])
                for device in devices
                if readings_by_gpu[device.id]
            ]
            index_counts = Counter(device.gpu_index for device, _readings in sampled)
            gpu_series = []
            for device, readings in sampled:
                series_points = []
                for bucket in buckets_for(readings):
                    memory_used = average(bucket, "memory_used_mib")
                    series_points.append(
                        {
                            "observed_at": _iso(bucket[-1].observed_at),
                            "gpu_utilization_pct": average(bucket, "gpu_utilization_pct"),
                            "memory_used_pct": (
                                round((memory_used or 0) * 100 / device.total_vram_mib, 2)
                                if device.total_vram_mib
                                else None
                            ),
                            "memory_used_mib": memory_used,
                            "memory_total_mib": device.total_vram_mib,
                        }
                    )
                gpu_series.append(
                    {
                        "gpu_id": device.id,
                        "gpu_uuid": device.gpu_uuid,
                        "gpu_index": device.gpu_index,
                        "label": _gpu_history_series_label(
                            gpu_index=device.gpu_index,
                            gpu_uuid=device.gpu_uuid,
                            index_count=index_counts[device.gpu_index],
                        ),
                        "points": series_points,
                    }
                )

            return self.envelope(
                session,
                {
                    "endpoint_id": endpoint.id,
                    "window_seconds": window_seconds,
                    "point_count": len(points),
                    "points": points,
                    "gpu_series": gpu_series,
                },
            )

        return self._read(operation)

    def prune_telemetry_history(
        self, older_than_seconds: int = TELEMETRY_HISTORY_RETENTION_SECONDS
    ) -> int:
        """Internal hourly retention pass; current telemetry, leases and audit are untouched."""

        cutoff = utcnow() - timedelta(seconds=older_than_seconds)

        def operation(session: Session) -> int:
            gpu_result = session.execute(
                delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
            )
            endpoint_result = session.execute(
                delete(EndpointTelemetrySnapshot).where(
                    EndpointTelemetrySnapshot.observed_at < cutoff
                )
            )
            return max(0, gpu_result.rowcount or 0) + max(0, endpoint_result.rowcount or 0)

        return self._write(operation)

    # ---- collector input and telemetry / process reconciliation ----------------

    def _converge_gpu_uuid_ownership(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observed_uuids: dict[str, GPUDevice],
        observed_at: datetime,
        now: datetime,
    ) -> set[str]:
        """Make one physical GPU belong to exactly one endpoint.

        A GPU UUID is an identity boundary, but a GPU row is keyed by endpoint,
        so re-registering the same machine on a new forwarded port leaves the
        same card present under two endpoints, each free to lease it.

        The rule is a sticky incumbent with handover on staleness: an endpoint
        that still sees the card keeps it and a later registration stands down,
        and once the incumbent goes stale the newcomer takes over, which is
        what a restarted container needs. The winner is decided by the
        persisted ``present`` flag rather than by comparing two timestamps, so
        two live endpoints settle instead of trading the card every cycle.

        Endpoints are never merged; only the card's owner is decided. Returns
        the endpoints that lost a card, so their leases are reconciled too.
        """

        if not observed_uuids:
            return set()
        stale_before = observed_at - timedelta(
            seconds=self.inventory.collector.stale_after_seconds
        )
        affected: set[str] = set()
        for sibling in session.scalars(
            select(GPUDevice).where(
                GPUDevice.gpu_uuid.in_(list(observed_uuids)),
                GPUDevice.endpoint_id != endpoint_id,
                GPUDevice.present.is_(True),
            )
        ).all():
            mine = observed_uuids.get(sibling.gpu_uuid)
            if mine is None:
                continue
            last_seen = _as_utc(sibling.last_seen_at)
            if last_seen is not None and last_seen >= stale_before:
                loser, alert_type, held_by = mine, "duplicate_gpu_uuid", sibling.endpoint_id
            else:
                loser, alert_type, held_by = sibling, "gpu_uuid_moved", endpoint_id
                affected.add(sibling.endpoint_id)
            loser.present = False
            loser.absent_at = observed_at
            self._upsert_alert(
                session,
                alert_type=alert_type,
                severity="warning",
                resource_type="endpoint",
                resource_id=loser.endpoint_id,
                message=(
                    f"GPU {sibling.gpu_uuid} is held by endpoint {held_by}; "
                    "this registration cannot lease it"
                ),
                now=now,
            )
        if affected:
            session.flush()
        return affected

    def _release_absent_keepalive_leases(
        self,
        session: Session,
        *,
        endpoint_ids: set[str],
        now: datetime,
        revision: int,
    ) -> int:
        """Release keepalive leases whose every active GPU is absent from inventory.

        Workload leases are left in place; a complete observation can prove a
        vanished GPU is gone, but it cannot prove a user job was empty.

        More than one endpoint can be reconciled at once: when a card is
        observed on a new registration the old one loses it in the same pass,
        and its keeper must not go on claiming a card it no longer holds.
        """

        gpus = {
            gpu.id: gpu
            for gpu in session.scalars(
                select(GPUDevice).where(GPUDevice.endpoint_id.in_(sorted(endpoint_ids)))
            ).all()
        }
        absent_gpu_ids = {gpu_id for gpu_id, gpu in gpus.items() if not gpu.present}
        if not absent_gpu_ids:
            return revision

        for current in session.scalars(
            select(KeepaliveCurrent).where(KeepaliveCurrent.gpu_id.in_(absent_gpu_ids))
        ).all():
            session.delete(current)

        resources = session.scalars(
            select(LeaseResource).where(
                LeaseResource.gpu_id.in_(list(gpus)),
                LeaseResource.active.is_(True),
            )
        ).all()
        resources_by_lease: dict[str, list[LeaseResource]] = defaultdict(list)
        for resource in resources:
            resources_by_lease[resource.lease_id].append(resource)

        released_any = False
        for lease_id in resources_by_lease:
            lease = session.get(Lease, lease_id)
            if (
                lease is None
                or lease.kind != "keepalive"
                or lease.state in TERMINAL_LEASE_STATES
            ):
                continue
            all_active = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            if not all_active:
                continue
            if any(
                (gpu := session.get(GPUDevice, resource.gpu_id)) is None or gpu.present
                for resource in all_active
            ):
                continue
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = "gpu absent from endpoint inventory"
            for resource in all_active:
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            self._audit(
                session,
                actor_id=f"collector:{sorted(endpoint_ids)[0]}",
                action="lease.released",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"reason": lease.release_reason},
                now=now,
            )
            released_any = True
        if released_any:
            revision = self._bump_revision(session, now)
        return revision

    def ingest_observation(
        self,
        observation: EndpointObservation,
        *,
        provider: str = "raw-ssh",
        implementation_version: str | None = None,
    ) -> dict[str, Any]:
        """Persist one all-or-nothing read-only endpoint observation.

        Collector data is accepted only from an internal collector call in the
        pilot. The HTTP layer never exposes arbitrary remote command execution.
        """

        from serverpilot.collector_protocol import take_collector_implementation_version

        resolved_version = implementation_version
        if resolved_version is None:
            resolved_version = take_collector_implementation_version(observation)

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            endpoint = session.get(Endpoint, observation.endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_found", "collector reported an unknown endpoint", status_code=404
                )
            observed_gpu_ids = {f"{endpoint.id}:{sample.gpu_uuid}" for sample in observation.gpus}
            observed_at = ensure_utc(observation.observed_at)
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            current_observed_at = _as_utc(host_telemetry.observed_at) if host_telemetry else None
            if current_observed_at is not None and observed_at < current_observed_at:
                return {
                    "event_id": None,
                    "snapshot_revision": self._revision(session),
                    "endpoint_id": endpoint.id,
                    "gpu_count": len(observation.gpus),
                    "absent_gpu_count": 0,
                    "observation_complete": observation.observation_complete,
                    "process_count": len(observation.processes),
                    "history_points_written": 0,
                    "endpoint_history_points_written": 0,
                    "ignored": True,
                    "ignore_reason": "stale_observation",
                    "current_observed_at": _iso(current_observed_at),
                }
            revision = self._bump_revision(session, now)
            if observation.gpu_probe_status == "cpu_only":
                endpoint.resource_kind = "cpu_only"
                endpoint.updated_at = now
            elif observation.gpus:
                endpoint.resource_kind = "gpu"
                endpoint.updated_at = now
            self._persist_plugin_capacity(
                session, endpoint, observation.scheduler, now=now
            )
            self._persist_collector_implementation_version(
                session, endpoint.id, resolved_version, now=now
            )
            host_cpu_total_ticks = observation.host.cpu_total_ticks
            host_cpu_idle_ticks = observation.host.cpu_idle_ticks
            host_cpu_usage_usec = observation.host.cpu_usage_usec
            host_cpu_quota_usec = observation.host.cpu_quota_usec
            host_cpu_period_usec = observation.host.cpu_period_usec
            host_cpu_utilization_pct = self._host_cpu_utilization_pct(
                host_telemetry,
                observed_at=observed_at,
                cpu_count=observation.host.cpu_count,
                cpu_usage_usec=host_cpu_usage_usec,
                cpu_quota_usec=host_cpu_quota_usec,
                cpu_period_usec=host_cpu_period_usec,
            )
            latest_endpoint_history_at = _as_utc(
                session.scalar(
                    select(func.max(EndpointTelemetrySnapshot.observed_at)).where(
                        EndpointTelemetrySnapshot.endpoint_id == endpoint.id
                    )
                )
            )
            endpoint_history_points_written = 0
            if (
                latest_endpoint_history_at is None
                or (observed_at - latest_endpoint_history_at).total_seconds()
                >= TELEMETRY_HISTORY_INTERVAL_SECONDS
            ):
                session.add(
                    EndpointTelemetrySnapshot(
                        endpoint_id=endpoint.id,
                        observed_at=observed_at,
                        collected_at=now,
                        cpu_count=observation.host.cpu_count,
                        load_1m=observation.host.load_1m,
                        cpu_total_ticks=host_cpu_total_ticks,
                        cpu_idle_ticks=host_cpu_idle_ticks,
                        cpu_usage_usec=host_cpu_usage_usec,
                        cpu_quota_usec=host_cpu_quota_usec,
                        cpu_period_usec=host_cpu_period_usec,
                        cpu_utilization_pct=host_cpu_utilization_pct,
                        memory_total_mib=observation.host.memory_total_mib,
                        memory_available_mib=observation.host.memory_available_mib,
                        memory_limit_mib=observation.host.memory_limit_mib,
                        memory_current_mib=observation.host.memory_current_mib,
                        provider=provider,
                    )
                )
                endpoint_history_points_written = 1
            if host_telemetry is None:
                host_telemetry = EndpointTelemetryCurrent(
                    endpoint_id=endpoint.id,
                    observed_at=observed_at,
                    collected_at=now,
                    cpu_count=observation.host.cpu_count,
                    load_1m=observation.host.load_1m,
                    cpu_total_ticks=host_cpu_total_ticks,
                    cpu_idle_ticks=host_cpu_idle_ticks,
                    cpu_usage_usec=host_cpu_usage_usec,
                    cpu_quota_usec=host_cpu_quota_usec,
                    cpu_period_usec=host_cpu_period_usec,
                    cpu_utilization_pct=host_cpu_utilization_pct,
                    memory_total_mib=observation.host.memory_total_mib,
                    memory_available_mib=observation.host.memory_available_mib,
                    memory_limit_mib=observation.host.memory_limit_mib,
                    memory_current_mib=observation.host.memory_current_mib,
                    provider=provider,
                )
                session.add(host_telemetry)
            else:
                host_telemetry.observed_at = observed_at
                host_telemetry.collected_at = now
                host_telemetry.cpu_count = observation.host.cpu_count
                host_telemetry.load_1m = observation.host.load_1m
                host_telemetry.cpu_total_ticks = host_cpu_total_ticks
                host_telemetry.cpu_idle_ticks = host_cpu_idle_ticks
                host_telemetry.cpu_usage_usec = host_cpu_usage_usec
                host_telemetry.cpu_quota_usec = host_cpu_quota_usec
                host_telemetry.cpu_period_usec = host_cpu_period_usec
                host_telemetry.cpu_utilization_pct = host_cpu_utilization_pct
                host_telemetry.memory_total_mib = observation.host.memory_total_mib
                host_telemetry.memory_available_mib = observation.host.memory_available_mib
                host_telemetry.memory_limit_mib = observation.host.memory_limit_mib
                host_telemetry.memory_current_mib = observation.host.memory_current_mib
                host_telemetry.provider = provider
            gpu_ids = list(observed_gpu_ids)
            existing_gpus = {
                gpu.id: gpu
                for gpu in session.scalars(select(GPUDevice).where(GPUDevice.id.in_(gpu_ids))).all()
            }
            current_telemetry = {
                item.gpu_id: item
                for item in session.scalars(
                    select(TelemetryCurrent).where(TelemetryCurrent.gpu_id.in_(gpu_ids))
                ).all()
            }
            latest_history = {
                gpu_id: _as_utc(latest_observed_at)
                for gpu_id, latest_observed_at in session.execute(
                    select(TelemetrySnapshot.gpu_id, func.max(TelemetrySnapshot.observed_at))
                    .where(TelemetrySnapshot.gpu_id.in_(gpu_ids))
                    .group_by(TelemetrySnapshot.gpu_id)
                ).all()
            }
            by_uuid: dict[str, GPUDevice] = {}
            history_points_written = 0
            for sample in observation.gpus:
                gpu_id = f"{endpoint.id}:{sample.gpu_uuid}"
                gpu = existing_gpus.get(gpu_id)
                if gpu is None:
                    gpu = GPUDevice(
                        id=gpu_id,
                        endpoint_id=endpoint.id,
                        gpu_uuid=sample.gpu_uuid,
                        gpu_index=sample.gpu_index,
                        cuda_ordinal=sample.cuda_ordinal,
                        name=sample.name,
                        total_vram_mib=sample.total_vram_mib,
                        labels_json="[]",
                        health=sample.health,
                        enabled=True,
                        present=True,
                        first_seen_at=observed_at,
                        last_seen_at=observed_at,
                        absent_at=None,
                    )
                    session.add(gpu)
                else:
                    gpu.gpu_index = sample.gpu_index
                    gpu.cuda_ordinal = sample.cuda_ordinal
                    gpu.name = sample.name
                    gpu.total_vram_mib = sample.total_vram_mib
                    gpu.health = sample.health
                    gpu.present = True
                    gpu.last_seen_at = observed_at
                    gpu.absent_at = None
                by_uuid[sample.gpu_uuid] = gpu
                current = current_telemetry.get(gpu_id)
                if current is None:
                    current = TelemetryCurrent(
                        gpu_id=gpu_id,
                        observed_at=observed_at,
                        collected_at=now,
                        memory_used_mib=sample.memory_used_mib,
                        memory_free_mib=sample.memory_free_mib,
                        gpu_utilization_pct=sample.gpu_utilization_pct,
                        memory_utilization_pct=sample.memory_utilization_pct,
                        temperature_c=sample.temperature_c,
                        power_watts=sample.power_watts,
                        pstate=sample.pstate,
                        health=sample.health,
                        provider=provider,
                    )
                    session.add(current)
                else:
                    current.observed_at = observed_at
                    current.collected_at = now
                    current.memory_used_mib = sample.memory_used_mib
                    current.memory_free_mib = sample.memory_free_mib
                    current.gpu_utilization_pct = sample.gpu_utilization_pct
                    current.memory_utilization_pct = sample.memory_utilization_pct
                    current.temperature_c = sample.temperature_c
                    current.power_watts = sample.power_watts
                    current.pstate = sample.pstate
                    current.health = sample.health
                    current.provider = provider
                last_history_at = latest_history.get(gpu_id)
                if (
                    last_history_at is None
                    or (observed_at - last_history_at).total_seconds()
                    >= TELEMETRY_HISTORY_INTERVAL_SECONDS
                ):
                    session.add(
                        TelemetrySnapshot(
                            gpu_id=gpu_id,
                            observed_at=observed_at,
                            collected_at=now,
                            memory_used_mib=sample.memory_used_mib,
                            memory_free_mib=sample.memory_free_mib,
                            gpu_utilization_pct=sample.gpu_utilization_pct,
                            memory_utilization_pct=sample.memory_utilization_pct,
                            temperature_c=sample.temperature_c,
                            power_watts=sample.power_watts,
                            pstate=sample.pstate,
                            health=sample.health,
                            provider=provider,
                        )
                    )
                    history_points_written += 1
            absent_gpu_count = 0
            if observation.observation_complete:
                handed_over = self._converge_gpu_uuid_ownership(
                    session,
                    endpoint_id=endpoint.id,
                    observed_uuids=by_uuid,
                    observed_at=observed_at,
                    now=now,
                )
                purged = self._purge_unobserved_plugin_gpus(
                    session,
                    endpoint=endpoint,
                    observed_gpu_ids=observed_gpu_ids,
                    now=observed_at,
                )
                prior_gpus = session.scalars(
                    select(GPUDevice).where(GPUDevice.endpoint_id == endpoint.id)
                ).all()
                for gpu in prior_gpus:
                    if gpu.id not in observed_gpu_ids and gpu.present:
                        gpu.present = False
                        gpu.absent_at = observed_at
                        absent_gpu_count += 1
                session.flush()
                revision = self._release_absent_keepalive_leases(
                    session,
                    endpoint_ids={endpoint.id, *handed_over},
                    now=now,
                    revision=revision,
                )
                if purged:
                    session.flush()
            session.flush()
            observed_process_keys: set[tuple[str, int, str, datetime]] = set()
            for process in observation.processes:
                gpu = by_uuid.get(process.gpu_uuid)
                if gpu is None:
                    self._upsert_alert(
                        session,
                        alert_type="unknown_process_gpu",
                        severity="warning",
                        resource_type="endpoint",
                        resource_id=endpoint.id,
                        message="collector reported a compute process for an unobserved GPU UUID",
                        now=now,
                    )
                    continue
                started_at = ensure_utc(process.process_started_at)
                current = session.scalar(
                    select(ProcessObservation).where(
                        ProcessObservation.gpu_id == gpu.id,
                        ProcessObservation.pid == process.pid,
                        ProcessObservation.boot_id == observation.boot_id,
                        ProcessObservation.process_started_at == started_at,
                    )
                )
                if current is None:
                    # `ps etimes` is intentionally used instead of a full
                    # process command line, but it makes the calculated
                    # start timestamp susceptible to a one-second boundary
                    # race. Reuse only the immediately-active identity for
                    # the same GPU/PID/boot when the derived times are very
                    # close; a materially different start time remains a
                    # new, fail-closed process identity.
                    candidate = session.scalar(
                        select(ProcessObservation)
                        .where(
                            ProcessObservation.gpu_id == gpu.id,
                            ProcessObservation.pid == process.pid,
                            ProcessObservation.boot_id == observation.boot_id,
                            ProcessObservation.active.is_(True),
                        )
                        .order_by(ProcessObservation.last_seen_at.desc())
                    )
                    candidate_started_at = (
                        _as_utc(candidate.process_started_at) if candidate else None
                    )
                    # `nvidia-smi` can report a host PID that is not visible to
                    # the endpoint's `ps` namespace.  The sealed collector
                    # marks that case with a missing username and has no real
                    # start time, so its conservative fallback is the current
                    # observation time.  Reuse only the *currently active*
                    # GPU/PID/boot identity in that case.  If the process
                    # disappears for one complete observation the candidate is
                    # inactive and a reused PID becomes a new, fail-closed
                    # identity as intended.
                    process_identity_metadata_missing = process.username is None
                    if (
                        candidate is not None
                        and candidate_started_at is not None
                        and (
                            process_identity_metadata_missing
                            or abs((candidate_started_at - started_at).total_seconds())
                            <= PROCESS_START_TIME_JITTER_SECONDS
                        )
                    ):
                        current = candidate
                        started_at = candidate_started_at
                key = (gpu.id, process.pid, observation.boot_id, started_at)
                observed_process_keys.add(key)
                if current is None:
                    session.add(
                        ProcessObservation(
                            endpoint_id=endpoint.id,
                            gpu_id=gpu.id,
                            pid=process.pid,
                            boot_id=observation.boot_id,
                            process_started_at=started_at,
                            username=process.username,
                            executable=self._sanitize_executable(process.executable),
                            used_memory_mib=process.used_memory_mib,
                            first_seen_at=now,
                            last_seen_at=now,
                            observations=1,
                            active=True,
                        )
                    )
                else:
                    current.username = process.username
                    current.executable = self._sanitize_executable(process.executable)
                    current.used_memory_mib = process.used_memory_mib
                    current.last_seen_at = now
                    current.observations += 1
                    current.active = True
            session.flush()
            if observation.observation_complete:
                current_processes = session.scalars(
                    select(ProcessObservation).where(
                        ProcessObservation.endpoint_id == endpoint.id,
                        ProcessObservation.active.is_(True),
                    )
                ).all()
                for prior in current_processes:
                    key = (
                        prior.gpu_id,
                        prior.pid,
                        prior.boot_id,
                        _as_utc(prior.process_started_at),
                    )
                    if key not in observed_process_keys:
                        prior.active = False
            provider_state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == provider, ProviderState.endpoint_id == endpoint.id
                )
            )
            recovered = (
                observation.observation_complete
                and provider_state is not None
                and provider_state.last_error is not None
            )
            incomplete_error = (
                None if observation.observation_complete else "incomplete endpoint observation"
            )
            if provider_state is None:
                session.add(
                    ProviderState(
                        provider=provider,
                        endpoint_id=endpoint.id,
                        last_success_at=now if observation.observation_complete else None,
                        last_attempt_at=now,
                        last_error=incomplete_error,
                        revision=revision,
                    )
                )
            else:
                if observation.observation_complete:
                    provider_state.last_success_at = now
                provider_state.last_attempt_at = now
                provider_state.last_error = incomplete_error
                provider_state.revision = revision
            if recovered:
                for alert in session.scalars(
                    select(Alert).where(
                        Alert.alert_type == "collector_unreachable",
                        Alert.resource_type == "endpoint",
                        Alert.resource_id == endpoint.id,
                        Alert.active.is_(True),
                    )
                ).all():
                    alert.active = False
                    alert.last_seen_at = now
            self._record_observed_keepalive(
                session,
                endpoint_id=endpoint.id,
                observation_complete=observation.observation_complete,
                now=now,
            )
            self._bind_new_workload_processes(
                session,
                endpoint_id=endpoint.id,
                observation_complete=observation.observation_complete,
                now=now,
            )
            self._clear_keepalive_errors_for_assigned_workloads(
                session,
                endpoint_id=endpoint.id,
                observation_complete=observation.observation_complete,
                now=now,
            )
            plugin_releases = self._reconcile_leases(
                session, now, actor_id=f"collector:{endpoint.id}"
            )
            # A fresh observation can make a previously fail-closed request eligible.
            self._allocate_queued(session, now, revision)
            event = None
            if recovered:
                event = self._audit(
                    session,
                    actor_id=f"collector:{endpoint.id}",
                    action="telemetry.recovered",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    after={
                        "gpu_count": len(observation.gpus),
                        "process_count": len(observation.processes),
                    },
                    summary={"provider": provider, "revision": revision},
                    now=now,
                )
            return {
                "event_id": event.id if event else None,
                "snapshot_revision": revision,
                "endpoint_id": endpoint.id,
                "gpu_count": len(observation.gpus),
                "absent_gpu_count": absent_gpu_count,
                "observation_complete": observation.observation_complete,
                "process_count": len(observation.processes),
                "history_points_written": history_points_written,
                "endpoint_history_points_written": endpoint_history_points_written,
                "ignored": False,
                "_plugin_releases": plugin_releases,
            }

        result = self._write(operation)
        self._release_plugin_allocations(result.pop("_plugin_releases", []))
        return result

    def _bind_new_workload_processes(
        self,
        session: Session,
        *,
        endpoint_id: str,
        observation_complete: bool,
        now: datetime,
    ) -> None:
        """Record initial observed workload activity for a routine lease.

        The lease's durable task-to-GPU assignment is independent of the
        collected PID cohort.  Observed process identities are retained for
        display and auditing, but later worker turnover (including a mixed
        bridge-to-replacement transition) must not reassign or conflict the
        existing task lease.
        """

        lease_ids = session.scalars(
            select(LeaseResource.lease_id)
            .join(GPUDevice, GPUDevice.id == LeaseResource.gpu_id)
            .where(
                LeaseResource.active.is_(True),
                GPUDevice.endpoint_id == endpoint_id,
            )
        ).all()
        if not lease_ids:
            return
        leases = session.scalars(
            select(Lease).where(
                Lease.id.in_(lease_ids),
                Lease.kind == "workload",
                Lease.state.in_({"HELD", "ACTIVE", "CONFLICT"}),
            )
        ).all()
        for lease in leases:
            bindings = session.scalars(
                select(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id)
            ).all()
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            processes_by_gpu = {
                resource.gpu_id: self._current_processes(session, resource.gpu_id, now)
                for resource in resources
            }
            if not resources or any(
                not processes_by_gpu[resource.gpu_id] for resource in resources
            ):
                continue
            # A binding records the original observed start only. It never
            # claims ownership of a later PID cohort; the GPU assignment is
            # the durable ownership record.
            if bindings:
                continue
            process_keys = sorted(
                {
                    self._process_key(process)
                    for processes in processes_by_gpu.values()
                    for process in processes
                }
            )
            run_id = f"collector:lease:{lease.id}"
            before = self._lease_dict(session, lease)
            session.add(
                WorkloadBinding(
                    lease_id=lease.id,
                    run_id=run_id,
                    process_keys_json=json_dump(process_keys),
                    created_at=now,
                )
            )
            lease.state = "ACTIVE"
            lease.activated_at = now
            lease.last_heartbeat_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "ACTIVE"
                request.updated_at = now
            actor_id = f"collector:{endpoint_id}"
            self._audit(
                session,
                actor_id=actor_id,
                action="lease.activated",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"activation": "observed_workload_bind", "run_id": run_id},
                now=now,
            )
            self._audit(
                session,
                actor_id=actor_id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": run_id, "process_key_count": len(process_keys)},
                summary={
                    "source": "collector_observed",
                    "gpu_count": len(resources),
                },
                now=now,
            )

    @staticmethod
    def _sanitize_executable(value: str) -> str:
        # Collector never stores cmdline/cwd/environment. Keep a bounded basename-like label only.
        stripped = value.replace("\\x00", " ").replace("\n", " ").strip()
        return stripped.rsplit("/", maxsplit=1)[-1][:255] or "unknown"

    def record_provider_failure(
        self, endpoint_id: str, message: str, *, provider: str = "raw-ssh"
    ) -> None:
        def operation(session: Session) -> None:
            now = utcnow()
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError(
                    "endpoint_not_found", "collector reported an unknown endpoint", status_code=404
                )
            revision = self._bump_revision(session, now)
            state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == provider, ProviderState.endpoint_id == endpoint_id
                )
            )
            first_failure = state is None or state.last_error is None
            last_success = _as_utc(state.last_success_at) if state is not None else None
            if state is None:
                session.add(
                    ProviderState(
                        provider=provider,
                        endpoint_id=endpoint_id,
                        last_success_at=None,
                        last_attempt_at=now,
                        last_error=message[:1000],
                        revision=revision,
                    )
                )
            else:
                state.last_attempt_at = now
                state.last_error = message[:1000]
                state.revision = revision
            # A failed attempt is a fact worth recording, but it is not yet a
            # host in trouble.  The alert waits for the same criterion the
            # monitor status uses -- the last successful observation having
            # gone stale -- so the alert bar cannot contradict an ONLINE host.
            if last_success is None or now - last_success > timedelta(
                seconds=self.inventory.collector.stale_after_seconds
            ):
                self._upsert_alert(
                    session,
                    alert_type="collector_unreachable",
                    severity="warning",
                    resource_type="endpoint",
                    resource_id=endpoint_id,
                    message="collector failed; endpoint telemetry is stale and fails closed",
                    now=now,
                )
            if first_failure:
                self._audit(
                    session,
                    actor_id=f"collector:{endpoint_id}",
                    action="telemetry.failed",
                    resource_type="endpoint",
                    resource_id=endpoint_id,
                    result="failure",
                    summary={"provider": provider, "message": message[:300]},
                    now=now,
                )

        self._write(operation)

    def _upsert_alert(
        self,
        session: Session,
        *,
        alert_type: str,
        severity: str,
        resource_type: str,
        resource_id: str,
        message: str,
        now: datetime,
    ) -> Alert:
        alert = session.scalar(
            select(Alert).where(
                Alert.alert_type == alert_type,
                Alert.resource_type == resource_type,
                Alert.resource_id == resource_id,
                Alert.active.is_(True),
            )
        )
        if alert is None:
            alert = Alert(
                id=secrets.token_hex(16),
                alert_type=alert_type,
                severity=severity,
                resource_type=resource_type,
                resource_id=resource_id,
                message=message[:1000],
                active=True,
                first_seen_at=now,
                last_seen_at=now,
                acknowledged_at=None,
                acknowledged_by=None,
            )
            session.add(alert)
        else:
            alert.severity = severity
            alert.message = message[:1000]
            alert.last_seen_at = now
        return alert

    # ---- atomic scheduling -----------------------------------------------------

    def _project_usage(self, session: Session) -> tuple[dict[str, int], dict[str, int]]:
        gpu_usage: dict[str, int] = defaultdict(int)
        lease_usage: dict[str, int] = defaultdict(int)
        active_leases = session.scalars(
            select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES), Lease.kind == "workload")
        ).all()
        for lease in active_leases:
            lease_usage[lease.project_id] += 1
            gpu_usage[lease.project_id] += len(
                session.scalars(
                    select(LeaseResource.gpu_id).where(
                        LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                    )
                ).all()
            )
        return gpu_usage, lease_usage

    def _project_can_allocate(
        self,
        session: Session,
        project: Project,
        constraints: ResourceConstraints,
        gpu_usage: dict[str, int],
        lease_usage: dict[str, int],
    ) -> str | None:
        if not project.enabled:
            return "project is disabled"
        if (
            project.quota_gpus is not None
            and gpu_usage[project.id] + constraints.gpu_count > project.quota_gpus
        ):
            return f"project GPU quota {project.quota_gpus} would be exceeded"
        if (
            project.concurrency_limit is not None
            and lease_usage[project.id] >= project.concurrency_limit
        ):
            return f"project concurrency limit {project.concurrency_limit} is reached"
        return None

    @staticmethod
    def _ensure_claim_project(session: Session, project_id: str, now: datetime) -> Project:
        """Persist a neutral project tag only because request rows reference it.

        A project id is supplied by the claimant; it is not an enrollment or
        endpoint-access check.  Configured projects can still carry optional
        fairness or quota policy, while first use gets the neutral defaults.
        """

        if project_id == SYSTEM_PROJECT_ID:
            raise BrokerError(
                "reserved_project_id",
                "the ServerPilot internal project cannot be used by workload claims",
                status_code=422,
            )
        project = session.get(Project, project_id)
        if project is not None:
            return project
        project = Project(
            id=project_id,
            display_name=project_id,
            weight=1,
            quota_gpus=None,
            concurrency_limit=None,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(project)
        session.flush()
        return project

    @staticmethod
    def _endpoint_commitment_usage(
        session: Session,
    ) -> dict[str, tuple[float, int]]:
        """Return active direct-lease commitments keyed by endpoint identity."""

        usage: dict[str, tuple[float, int]] = {}
        commitments = session.scalars(
            select(LeaseEndpointCommitment)
            .join(Lease, Lease.id == LeaseEndpointCommitment.lease_id)
            .where(Lease.state.in_(ACTIVE_LEASE_STATES))
        ).all()
        for commitment in commitments:
            cpu, memory = usage.get(commitment.endpoint_id, (0.0, 0))
            usage[commitment.endpoint_id] = (
                cpu + commitment.cpu_cores,
                memory + commitment.memory_mib,
            )
        return usage

    @staticmethod
    def _reserved_gpu_ids(
        session: Session,
        *,
        start: datetime,
        end: datetime,
    ) -> set[str]:
        """Every GPU an active reservation covers in this window, in one query."""

        reserved: set[str] = set()
        for reservation in session.scalars(
            select(Reservation).where(
                Reservation.state == "ACTIVE",
                Reservation.start_at < end,
                Reservation.end_at > start,
            )
        ).all():
            reserved.update(json_load(reservation.gpu_ids_json))
        return reserved

    def _reservation_blocks_gpu(
        self,
        session: Session,
        gpu_id: str,
        *,
        start: datetime,
        end: datetime,
    ) -> bool:
        return gpu_id in self._reserved_gpu_ids(session, start=start, end=end)

    def _eligible_gpus(
        self,
        session: Session,
        *,
        request: AllocationRequest,
        now: datetime,
        include_reclaimable_keepalive: bool = False,
    ) -> tuple[list[GPUDevice], dict[str, int]]:
        constraints = self._request_resource_constraints(request)
        lease_end = now + timedelta(seconds=request.duration_seconds)
        excluded: dict[str, int] = defaultdict(int)
        values: list[GPUDevice] = []
        direct_commitment_usage = self._endpoint_commitment_usage(session)
        generic_host_usage: dict[str, tuple[float, int]] = {}
        selected_group_ids = set(constraints.server_group_ids)
        all_gpus = session.scalars(
            select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
        ).all()
        # A GPU UUID is one physical card. Re-registering a machine on a new
        # forwarded port can leave a second row for that card, and leasing both
        # rows would hand the same card to two callers. Ownership convergence
        # prevents the duplicate; this keeps the allocator itself fail-closed.
        # Scanning the reservation table once per card made the allocator's cost
        # quadratic in a fleet's size; the window is the same for every card.
        reserved_gpu_ids = self._reserved_gpu_ids(session, start=now, end=lease_end)
        leased_gpu_ids_by_uuid: dict[str, set[str]] = defaultdict(set)
        for gpu_uuid, gpu_id in session.execute(
            select(GPUDevice.gpu_uuid, GPUDevice.id)
            .join(LeaseResource, LeaseResource.gpu_id == GPUDevice.id)
            .join(Lease, Lease.id == LeaseResource.lease_id)
            .where(LeaseResource.active.is_(True), Lease.state.in_(ACTIVE_LEASE_STATES))
        ).all():
            leased_gpu_ids_by_uuid[gpu_uuid].add(gpu_id)
        for gpu in all_gpus:
            if not gpu.present:
                excluded["absent"] += 1
                continue
            if leased_gpu_ids_by_uuid[gpu.gpu_uuid] - {gpu.id}:
                excluded["uuid_leased_elsewhere"] += 1
                continue
            if gpu.cuda_ordinal is None:
                excluded["cuda_selector"] += 1
                continue
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None:
                excluded["missing_endpoint"] += 1
                continue
            if selected_group_ids:
                if endpoint.server_group_id not in selected_group_ids:
                    excluded["server_group"] += 1
                    continue
            elif constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                excluded["endpoint_allowlist"] += 1
                continue
            if endpoint.lifecycle_state != "active":
                excluded["endpoint_lifecycle"] += 1
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                excluded["endpoint_denylist"] += 1
                continue
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            # The allocator admits against the same figures the interface shows.
            # A container endpoint owns its cgroup budget, not the node's cores,
            # so `_host_capacity` is the authority on both sides.
            host_capacity = (
                self._host_capacity(host_telemetry) if host_telemetry is not None else None
            )
            if (
                constraints.cpu_cores is not None or constraints.memory_mib is not None
            ) and host_capacity is None:
                excluded["host_telemetry"] += 1
                continue
            if host_capacity is not None:
                direct_cpu, direct_memory = direct_commitment_usage.get(endpoint.id, (0.0, 0))
                generic_cpu, generic_memory = generic_host_usage.get(endpoint.id, (0.0, 0))
                committed_cpu = direct_cpu + generic_cpu
                committed_memory = direct_memory + generic_memory
                if (
                    constraints.cpu_cores is not None
                    and committed_cpu + constraints.cpu_cores > host_capacity["cpu_cores"]
                ):
                    excluded["committed_cpu"] += 1
                    continue
                if (
                    constraints.memory_mib is not None
                    and committed_memory + constraints.memory_mib
                    > host_capacity["memory_total_mib"]
                ):
                    excluded["committed_memory"] += 1
                    continue
            if constraints.min_available_cpu_cores is not None:
                if host_capacity is None:
                    excluded["host_telemetry"] += 1
                    continue
                available_cpu_cores = host_capacity["cpu_available_cores"]
                # A share we cannot compute is not a share we can promise.
                if available_cpu_cores is None:
                    excluded["host_telemetry"] += 1
                    continue
                if available_cpu_cores < constraints.min_available_cpu_cores:
                    excluded["available_cpu"] += 1
                    continue
            if constraints.min_available_memory_mib is not None:
                if host_capacity is None:
                    excluded["host_telemetry"] += 1
                    continue
                if host_capacity["memory_available_mib"] < constraints.min_available_memory_mib:
                    excluded["available_memory"] += 1
                    continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                excluded["gpu_allowlist"] += 1
                continue
            if gpu.id in constraints.deny_gpu_ids:
                excluded["gpu_denylist"] += 1
                continue
            endpoint_labels = set(json_load(endpoint.labels_json))
            gpu_labels = set(json_load(gpu.labels_json))
            if not set(constraints.endpoint_labels).issubset(endpoint_labels):
                excluded["endpoint_labels"] += 1
                continue
            if not set(constraints.gpu_labels).issubset(gpu_labels):
                excluded["gpu_labels"] += 1
                continue
            if (
                constraints.min_total_vram_mib
                and gpu.total_vram_mib < constraints.min_total_vram_mib
            ):
                excluded["total_vram"] += 1
                continue
            state, _reason = self._gpu_state(session, gpu, now)
            reclaimable_keepalive = False
            occupying_lease = self._active_lease_for_gpu(session, gpu.id)
            if occupying_lease is not None and occupying_lease.kind == "keepalive":
                # This opt-in is used only by the pure reclaim planner. A
                # normal claim path never treats keepalive capacity as free:
                # an adapter stop plus fresh empty observation must occur
                # before the caller runs its ordinary claim.
                if not include_reclaimable_keepalive:
                    excluded["keepalive"] += 1
                    continue
                # A keeper we cannot currently observe is not reclaimable
                # capacity. Stopping it needs the host to answer, and a host
                # that stopped answering is exactly the one whose cards must
                # not be picked: the claim would spend its entire budget on an
                # SSH timeout instead of choosing a reachable host.
                if state not in {"KEEPALIVE", "AVAILABLE"}:
                    excluded[state.lower()] += 1
                    continue
                status, _keepalive_reason = self._keepalive_gpu_status(
                    session, gpu, occupying_lease, now
                )
                if occupying_lease.state != "ACTIVE" or status not in {"ON", "OFF"}:
                    excluded["keepalive_not_verified"] += 1
                    continue
                reclaimable_keepalive = True
            elif state != "AVAILABLE":
                excluded[state.lower()] += 1
                continue
            telemetry = self._latest_telemetry(session, gpu.id)
            assert telemetry is not None  # AVAILABLE implies fresh telemetry
            if (
                constraints.min_free_vram_mib is not None
                and telemetry.memory_free_mib < constraints.min_free_vram_mib
                and not reclaimable_keepalive
            ):
                excluded["free_vram"] += 1
                continue
            if gpu.id in reserved_gpu_ids:
                excluded["future_reservation"] += 1
                continue
            values.append(gpu)
        return values, dict(excluded)

    def plan_keepalive_reclaim(self, request_data: RequestCreate) -> dict[str, Any]:
        """Purely plan exact verified keepalive GPUs that could satisfy a claim.

        The planner deliberately does not alter a lease or adapter. It uses
        the allocator's own filter and topology selector with ``KEEPALIVE``
        admitted only as a temporary planning candidate. A caller may reclaim
        only the returned per-GPU set, obtain fresh empty telemetry, finalize
        those leases, and then run the ordinary claim through its normal
        admission path.
        """

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            request = AllocationRequest(
                id="keepalive-reclaim-plan",
                actor_id=SYSTEM_ACTOR_ID,
                project_id=request_data.project_id,
                auto_activate=False,
                task_ref=request_data.task_ref,
                purpose=request_data.purpose,
                constraints_json=json_dump(request_data.constraints.model_dump(mode="json")),
                duration_seconds=request_data.duration_seconds,
                expected_duration_seconds=None,
                start_after=None,
                deadline=None,
                approval_ref=request_data.approval_ref,
                state="QUEUED",
                priority_class="normal",
                blocked_reason=None,
                created_at=now,
                updated_at=now,
            )
            candidates, excluded = self._eligible_gpus(
                session,
                request=request,
                now=now,
                include_reclaimable_keepalive=True,
            )
            selected = self._select_resources_in_group_partitions(
                session, candidates, request_data.constraints
            )
            if selected is None:
                return {
                    "snapshot_revision": self._revision(session),
                    "complete": False,
                    "transitions": [],
                    "excluded": excluded,
                }
            transitions: list[dict[str, Any]] = []
            for gpu in selected:
                lease = self._active_lease_for_gpu(session, gpu.id)
                if lease is None or lease.kind != "keepalive":
                    continue
                status, _reason = self._keepalive_gpu_status(session, gpu, lease, now)
                if lease.state != "ACTIVE" or status not in {"ON", "OFF"}:
                    return {
                        "snapshot_revision": self._revision(session),
                        "complete": False,
                        "transitions": [],
                        "excluded": {**excluded, "keepalive_not_verified": 1},
                    }
                transitions.append(
                    {
                        "action": "reclaim",
                        "endpoint_id": gpu.endpoint_id,
                        "gpu_id": gpu.id,
                        "gpu_uuid": gpu.gpu_uuid,
                        "lease_id": lease.id,
                    }
                )
            return {
                "snapshot_revision": self._revision(session),
                "complete": True,
                "transitions": transitions,
                "excluded": excluded,
            }

        return self._read(operation)

    @staticmethod
    def _select_resources(
        candidates: list[GPUDevice], constraints: ResourceConstraints
    ) -> list[GPUDevice] | None:
        if len(candidates) < constraints.gpu_count:
            return None
        by_endpoint: dict[str, list[GPUDevice]] = defaultdict(list)
        for gpu in candidates:
            by_endpoint[gpu.endpoint_id].append(gpu)
        for values in by_endpoint.values():
            values.sort(key=lambda item: item.gpu_index)

        if constraints.placement == "exact":
            by_id = {gpu.id: gpu for gpu in candidates}
            try:
                return [by_id[gpu_id] for gpu_id in constraints.gpu_ids]
            except KeyError:
                return None

        # Best fit is the single host-ordering rule: prefer the host with the
        # fewest eligible GPUs that can still serve what is asked of it, so a
        # host holding a large free block stays intact for a request that needs
        # the whole block. A single card must not break the last eight-card
        # machine. Ties resolve on endpoint id, which is unique, so the order is
        # total. Placement does not participate: once the host count is fixed,
        # pack and spread would choose the same hosts.
        def best_fit(hosts: list[tuple[str, list[GPUDevice]]]) -> list[tuple[str, list[GPUDevice]]]:
            return sorted(hosts, key=lambda item: (len(item[1]), item[0]))

        per_node = constraints.gpus_per_node
        if per_node is not None:
            hosts = best_fit(
                [item for item in by_endpoint.items() if len(item[1]) >= per_node]
            )
            if len(hosts) < constraints.nodes:
                return None
            selected: list[GPUDevice] = []
            for _endpoint_id, values in hosts[: constraints.nodes]:
                selected.extend(values[:per_node])
            return selected if len(selected) == constraints.gpu_count else None

        fitting = best_fit(
            [item for item in by_endpoint.items() if len(item[1]) >= constraints.gpu_count]
        )

        if constraints.same_host:
            if not fitting:
                return None
            return fitting[0][1][: constraints.gpu_count]

        if constraints.placement == "pack":
            if fitting:
                return fitting[0][1][: constraints.gpu_count]
            # Nothing fits alone. Spanning stays possible, but consume the
            # largest blocks first so it costs the fewest hosts.
            ordered = [
                gpu
                for _endpoint_id, values in sorted(
                    by_endpoint.items(), key=lambda item: (-len(item[1]), item[0])
                )
                for gpu in values
            ]
            return ordered[: constraints.gpu_count]

        # spread: take one GPU per endpoint in rounds, then fill deterministically.
        selected = []
        queues = [values[:] for _endpoint, values in sorted(by_endpoint.items())]
        while queues and len(selected) < constraints.gpu_count:
            next_queues: list[list[GPUDevice]] = []
            for values in queues:
                if len(selected) >= constraints.gpu_count:
                    break
                if values:
                    selected.append(values.pop(0))
                if values:
                    next_queues.append(values)
            queues = next_queues
        return selected if len(selected) == constraints.gpu_count else None

    def _select_resources_in_group_partitions(
        self,
        session: Session,
        candidates: list[GPUDevice],
        constraints: ResourceConstraints,
    ) -> list[GPUDevice] | None:
        """Choose GPUs inside one server-group partition only.

        Ungrouped endpoints share one partition. Existing host topology and
        best-fit stay in ``_select_resources``; this only prevents a lease from
        mixing two groups, or a group with ungrouped hosts.
        """

        if not candidates:
            return None
        partitions: dict[str | None, list[GPUDevice]] = defaultdict(list)
        for gpu in candidates:
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            partitions[None if endpoint is None else endpoint.server_group_id].append(gpu)
        chosen: list[GPUDevice] | None = None
        chosen_score: tuple[int, int, str, str] | None = None
        for group_id, gpus in partitions.items():
            selected = self._select_resources(gpus, constraints)
            if selected is None:
                continue
            used = {gpu.endpoint_id for gpu in selected}
            eligible_by_host: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                eligible_by_host[gpu.endpoint_id] += 1
            host_score = min((eligible_by_host[host_id], host_id) for host_id in used)
            score = (len(used), host_score[0], host_score[1], group_id or "")
            if chosen_score is None or score < chosen_score:
                chosen = selected
                chosen_score = score
        return chosen

    def _queue_candidates(self, session: Session, now: datetime) -> list[AllocationRequest]:
        queued = session.scalars(
            select(AllocationRequest)
            .where(
                AllocationRequest.state == "QUEUED",
                AllocationRequest.priority_class == "normal",
            )
            .order_by(AllocationRequest.created_at, AllocationRequest.id)
        ).all()
        valid: list[AllocationRequest] = []
        for request in queued:
            if request.deadline is not None and (_as_utc(request.deadline) or now) <= now:
                request.state = "EXPIRED"
                request.blocked_reason = "deadline passed before allocation"
                request.updated_at = now
                self._audit(
                    session,
                    actor_id=request.actor_id,
                    action="request.expired",
                    resource_type="request",
                    resource_id=request.id,
                    result="success",
                    after={"state": request.state},
                    summary={"reason": request.blocked_reason},
                    now=now,
                )
                continue
            if request.start_after is not None and (_as_utc(request.start_after) or now) > now:
                request.blocked_reason = "waiting for start_after"
                request.updated_at = now
                continue
            valid.append(request)
        return valid

    def _fair_order(
        self,
        session: Session,
        requests: list[AllocationRequest],
        now: datetime,
    ) -> list[AllocationRequest]:
        """Explainable weighted fair order: least active GPUs per project weight, then aging."""

        gpu_usage, _lease_usage = self._project_usage(session)
        projects = {project.id: project for project in session.scalars(select(Project)).all()}
        by_project: dict[str, list[AllocationRequest]] = defaultdict(list)
        for request in requests:
            by_project[request.project_id].append(request)
        ordered: list[AllocationRequest] = []
        while by_project:
            choices: list[tuple[float, float, str]] = []
            for project_id, entries in by_project.items():
                project = projects.get(project_id)
                if project is None:
                    continue
                oldest = _as_utc(entries[0].created_at) or now
                age_seconds = max(0.0, (now - oldest).total_seconds())
                choices.append((gpu_usage[project_id] / project.weight, -age_seconds, project_id))
            if not choices:
                break
            _ratio, _aging, selected_project = min(choices)
            selected_request = by_project[selected_project].pop(0)
            ordered.append(selected_request)
            # Virtual usage makes the order deficit-like: a project cannot win
            # every tie merely because several of its requests were submitted first.
            gpu_usage[selected_project] += self._request_resource_constraints(
                selected_request
            ).gpu_count
            if not by_project[selected_project]:
                del by_project[selected_project]
        return ordered

    def _allocate_queued(
        self,
        session: Session,
        now: datetime,
        revision: int,
        *,
        request_ids: set[str] | None = None,
    ) -> list[str]:
        # ServerPilot no longer keeps a waiting queue. Allocation is attempted
        # only for the request being created; lifecycle hooks must not revive
        # historical QUEUED rows from older versions.
        if not request_ids:
            return []
        allocated: list[str] = []
        requests = [
            request for request in self._queue_candidates(session, now) if request.id in request_ids
        ]
        for request in self._fair_order(session, requests, now):
            project = session.get(Project, request.project_id)
            if project is None:
                request.state = "REJECTED"
                request.blocked_reason = "project no longer exists"
                request.updated_at = now
                continue
            constraints = self._request_resource_constraints(request)
            gpu_usage, lease_usage = self._project_usage(session)
            policy_block = self._project_can_allocate(
                session, project, constraints, gpu_usage, lease_usage
            )
            if policy_block:
                request.blocked_reason = policy_block
                request.updated_at = now
                continue
            candidates, excluded = self._eligible_gpus(session, request=request, now=now)
            resources = self._select_resources_in_group_partitions(
                session, candidates, constraints
            )
            if resources is None:
                top_exclusions = ", ".join(
                    f"{reason}={count}"
                    for reason, count in sorted(
                        excluded.items(), key=lambda item: (-item[1], item[0])
                    )[:3]
                )
                blocked_reason = f"insufficient eligible GPUs: need {constraints.gpu_count}, have {len(candidates)}"
                if top_exclusions:
                    blocked_reason += f"; blocked by {top_exclusions}"
                changed = request.blocked_reason != blocked_reason
                request.blocked_reason = blocked_reason
                request.updated_at = now
                if changed:
                    self._audit(
                        session,
                        actor_id=request.actor_id,
                        action="scheduler.blocked",
                        resource_type="request",
                        resource_id=request.id,
                        result="success",
                        after={"blocked_reason": request.blocked_reason},
                        summary={"excluded": excluded},
                        now=now,
                    )
                continue
            lease = Lease(
                id=secrets.token_hex(16),
                request_id=request.id,
                actor_id=request.actor_id,
                project_id=request.project_id,
                kind="workload",
                state="HELD",
                issued_at=now,
                expires_at=now + timedelta(seconds=request.duration_seconds),
                last_heartbeat_at=now,
                activated_at=None,
                released_at=None,
                release_reason=None,
                issued_revision=revision,
            )
            session.add(lease)
            session.flush()
            for gpu in resources:
                session.add(
                    LeaseResource(lease_id=lease.id, gpu_id=gpu.id, active=True, released_at=None)
                )
            for endpoint_id in sorted({gpu.endpoint_id for gpu in resources}):
                session.add(
                    LeaseEndpointCommitment(
                        lease_id=lease.id,
                        endpoint_id=endpoint_id,
                        cpu_cores=constraints.cpu_cores or 0.0,
                        memory_mib=constraints.memory_mib or 0,
                        created_at=now,
                    )
                )
            request.state = "LEASED"
            request.blocked_reason = None
            request.updated_at = now
            self._audit(
                session,
                actor_id=request.actor_id,
                action="lease.issued",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={
                    "gpu_ids": [gpu.id for gpu in resources],
                    "state": lease.state,
                    "endpoint_commitments": {
                        endpoint_id: {
                            "cpu_cores": constraints.cpu_cores or 0.0,
                            "memory_mib": constraints.memory_mib or 0,
                        }
                        for endpoint_id in sorted({gpu.endpoint_id for gpu in resources})
                    },
                },
                summary={
                    "request_id": request.id,
                    "project_id": request.project_id,
                    "candidate_count": len(candidates),
                    "excluded": excluded,
                    "placement": constraints.placement,
                    "gang_size": len(resources),
                },
                now=now,
            )
            allocated.append(lease.id)
        return allocated

    def _enforce_direct_group_selection(
        self,
        session: Session,
        constraints: ResourceConstraints,
        *,
        plugin_allocation: dict[str, Any] | None,
        now: datetime,
    ) -> None:
        if plugin_allocation is not None or constraints.gpu_count <= 0:
            return
        selected_groups = list(constraints.server_group_ids)
        if selected_groups:
            missing = [
                group_id
                for group_id in selected_groups
                if session.get(ServerGroup, group_id) is None
            ]
            if missing:
                raise BrokerError(
                    "server_group_not_found",
                    f"unknown server_group_ids: {missing}",
                    status_code=404,
                    details={"server_group_ids": missing},
                )
            return
        groups = self._server_groups_by_id(session)
        if not groups:
            return
        pinned_ids = list(constraints.endpoint_ids)
        if pinned_ids:
            from serverpilot.plugins import is_plugin_profile

            grouped_pins: list[str] = []
            for endpoint_id in pinned_ids:
                endpoint = session.get(Endpoint, endpoint_id)
                if endpoint is None:
                    continue
                if is_plugin_profile(endpoint.observation_profile):
                    continue
                if endpoint.server_group_id is not None:
                    grouped_pins.append(endpoint.id)
            if not grouped_pins:
                return
        raise BrokerError(
            "group_selection_required",
            "select a server_group_id before claiming GPUs",
            status_code=409,
            details=self._group_selection_details(session, groups, now),
        )

    def _group_selection_details(
        self,
        session: Session,
        groups: Mapping[str, ServerGroup],
        now: datetime,
    ) -> dict[str, Any]:
        endpoints = session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
        gpus = session.scalars(
            select(GPUDevice).where(GPUDevice.present.is_(True)).order_by(
                GPUDevice.endpoint_id, GPUDevice.gpu_index
            )
        ).all()
        gpus_by_endpoint: dict[str, list[GPUDevice]] = defaultdict(list)
        for gpu in gpus:
            gpus_by_endpoint[gpu.endpoint_id].append(gpu)

        def server_shape(endpoint: Endpoint) -> dict[str, Any]:
            skus: dict[tuple[str, int], dict[str, int]] = {}
            for gpu in gpus_by_endpoint.get(endpoint.id, []):
                key = (gpu.name, gpu.total_vram_mib)
                bucket = skus.setdefault(
                    key,
                    {
                        "name": gpu.name,
                        "vram_mib": gpu.total_vram_mib,
                        "total_count": 0,
                        "available_count": 0,
                    },
                )
                bucket["total_count"] += 1
                state, _reason = self._gpu_state(session, gpu, now)
                if state in {"AVAILABLE", "KEEPALIVE"}:
                    bucket["available_count"] += 1
            return {
                "server_id": endpoint.id,
                "gpus": [
                    skus[key]
                    for key in sorted(skus, key=lambda item: (item[0], item[1]))
                ],
            }

        group_payloads = []
        for group_id in sorted(groups):
            group = groups[group_id]
            members = [
                endpoint for endpoint in endpoints if endpoint.server_group_id == group.id
            ]
            group_payloads.append(
                {
                    **self._server_group_dict(group),
                    "servers": [server_shape(endpoint) for endpoint in members],
                }
            )
        ungrouped = [
            server_shape(endpoint) for endpoint in endpoints if endpoint.server_group_id is None
        ]
        return {"server_groups": group_payloads, "ungrouped_servers": ungrouped}

    def _create_request_in_session(
        self,
        session: Session,
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str | None,
        idempotency_action: str,
        activate_if_allocated: bool,
        persistent_lease: bool = False,
        idempotency_checked: bool = False,
        plugin_allocation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not idempotency_checked and idempotency_key is not None:
            existing = self._idempotent(
                session, actor=actor, action=idempotency_action, key=idempotency_key
            )
            if existing is not None:
                return existing
        now = utcnow()
        project = self._ensure_claim_project(session, request_data.project_id, now)
        if not project.enabled:
            raise BrokerError("project_disabled", "project is disabled", status_code=409)
        self._enforce_direct_group_selection(
            session,
            request_data.constraints,
            plugin_allocation=plugin_allocation,
            now=now,
        )
        revision = self._bump_revision(session, now)
        constraints_payload = request_data.constraints.model_dump(mode="json")
        if plugin_allocation is not None:
            constraints_payload["plugin_allocation"] = plugin_allocation
        request = AllocationRequest(
            id=secrets.token_hex(16),
            actor_id=actor.id,
            project_id=request_data.project_id,
            auto_activate=activate_if_allocated,
            task_ref=request_data.task_ref,
            purpose=request_data.purpose,
            constraints_json=json_dump(constraints_payload),
            duration_seconds=request_data.duration_seconds,
            expected_duration_seconds=None,
            start_after=ensure_utc(request_data.start_after) if request_data.start_after else None,
            deadline=ensure_utc(request_data.deadline) if request_data.deadline else None,
            approval_ref=request_data.approval_ref,
            state="QUEUED",
            priority_class="normal",
            blocked_reason=None,
            created_at=now,
            updated_at=now,
        )
        session.add(request)
        summary = {"project_id": request.project_id, "task_ref": request.task_ref}
        event = self._audit(
            session,
            actor_id=actor.id,
            action="request.created",
            resource_type="request",
            resource_id=request.id,
            result="success",
            after=self._request_dict(request),
            summary=summary,
            now=now,
        )
        session.flush()
        self._allocate_queued(session, now, revision, request_ids={request.id})
        lease = session.scalar(select(Lease).where(Lease.request_id == request.id))
        if lease is None:
            constraints = request_data.constraints
            raise BrokerError(
                "no_capacity",
                f"没有满足本次申请的可用 GPU（需要 {constraints.gpu_count} 张）",
                status_code=409,
            )
        if persistent_lease:
            lease.expires_at = None
        result = {
            "event_id": event.id,
            "snapshot_revision": revision,
            "request": self._request_dict(request),
            "lease": self._lease_dict(session, lease) if lease else None,
            "authority": "这里只分配 GPU；启动任务仍需遵守项目或资源所有者的授权。",
        }
        if idempotency_key is not None:
            self._remember_idempotency(
                session,
                actor=actor,
                action=idempotency_action,
                key=idempotency_key,
                response=result,
                now=now,
            )
        return result

    def _release_plugin_allocation_before_write(
        self, actor: ActorContext, lease_id: str, operator_override: bool
    ) -> None:
        """Give a plugin allocation back before taking the writer lock.

        Only a lease this actor may release and that is not already settled
        gets this far, so a caller cannot cancel someone else's cluster job by
        naming their lease id.
        """

        def operation(session: Session) -> AllocationRequest | None:
            lease = session.get(Lease, lease_id)
            if lease is None or lease.state in TERMINAL_LEASE_STATES:
                return None
            if not self._can_manage_lease(actor, lease) and not operator_override:
                return None
            return session.get(AllocationRequest, lease.request_id)

        request = self._read(operation)
        if request is None:
            return
        try:
            self._release_plugin_allocation_if_needed(request)
        except Exception as exc:
            from serverpilot.plugins import PluginError

            if not isinstance(exc, PluginError):
                raise
            raise BrokerError("plugin_release_failed", str(exc), status_code=409) from exc

    def _release_plugin_allocation_if_needed(self, request: AllocationRequest | None) -> None:
        from serverpilot.plugins import release_plugin

        if request is None:
            return
        constraints = json_load(request.constraints_json)
        if not isinstance(constraints, dict):
            return
        allocation = constraints.get("plugin_allocation")
        if not isinstance(allocation, dict):
            return
        plugin_id = allocation.get("plugin_id")
        allocation_ref = allocation.get("allocation_ref")
        if isinstance(plugin_id, str) and isinstance(allocation_ref, str):
            release_plugin(plugin_id, allocation_ref=allocation_ref)

    def create_request(
        self,
        actor: ActorContext,
        request_data: RequestCreate,
        *,
        idempotency_key: str | None,
        activate_if_allocated: bool = False,
        persistent_lease: bool = False,
        plugin_allocation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            return self._create_request_in_session(
                session,
                actor,
                request_data,
                idempotency_key=idempotency_key,
                idempotency_action="request.create",
                activate_if_allocated=activate_if_allocated,
                persistent_lease=persistent_lease,
                plugin_allocation=plugin_allocation,
            )

        return self._write(operation)

    def cancel_request(
        self, actor: ActorContext, request_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="request.cancel", key=idempotency_key
            )
            if existing is not None:
                return existing
            request = session.get(AllocationRequest, request_id)
            if request is None:
                raise BrokerError("request_not_found", "request does not exist", status_code=404)
            if request.actor_id != actor.id:
                raise BrokerError(
                    "request_forbidden", "cannot cancel another actor's request", status_code=403
                )
            if request.state not in {"QUEUED", "PENDING_APPROVAL"}:
                raise BrokerError(
                    "request_not_cancellable",
                    f"request in state {request.state} cannot be cancelled",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._request_dict(request)
            request.state = "CANCELLED"
            request.blocked_reason = "cancelled by actor"
            request.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="request.cancelled",
                resource_type="request",
                resource_id=request.id,
                result="success",
                before=before,
                after=self._request_dict(request),
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "request": self._request_dict(request),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="request.cancel",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def activate_lease(
        self, actor: ActorContext, lease_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.activate", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "cannot activate another actor's lease", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_activatable",
                    f"lease in state {lease.state} cannot be activated",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "ACTIVE"
            lease.activated_at = lease.activated_at or now
            lease.last_heartbeat_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "ACTIVE"
                request.updated_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.activated",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "authority": "Activation records lease use; it does not launch a workload.",
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.activate",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def renew_lease(
        self, actor: ActorContext, lease_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.renew", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "不能续期其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_renewable",
                    "当前状态不能续期这个 GPU 租约",
                    status_code=409,
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            issued_at = _as_utc(lease.issued_at) or now
            expires_at = _as_utc(lease.expires_at) or now
            duration = max(60, int((expires_at - issued_at).total_seconds()))
            renewed_expiry = max(expires_at, now) + timedelta(seconds=duration)
            reserved_gpu_ids = [
                resource.gpu_id
                for resource in session.scalars(
                    select(LeaseResource).where(
                        LeaseResource.lease_id == lease.id,
                        LeaseResource.active.is_(True),
                    )
                ).all()
                if self._reservation_blocks_gpu(
                    session, resource.gpu_id, start=now, end=renewed_expiry
                )
            ]
            if reserved_gpu_ids:
                raise BrokerError(
                    "lease_renewal_conflicts_with_reservation",
                    "lease renewal would overlap a future GPU reservation",
                    status_code=409,
                    details={"gpu_ids": reserved_gpu_ids},
                )
            lease.expires_at = renewed_expiry
            lease.last_heartbeat_at = now
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.renewed",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.renew",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def release_lease(
        self,
        actor: ActorContext,
        lease_id: str,
        *,
        reason: str,
        idempotency_key: str | None,
        operator_override: bool = False,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)
        if operator_override and actor.role not in {"operator", "admin"}:
            raise BrokerError(
                "operator_role_required",
                "human lease correction requires an operator role",
                status_code=403,
            )
        if not reason.strip():
            raise BrokerError("release_reason_required", "释放 GPU 时需要提供原因", status_code=422)

        # A plugin release runs a subprocess, which must not happen while the
        # single writer lock is held. Doing it first means a failed write leaves
        # a cluster allocation already given back, so the ledger is repaired by
        # the next reconcile rather than by holding the lock.
        self._release_plugin_allocation_before_write(actor, lease_id, operator_override)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            if idempotency_key is not None:
                existing = self._idempotent(
                    session, actor=actor, action="lease.release", key=idempotency_key
                )
                if existing is not None:
                    return existing
            if not self._can_manage_lease(actor, lease) and not operator_override:
                raise BrokerError(
                    "lease_forbidden", "不能释放其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state in TERMINAL_LEASE_STATES:
                raise BrokerError(
                    "lease_already_released", "这个 GPU 租约已经结束", status_code=409
                )
            now = utcnow()
            request = session.get(AllocationRequest, lease.request_id)
            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = reason.strip()[:500]
            for resource in session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all():
                resource.active = False
                resource.released_at = now
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.released",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"reason": lease.release_reason},
                now=now,
            )
            # A retained/unknown process still blocks eligibility after this release.
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            if idempotency_key is not None:
                self._remember_idempotency(
                    session,
                    actor=actor,
                    action="lease.release",
                    key=idempotency_key,
                    response=result,
                    now=now,
                )
            return result

        return self._write(operation)

    def _workload_reassignment_targets(
        self,
        session: Session,
        actor: ActorContext,
        lease_id: str,
        gpu_ids: list[str],
        *,
        operator_override: bool,
    ) -> tuple[Lease, list[LeaseResource], list[GPUDevice]]:
        lease = session.get(Lease, lease_id)
        if lease is None:
            raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
        self._require_lease_reassignment_authorization(
            actor,
            lease,
            operator_override=operator_override,
        )
        if lease.kind != "workload" or lease.state not in ACTIVE_LEASE_STATES:
            raise BrokerError(
                "workload_lease_required",
                "only a current workload assignment can be moved",
                status_code=409,
            )
        current_resources = session.scalars(
            select(LeaseResource).where(
                LeaseResource.lease_id == lease.id,
                LeaseResource.active.is_(True),
            )
        ).all()
        if len(gpu_ids) != len(current_resources) or len(set(gpu_ids)) != len(gpu_ids):
            raise BrokerError(
                "gpu_count_mismatch",
                "select the same number of distinct GPUs as the current task",
                status_code=422,
                details={"gpu_count": len(current_resources)},
            )
        target_gpus = [session.get(GPUDevice, gpu_id) for gpu_id in gpu_ids]
        if any(gpu is None for gpu in target_gpus):
            raise BrokerError(
                "gpu_not_found",
                "one or more selected GPUs do not exist",
                status_code=404,
            )
        return lease, current_resources, [gpu for gpu in target_gpus if gpu is not None]

    def keepalive_reclaim_request_for_reassignment(
        self,
        actor: ActorContext,
        lease_id: str,
        gpu_ids: list[str],
        *,
        operator_override: bool = False,
    ) -> RequestCreate | None:
        """Describe only the selected occupancy GPUs that block an APP move."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> RequestCreate | None:
            lease, _current_resources, _target_gpus = self._workload_reassignment_targets(
                session,
                actor,
                lease_id,
                gpu_ids,
                operator_override=operator_override,
            )
            occupied = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.gpu_id.in_(gpu_ids),
                    LeaseResource.active.is_(True),
                    LeaseResource.lease_id != lease.id,
                )
            ).all()
            if not occupied:
                return None
            keeper_gpu_ids: list[str] = []
            for resource in occupied:
                occupying_lease = session.get(Lease, resource.lease_id)
                if occupying_lease is None or occupying_lease.kind != "keepalive":
                    raise BrokerError(
                        "gpu_already_assigned",
                        "move or release the task already assigned to the selected GPU first",
                        status_code=409,
                        details={"gpu_ids": sorted({item.gpu_id for item in occupied})},
                    )
                keeper_gpu_ids.append(resource.gpu_id)
            return RequestCreate.model_validate(
                {
                    "project_id": lease.project_id,
                    "task_ref": f"reassign:{lease.id}",
                    "purpose": "APP task GPU reassignment",
                    "constraints": {
                        "gpu_count": len(keeper_gpu_ids),
                        "gpu_ids": sorted(keeper_gpu_ids),
                        "placement": "exact",
                    },
                }
            )

        return self._read(operation)

    def reassign_lease_gpus(
        self,
        actor: ActorContext,
        lease_id: str,
        gpu_ids: list[str],
        *,
        idempotency_key: str,
        operator_override: bool = False,
    ) -> dict[str, Any]:
        """Move a workload assignment to the exact GPUs chosen in the APP.

        This changes ServerPilot's assignment only. The operator or owning Agent
        restarts the workload with the returned CUDA selectors.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            lease, current_resources, target_gpus = self._workload_reassignment_targets(
                session,
                actor,
                lease_id,
                gpu_ids,
                operator_override=operator_override,
            )
            existing = self._idempotent(
                session, actor=actor, action="lease.gpus.reassign", key=idempotency_key
            )
            if existing is not None:
                return existing
            occupied = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.gpu_id.in_(gpu_ids),
                    LeaseResource.active.is_(True),
                    LeaseResource.lease_id != lease.id,
                )
            ).all()
            if occupied:
                raise BrokerError(
                    "gpu_already_assigned",
                    "move or release the task already assigned to the selected GPU first",
                    status_code=409,
                    details={"gpu_ids": sorted({item.gpu_id for item in occupied})},
                )

            now = utcnow()
            before = self._lease_dict(session, lease)
            for resource in current_resources:
                resource.active = False
                resource.released_at = now
            session.flush()
            for gpu_id in gpu_ids:
                resource = session.scalar(
                    select(LeaseResource).where(
                        LeaseResource.lease_id == lease.id,
                        LeaseResource.gpu_id == gpu_id,
                    )
                )
                if resource is None:
                    session.add(
                        LeaseResource(
                            lease_id=lease.id,
                            gpu_id=gpu_id,
                            active=True,
                            released_at=None,
                        )
                    )
                else:
                    resource.active = True
                    resource.released_at = None
            session.execute(delete(WorkloadBinding).where(WorkloadBinding.lease_id == lease.id))
            session.execute(
                delete(LeaseEndpointCommitment).where(LeaseEndpointCommitment.lease_id == lease.id)
            )
            request = session.get(AllocationRequest, lease.request_id)
            constraints = (
                self._request_resource_constraints(request) if request is not None else None
            )
            endpoint_ids = sorted({gpu.endpoint_id for gpu in target_gpus})
            for endpoint_id in endpoint_ids:
                session.add(
                    LeaseEndpointCommitment(
                        lease_id=lease.id,
                        endpoint_id=endpoint_id,
                        cpu_cores=constraints.cpu_cores
                        if constraints and constraints.cpu_cores
                        else 0.0,
                        memory_mib=constraints.memory_mib
                        if constraints and constraints.memory_mib
                        else 0,
                        created_at=now,
                    )
                )
            if request is not None and constraints is not None:
                request.constraints_json = json_dump(
                    constraints.model_copy(
                        update={
                            "gpu_ids": list(gpu_ids),
                            "endpoint_ids": endpoint_ids,
                            "placement": "exact",
                        }
                    ).model_dump(mode="json")
                )
                request.updated_at = now
            revision = self._bump_revision(session, now)
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.gpus_reassigned",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"gpu_ids": gpu_ids},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "restart_required": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.gpus.reassign",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def release_empty_conflicted_lease(
        self,
        actor: ActorContext,
        endpoint_id: str,
        lease_id: str,
        *,
        observation_not_before: datetime,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Release one empty workload or keepalive lease after fresh evidence.

        A conflict is deliberately not auto-cleared by collection: it means a
        prior lease/process binding diverged, and the original actor may have
        left a real workload behind.  Endpoint operators get this narrow
        recovery path only after the REST layer has collected the endpoint and
        this method verifies a complete, fresh, process-free observation for
        every GPU still owned by the lease. GPU utilization alone is never
        treated as proof of emptiness. The same proof applies to internal
        per-GPU keepalive leases so a failed stop cannot permanently wedge a
        GPU that a human needs to recover.

        The proof is waived in exactly one case: the endpoint has stopped
        answering altogether. Demanding evidence from a machine that is gone is
        what wedges the cards, and the ledger it protects has stopped
        protecting anything. The release then records that the server was
        unreachable rather than claiming its GPUs were observed empty.
        """

        self._require_role(actor, MUTATING_ROLES)
        barrier = ensure_utc(observation_not_before)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session,
                actor=actor,
                action="lease.empty.release",
                key=idempotency_key,
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "找不到这个 GPU 租约", status_code=404)
            if lease.kind not in {"workload", "keepalive"} or lease.state not in {
                "HELD",
                "ACTIVE",
                "ORPHANED_BUSY",
                "CONFLICT",
            }:
                raise BrokerError(
                    "empty_workload_lease_required",
                    "only a held, active, orphaned, or conflicted workload/keepalive lease can be cleared here",
                    status_code=409,
                )
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id,
                    LeaseResource.active.is_(True),
                )
            ).all()
            if not resources:
                raise BrokerError(
                    "lease_has_no_resources",
                    "the lease has no active GPU resources",
                    status_code=409,
                )
            gpu_ids = {resource.gpu_id for resource in resources}
            gpus = [session.get(GPUDevice, gpu_id) for gpu_id in gpu_ids]
            if any(gpu is None or gpu.endpoint_id != endpoint_id for gpu in gpus):
                raise BrokerError(
                    "lease_endpoint_mismatch",
                    "the lease does not belong entirely to this server",
                    status_code=409,
                )

            now = utcnow()
            unreachable = self._endpoint_is_unreachable(session, endpoint_id, now)
            cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            host = session.get(EndpointTelemetryCurrent, endpoint_id)
            provider_state = session.scalar(
                select(ProviderState).where(
                    ProviderState.provider == "raw-ssh",
                    ProviderState.endpoint_id == endpoint_id,
                )
            )
            host_collected_at = _as_utc(host.collected_at) if host is not None else None
            host_observed_at = _as_utc(host.observed_at) if host is not None else None
            last_success_at = (
                _as_utc(provider_state.last_success_at) if provider_state is not None else None
            )
            if not unreachable and (
                host_collected_at is None
                or host_collected_at < barrier
                or host_collected_at < cutoff
                or provider_state is None
                or last_success_at is None
                or last_success_at < barrier
            ):
                raise BrokerError(
                    "conflict_observation_stale",
                    "需要一次完整且最新的服务器采集后才能释放空闲占用",
                    status_code=409,
                )

            for gpu in [] if unreachable else gpus:
                assert gpu is not None
                telemetry = session.get(TelemetryCurrent, gpu.id)
                telemetry_collected_at = (
                    _as_utc(telemetry.collected_at) if telemetry is not None else None
                )
                observed_at = _as_utc(telemetry.observed_at) if telemetry is not None else None
                if not gpu.present:
                    # A vanished GPU cannot still be running this endpoint's
                    # processes. Host freshness above already required a
                    # complete observation.
                    continue
                if (
                    telemetry_collected_at is None
                    or telemetry_collected_at < barrier
                    or observed_at is None
                    or observed_at < cutoff
                    # This card's reading must belong to the same snapshot as
                    # the host's, the way `_validate_keepalive_observation`
                    # requires.  A later probe that refreshed the host but not
                    # this GPU is not an observation of this GPU.
                    or observed_at != host_observed_at
                ):
                    raise BrokerError(
                        "conflict_observation_incomplete",
                        "最新采集没有完整覆盖这张 GPU，暂不释放占用",
                        status_code=409,
                        details={"gpu_id": gpu.id},
                    )
                processes = self._current_processes(session, gpu.id, now)
                if processes:
                    raise BrokerError(
                        "conflict_process_present",
                        "仍观察到运行中的进程，不能释放这张 GPU 的占用",
                        status_code=409,
                        details={"gpu_id": gpu.id, "process_count": len(processes)},
                    )

            revision = self._bump_revision(session, now)
            before = self._lease_dict(session, lease)
            lease.state = "RELEASED"
            lease.released_at = now
            lease.release_reason = (
                "server unreachable; operator settled the ledger"
                if unreachable
                else "empty fresh observation cleared endpoint ownership"
            )
            for resource in resources:
                resource.active = False
                resource.released_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "RELEASED"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.empty_cleared",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={
                    "source": "endpoint_operator",
                    "endpoint_id": endpoint_id,
                    "endpoint_unreachable": unreachable,
                    "lease_kind": lease.kind,
                },
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "released": True,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.empty.release",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def bind_workload(
        self,
        actor: ActorContext,
        lease_id: str,
        binding: LeaseBind,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.bind", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "不能绑定其他 Agent 的 GPU 租约", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE"}:
                raise BrokerError(
                    "lease_not_bindable", "only held or active leases can be bound", status_code=409
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            existing_binding = session.scalar(
                select(WorkloadBinding).where(
                    WorkloadBinding.lease_id == lease.id, WorkloadBinding.run_id == binding.run_id
                )
            )
            if existing_binding is None:
                existing_binding = WorkloadBinding(
                    lease_id=lease.id,
                    run_id=binding.run_id,
                    process_keys_json=json_dump(binding.process_keys),
                    created_at=now,
                )
                session.add(existing_binding)
            else:
                existing_binding.process_keys_json = json_dump(binding.process_keys)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": binding.run_id, "process_key_count": len(binding.process_keys)},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.bind",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def bind_observed_workload(
        self,
        actor: ActorContext,
        lease_id: str,
        binding: LeaseObservedBind,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Bind fresh collector-observed processes for the caller's active lease.

        This never starts, stops, or selects a remote process. It only records
        the identities already observed on every GPU held by the specified
        lease, allowing the regular reconciliation loop to distinguish the
        caller's workload from an unmanaged process.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            lease = session.get(Lease, lease_id)
            if lease is None:
                raise BrokerError("lease_not_found", "lease does not exist", status_code=404)
            self._reject_generic_keepalive_mutation(lease)
            existing = self._idempotent(
                session, actor=actor, action="lease.bind_observed", key=idempotency_key
            )
            if existing is not None:
                return existing
            if not self._can_manage_lease(actor, lease):
                raise BrokerError(
                    "lease_forbidden", "cannot bind another actor's lease", status_code=403
                )
            if lease.state not in {"HELD", "ACTIVE", "CONFLICT"}:
                raise BrokerError(
                    "lease_not_bindable",
                    "当前状态不能绑定这个 GPU 租约",
                    status_code=409,
                )
            was_conflict = lease.state == "CONFLICT"
            conflict_before = self._lease_dict(session, lease) if was_conflict else None
            gpu_ids = session.scalars(
                select(LeaseResource.gpu_id).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            if not gpu_ids:
                raise BrokerError("lease_has_no_resources", "这个租约没有活动 GPU", status_code=409)
            now = utcnow()
            cutoff = now - timedelta(seconds=self.inventory.collector.stale_after_seconds)
            processes = session.scalars(
                select(ProcessObservation)
                .where(
                    ProcessObservation.gpu_id.in_(gpu_ids),
                    ProcessObservation.active.is_(True),
                    ProcessObservation.last_seen_at >= cutoff,
                )
                .order_by(ProcessObservation.gpu_id, ProcessObservation.pid)
            ).all()
            observed_gpu_ids = {process.gpu_id for process in processes}
            missing_gpu_ids = sorted(set(gpu_ids).difference(observed_gpu_ids))
            if missing_gpu_ids:
                raise BrokerError(
                    "workload_process_not_observed",
                    "每张已分配 GPU 都检测到新的任务进程后才能完成绑定",
                    status_code=409,
                    details={"missing_gpu_ids": missing_gpu_ids},
                )
            process_keys = sorted({self._process_key(process) for process in processes})
            run_id = binding.run_id or f"explicit:lease:{lease.id}"
            revision = self._bump_revision(session, now)
            if binding.run_id is None:
                collector_bindings = session.scalars(
                    select(WorkloadBinding).where(
                        WorkloadBinding.lease_id == lease.id,
                        WorkloadBinding.run_id.in_(
                            {f"collector:lease:{lease.id}", f"lease:{lease.id}"}
                        ),
                    )
                ).all()
                for collector_binding in collector_bindings:
                    session.delete(collector_binding)
                if collector_bindings:
                    session.flush()
            existing_binding = session.scalar(
                select(WorkloadBinding).where(
                    WorkloadBinding.lease_id == lease.id, WorkloadBinding.run_id == run_id
                )
            )
            if existing_binding is None:
                session.add(
                    WorkloadBinding(
                        lease_id=lease.id,
                        run_id=run_id,
                        process_keys_json=json_dump(process_keys),
                        created_at=now,
                    )
                )
            else:
                existing_binding.process_keys_json = json_dump(process_keys)
            should_promote = lease.state in {"HELD", "CONFLICT"}
            if was_conflict:
                # A lease owner explicitly confirming the current, freshly
                # observed process identities is the safe recovery action for
                # an attribution conflict. It changes no remote workload;
                # the lease remains blocked by any future unknown process.
                lease.state = "HELD"
                for alert in session.scalars(
                    select(Alert).where(
                        Alert.alert_type == "lease_process_conflict",
                        Alert.resource_type == "lease",
                        Alert.resource_id == lease.id,
                        Alert.active.is_(True),
                    )
                ).all():
                    alert.active = False
                    alert.last_seen_at = now
                self._audit(
                    session,
                    actor_id=actor.id,
                    action="lease.conflict_resolved",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=conflict_before,
                    after=self._lease_dict(session, lease),
                    summary={"source": "collector_observed", "run_id": run_id},
                    now=now,
                )
            if should_promote:
                before_activation = (
                    conflict_before if was_conflict else self._lease_dict(session, lease)
                )
                lease.state = "ACTIVE"
                lease.activated_at = lease.activated_at or now
                lease.last_heartbeat_at = now
                request = session.get(AllocationRequest, lease.request_id)
                if request is not None:
                    request.state = "ACTIVE"
                    request.updated_at = now
                self._audit(
                    session,
                    actor_id=actor.id,
                    action="lease.activated",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=before_activation,
                    after=self._lease_dict(session, lease),
                    summary={"activation": "observed_workload_bind", "run_id": run_id},
                    now=now,
                )
            event = self._audit(
                session,
                actor_id=actor.id,
                action="lease.workload_bound",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                after={"run_id": run_id, "process_key_count": len(process_keys)},
                summary={
                    "source": "collector_observed",
                    "gpu_count": len(gpu_ids),
                    "resolved_conflict": was_conflict,
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "lease": self._lease_dict(session, lease),
                "conflict_resolved": was_conflict,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="lease.bind_observed",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def _normalize_legacy_workload_conflicts(
        self,
        session: Session,
        now: datetime,
        *,
        actor_id: str,
    ) -> int:
        """Retire persisted workload PID-attribution conflicts safely.

        ``CONFLICT`` was previously created only when a workload's sampled
        process identity diverged. Workload assignments are now durable
        task-to-GPU records, so normalize those legacy rows at startup as well
        as during reconciliation. Keepalive conflicts deliberately stay out
        of this repair path because their sealed-worker identity remains an
        active controller safety check.
        """

        leases = session.scalars(
            select(Lease).where(
                Lease.kind == "workload",
                Lease.state == "CONFLICT",
            )
        ).all()
        for lease in leases:
            before = self._lease_dict(session, lease)
            lease.state = "ACTIVE"
            lease.last_heartbeat_at = now
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "ACTIVE"
                request.updated_at = now
            self._resolve_lease_alerts(session, lease.id, now)
            self._audit(
                session,
                actor_id=actor_id,
                action="lease.conflict_resolved",
                resource_type="lease",
                resource_id=lease.id,
                result="success",
                before=before,
                after=self._lease_dict(session, lease),
                summary={"source": "retired_process_attribution_policy"},
                now=now,
            )
        return len(leases)

    def _gpu_is_observably_idle(
        self,
        session: Session,
        resource: LeaseResource,
        now: datetime,
    ) -> bool:
        """Return whether this one GPU is currently observed to run nothing."""

        if self._current_processes(session, resource.gpu_id, now):
            return False
        return self._resources_have_fresh_telemetry(session, [resource], now)

    def _reconcile_idle_lease(
        self,
        session: Session,
        lease: Lease,
        resources: list[LeaseResource],
        now: datetime,
        *,
        actor_id: str,
    ) -> bool:
        """Track and reclaim the GPUs of a claim that holds them but runs nothing.

        A routine claim never expires, so nothing else ever returns its GPUs
        when the agent forgets to release or its process dies.  This is the
        two-phase evidence-based path: warn once the idle window is long enough
        to be worth a human's attention, reclaim once it is long enough to be
        certain, and only ever act on *observed* idleness.

        Idleness is tracked per GPU, so a claim that keeps eight cards and uses
        one returns the other seven instead of counting as busy as a whole.  The
        clock of a GPU is reset whenever a process appears on it or its
        telemetry goes stale, so each streak is a continuously observed window
        and a collector outage can never accumulate into a reclaim.
        """

        if not resources:
            return False
        if lease.expires_at is not None:
            # A lease that declared a duration already has a bound, and that
            # window is the user's stated intent: a job may legitimately sit at
            # zero GPU processes during a long CPU phase.  Only claims with no
            # other safety net are reclaimed on observed idleness.
            return False

        alert_after = self.inventory.idle_lease_alert_seconds
        reclaim_after = self.inventory.idle_lease_reclaim_seconds
        reclaimed: list[LeaseResource] = []
        alerting = 0
        for resource in resources:
            if not self._gpu_is_observably_idle(session, resource, now):
                resource.idle_since = None
                continue
            if resource.idle_since is None:
                resource.idle_since = now
                continue
            idle_seconds = (now - (_as_utc(resource.idle_since) or now)).total_seconds()
            if idle_seconds >= reclaim_after:
                reclaimed.append(resource)
            elif idle_seconds >= alert_after:
                alerting += 1

        remaining = [resource for resource in resources if resource not in reclaimed]
        # The lease-level streak stays defined as "every GPU idle", which keeps
        # the whole-claim reclaim below reading the same way it always has.
        idle_starts = [_as_utc(resource.idle_since) for resource in resources]
        lease.idle_since = min(idle_starts) if all(idle_starts) else None

        if not reclaimed:
            if alerting:
                self._upsert_alert(
                    session,
                    alert_type="idle_lease",
                    severity="warning",
                    resource_type="lease",
                    resource_id=lease.id,
                    message=(
                        f"{alerting} of {len(resources)} leased GPU(s) have run no compute "
                        f"process for over {alert_after // 60} minutes; each is reclaimed "
                        f"automatically after {reclaim_after // 60} minutes"
                    ),
                    now=now,
                )
            else:
                self._resolve_idle_lease_alert(session, lease.id, now)
            return False

        before = self._lease_dict(session, lease)
        for resource in reclaimed:
            resource.active = False
            resource.released_at = now
            resource.idle_since = None
        if remaining:
            # Part of the claim is still doing real work, so the lease lives on
            # with a smaller resource set.
            action = "lease.idle_gpu_reclaimed"
        else:
            action = "lease.idle_reclaimed"
            lease.state = "EXPIRED_EMPTY"
            lease.released_at = now
            lease.release_reason = "idle without observed process"
            lease.idle_since = None
            request = session.get(AllocationRequest, lease.request_id)
            if request is not None:
                request.state = "EXPIRED"
                request.updated_at = now
        self._resolve_idle_lease_alert(session, lease.id, now)
        self._audit(
            session,
            actor_id=actor_id,
            action=action,
            resource_type="lease",
            resource_id=lease.id,
            result="success",
            before=before,
            after=self._lease_dict(session, lease),
            summary={
                "reason": "idle without observed process",
                "reclaimed_gpu_count": len(reclaimed),
                "remaining_gpu_count": len(remaining),
            },
            now=now,
        )
        return True

    def _remember_plugin_release(
        self,
        session: Session,
        lease: Lease,
        releases: list[dict[str, str]],
    ) -> None:
        payload = self._plugin_allocation_payload(
            session.get(AllocationRequest, lease.request_id)
        )
        if payload is not None:
            releases.append(payload)

    def _reconcile_leases(self, session: Session, now: datetime, *, actor_id: str) -> list[dict[str, str]]:
        """Reconcile expiry and legacy attribution state; never kill/restart anything."""

        self._normalize_legacy_workload_conflicts(session, now, actor_id=actor_id)
        self._resolve_orphaned_alerts(session, now)
        leases = session.scalars(select(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))).all()
        expired_released = False
        plugin_releases: list[dict[str, str]] = []
        for lease in leases:
            if lease.kind == "keepalive":
                # Keepalive is durable controller ownership, not a timed user
                # workload. Current worker liveness is projected separately as
                # ON/OFF/ERROR and the endpoint policy drives reconciliation.
                lease.expires_at = None
                continue
            resources = session.scalars(
                select(LeaseResource).where(
                    LeaseResource.lease_id == lease.id, LeaseResource.active.is_(True)
                )
            ).all()
            processes = [
                process
                for resource in resources
                for process in self._current_processes(session, resource.gpu_id, now)
            ]
            if lease.state in {"HELD", "ACTIVE"}:
                if self._reconcile_idle_lease(session, lease, resources, now, actor_id=actor_id):
                    # Reclaiming even one GPU frees capacity a queued request
                    # may now fit into.
                    expired_released = True
                if lease.state == "EXPIRED_EMPTY":
                    self._remember_plugin_release(session, lease, plugin_releases)
                    continue
            expires_at = _as_utc(lease.expires_at)
            if lease.state in {"HELD", "ACTIVE"} and expires_at is not None and expires_at <= now:
                before = self._lease_dict(session, lease)
                if processes:
                    lease.state = "ORPHANED_BUSY"
                    self._upsert_alert(
                        session,
                        alert_type="orphaned_busy",
                        severity="critical",
                        resource_type="lease",
                        resource_id=lease.id,
                        message="lease expired but a real compute process is still observed; resource remains blocked",
                        now=now,
                    )
                else:
                    lease.state = "EXPIRED_EMPTY"
                    lease.released_at = now
                    lease.release_reason = "expired without observed process"
                    for resource in resources:
                        resource.active = False
                        resource.released_at = now
                    request = session.get(AllocationRequest, lease.request_id)
                    if request is not None:
                        request.state = "EXPIRED"
                        request.updated_at = now
                    expired_released = True
                    self._remember_plugin_release(session, lease, plugin_releases)
                self._audit(
                    session,
                    actor_id=actor_id,
                    action="lease.expiry_reconciled",
                    resource_type="lease",
                    resource_id=lease.id,
                    result="success",
                    before=before,
                    after=self._lease_dict(session, lease),
                    summary={"reason": lease.release_reason},
                    now=now,
                )
        self._resolve_orphaned_alerts(session, now)
        if expired_released:
            self._allocate_queued(session, now, self._revision(session))
        return plugin_releases

    def reconcile(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            now = utcnow()
            revision = self._bump_revision(session, now)
            plugin_releases = self._reconcile_leases(session, now, actor_id=actor.id)
            self._allocate_queued(session, now, revision)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reconciliation.run",
                resource_type="cluster",
                resource_id="global",
                result="success",
                summary={"revision": revision},
                now=now,
            )
            return {
                "event_id": event.id,
                "snapshot_revision": revision,
                "_plugin_releases": plugin_releases,
            }

        result = self._write(operation)
        self._release_plugin_allocations(result.pop("_plugin_releases", []))
        return result

    # ---- reservation, maintenance, alert and administration -------------------

    def _reservation_candidate_gpus(
        self,
        session: Session,
        *,
        constraints: ResourceConstraints,
        start_at: datetime,
        end_at: datetime,
    ) -> list[str] | None:
        """Resolve a constraint reservation to stable GPU identities at creation time."""

        candidates: list[GPUDevice] = []
        selected_group_ids = set(constraints.server_group_ids)
        for gpu in session.scalars(
            select(GPUDevice).order_by(GPUDevice.endpoint_id, GPUDevice.gpu_index)
        ).all():
            if not gpu.present:
                continue
            endpoint = session.get(Endpoint, gpu.endpoint_id)
            if endpoint is None or not endpoint.enabled or not gpu.enabled:
                continue
            if selected_group_ids and endpoint.server_group_id not in selected_group_ids:
                continue
            if constraints.endpoint_ids and endpoint.id not in constraints.endpoint_ids:
                continue
            if endpoint.id in constraints.deny_endpoint_ids:
                continue
            host_telemetry = session.get(EndpointTelemetryCurrent, endpoint.id)
            host_capacity = (
                self._host_capacity(host_telemetry) if host_telemetry is not None else None
            )
            if constraints.min_available_cpu_cores is not None:
                if host_capacity is None:
                    continue
                available_cpu_cores = host_capacity["cpu_available_cores"]
                if (
                    available_cpu_cores is None
                    or available_cpu_cores < constraints.min_available_cpu_cores
                ):
                    continue
            if constraints.min_available_memory_mib is not None and (
                host_capacity is None
                or host_capacity["memory_available_mib"] < constraints.min_available_memory_mib
            ):
                continue
            if constraints.gpu_ids and gpu.id not in constraints.gpu_ids:
                continue
            if gpu.id in constraints.deny_gpu_ids:
                continue
            if (
                constraints.min_total_vram_mib
                and gpu.total_vram_mib < constraints.min_total_vram_mib
            ):
                continue
            if not set(constraints.endpoint_labels).issubset(set(json_load(endpoint.labels_json))):
                continue
            if not set(constraints.gpu_labels).issubset(set(json_load(gpu.labels_json))):
                continue
            if self._reservation_blocks_gpu(session, gpu.id, start=start_at, end=end_at):
                continue
            candidates.append(gpu)
        selected = self._select_resources_in_group_partitions(
            session, candidates, constraints
        )
        return [gpu.id for gpu in selected] if selected else None

    def create_reservation(
        self,
        actor: ActorContext,
        reservation_data: ReservationCreate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="reservation.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            project = self._ensure_claim_project(session, reservation_data.project_id, now)
            start_at = ensure_utc(reservation_data.start_at)
            end_at = ensure_utc(reservation_data.end_at)
            gpu_ids = list(reservation_data.gpu_ids)
            if not gpu_ids:
                assert reservation_data.constraints is not None
                selected = self._reservation_candidate_gpus(
                    session,
                    constraints=reservation_data.constraints,
                    start_at=start_at,
                    end_at=end_at,
                )
                if selected is None:
                    raise BrokerError(
                        "reservation_capacity_unavailable",
                        "no complete stable GPU set satisfies the future reservation constraints",
                        status_code=409,
                    )
                gpu_ids = selected
            for gpu_id in gpu_ids:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None:
                    raise BrokerError(
                        "gpu_not_found", f"GPU {gpu_id} does not exist", status_code=404
                    )
                if not gpu.present:
                    raise BrokerError(
                        "gpu_absent",
                        f"GPU {gpu_id} is absent from the latest complete endpoint observation",
                        status_code=409,
                    )
                if self._reservation_blocks_gpu(session, gpu_id, start=start_at, end=end_at):
                    raise BrokerError(
                        "reservation_conflict",
                        f"GPU {gpu_id} overlaps an existing reservation",
                        status_code=409,
                    )
                leases = session.scalars(
                    select(Lease)
                    .join(LeaseResource, LeaseResource.lease_id == Lease.id)
                    .where(
                        LeaseResource.gpu_id == gpu_id,
                        LeaseResource.active.is_(True),
                        Lease.state.in_(ACTIVE_LEASE_STATES),
                    )
                ).all()
                if any(
                    (expires_at := _as_utc(lease.expires_at)) is None or expires_at > start_at
                    for lease in leases
                ):
                    raise BrokerError(
                        "reservation_active_lease_conflict",
                        f"GPU {gpu_id} has an active lease that overlaps the reservation start",
                        status_code=409,
                    )
            revision = self._bump_revision(session, now)
            reservation = Reservation(
                id=secrets.token_hex(16),
                actor_id=actor.id,
                project_id=project.id,
                gpu_ids_json=json_dump(gpu_ids),
                constraints_json=json_dump(
                    reservation_data.constraints.model_dump(mode="json")
                    if reservation_data.constraints
                    else {}
                ),
                start_at=start_at,
                end_at=end_at,
                reason=reservation_data.reason,
                state="ACTIVE",
                created_at=now,
            )
            session.add(reservation)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reservation.created",
                resource_type="reservation",
                resource_id=reservation.id,
                result="success",
                after=self._reservation_dict(reservation),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "reservation": self._reservation_dict(reservation),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="reservation.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def cancel_reservation(
        self, actor: ActorContext, reservation_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="reservation.cancel", key=idempotency_key
            )
            if existing is not None:
                return existing
            reservation = session.get(Reservation, reservation_id)
            if reservation is None:
                raise BrokerError(
                    "reservation_not_found", "reservation does not exist", status_code=404
                )
            if reservation.actor_id != actor.id:
                raise BrokerError(
                    "reservation_forbidden",
                    "cannot cancel another actor's reservation",
                    status_code=403,
                )
            if reservation.state != "ACTIVE":
                raise BrokerError(
                    "reservation_not_cancellable", "reservation is not active", status_code=409
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            before = self._reservation_dict(reservation)
            reservation.state = "CANCELLED"
            event = self._audit(
                session,
                actor_id=actor.id,
                action="reservation.cancelled",
                resource_type="reservation",
                resource_id=reservation.id,
                result="success",
                before=before,
                after=self._reservation_dict(reservation),
                now=now,
            )
            self._allocate_queued(session, now, revision)
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "reservation": self._reservation_dict(reservation),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="reservation.cancel",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def create_maintenance(
        self,
        actor: ActorContext,
        maintenance: MaintenanceCreate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, OPERATOR_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="maintenance.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            endpoint_id = maintenance.endpoint_id
            gpu_id = maintenance.gpu_id
            if endpoint_id:
                endpoint = session.get(Endpoint, endpoint_id)
                if endpoint is None:
                    raise BrokerError(
                        "endpoint_not_found", "maintenance endpoint does not exist", status_code=404
                    )
                self._require_endpoint_manager(actor, endpoint)
            if gpu_id:
                gpu = session.get(GPUDevice, gpu_id)
                if gpu is None:
                    raise BrokerError(
                        "gpu_not_found", "maintenance GPU does not exist", status_code=404
                    )
                endpoint = session.get(Endpoint, gpu.endpoint_id)
                if endpoint is None:
                    raise BrokerError(
                        "endpoint_not_found", "GPU endpoint does not exist", status_code=404
                    )
                self._require_endpoint_manager(actor, endpoint)
                endpoint_id = None
            revision = self._bump_revision(session, now)
            window = MaintenanceWindow(
                id=secrets.token_hex(16),
                endpoint_id=endpoint_id,
                gpu_id=gpu_id,
                actor_id=actor.id,
                start_at=ensure_utc(maintenance.start_at),
                end_at=ensure_utc(maintenance.end_at),
                reason=maintenance.reason,
                state="ACTIVE",
                created_at=now,
            )
            session.add(window)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="maintenance.created",
                resource_type="maintenance",
                resource_id=window.id,
                result="success",
                after=self._maintenance_dict(window),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "maintenance": self._maintenance_dict(window),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="maintenance.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def prune_telemetry(
        self,
        actor: ActorContext,
        retention: RetentionPrune,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Explicit retention action; current telemetry, audit and leases are never deleted."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="telemetry.prune", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            cutoff = now - timedelta(seconds=retention.older_than_seconds)
            revision = self._bump_revision(session, now)
            gpu_deleted = (
                session.execute(
                    delete(TelemetrySnapshot).where(TelemetrySnapshot.observed_at < cutoff)
                ).rowcount
                or 0
            )
            endpoint_deleted = (
                session.execute(
                    delete(EndpointTelemetrySnapshot).where(
                        EndpointTelemetrySnapshot.observed_at < cutoff
                    )
                ).rowcount
                or 0
            )
            deleted = gpu_deleted + endpoint_deleted
            event = self._audit(
                session,
                actor_id=actor.id,
                action="telemetry.pruned",
                resource_type="telemetry",
                resource_id="history",
                result="success",
                after={
                    "deleted_count": deleted,
                    "gpu_deleted_count": gpu_deleted,
                    "endpoint_deleted_count": endpoint_deleted,
                    "cutoff": _iso(cutoff),
                },
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "deleted_count": deleted,
                "gpu_deleted_count": gpu_deleted,
                "endpoint_deleted_count": endpoint_deleted,
                "cutoff": _iso(cutoff),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="telemetry.prune",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    @staticmethod
    def _constraints_reference_endpoint(
        constraints_json: str,
        *,
        endpoint_id: str,
        gpu_ids: set[str],
    ) -> bool:
        constraints = json_load(constraints_json)
        if not isinstance(constraints, dict):
            return False
        endpoint_ids = constraints.get("endpoint_ids") or []
        allowed_gpu_ids = constraints.get("gpu_ids") or []
        return endpoint_id in endpoint_ids or bool(gpu_ids.intersection(allowed_gpu_ids))

    def create_endpoint(
        self, actor: ActorContext, endpoint_data: EndpointCreate, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Create one active endpoint; identity is never updated in place."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            if endpoint_data.owner_project_id == SYSTEM_PROJECT_ID:
                raise BrokerError(
                    "reserved_project_id",
                    "the ServerPilot internal project cannot own an endpoint",
                    status_code=422,
                )
            if session.get(Endpoint, endpoint_data.id) is not None:
                raise BrokerError("endpoint_exists", "endpoint id already exists", status_code=409)
            same_address = session.scalar(
                select(Endpoint).where(
                    Endpoint.host == endpoint_data.host,
                    Endpoint.port == endpoint_data.port,
                )
            )
            if same_address is not None:
                raise BrokerError(
                    "endpoint_address_exists",
                    "an immutable endpoint already owns this host:port",
                    status_code=409,
                )
            group = None
            if endpoint_data.server_group_id is not None:
                group = session.get(ServerGroup, endpoint_data.server_group_id)
                if group is None:
                    raise BrokerError(
                        "server_group_not_found",
                        "server group does not exist",
                        status_code=404,
                    )
            stored_override = endpoint_data.stored_workspace_override()
            self._require_effective_workspace(
                override=stored_override,
                group=group,
            )
            now = utcnow()
            revision = self._bump_revision(session, now)
            endpoint = Endpoint(
                id=endpoint_data.id,
                host=endpoint_data.host,
                port=endpoint_data.port,
                ssh_user=endpoint_data.ssh_user,
                ssh_alias=endpoint_data.ssh_alias,
                workspace_path=stored_override,
                observation_profile=endpoint_data.observation_profile,
                keepalive_adapter_id=endpoint_data.keepalive_adapter_id,
                keepalive_policy=endpoint_data.keepalive_policy,
                labels_json=json_dump(endpoint_data.labels),
                storage_group=endpoint_data.storage_group,
                server_group_id=endpoint_data.server_group_id,
                expected_gpu_count=endpoint_data.expected_gpu_count,
                expected_gpu_total_vram_mib=endpoint_data.expected_gpu_total_vram_mib,
                resource_kind="unknown",
                owner_project_id=endpoint_data.owner_project_id,
                lifecycle_state="active",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
            session.add(endpoint)
            session.flush()
            session.execute(
                delete(EndpointDeletion).where(EndpointDeletion.endpoint_id == endpoint.id)
            )
            payload = self._project_endpoint(session, endpoint)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.created",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                after=payload,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "changed": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def update_endpoint(
        self,
        actor: ActorContext,
        endpoint_id: str,
        endpoint_data: EndpointUpdate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Update only safe monitoring metadata on an active or draining endpoint."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.update", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            before = self._project_endpoint(session, endpoint)
            fields = endpoint_data.model_fields_set
            values = endpoint_data.model_dump()
            if values.get("owner_project_id") == SYSTEM_PROJECT_ID:
                raise BrokerError(
                    "reserved_project_id",
                    "the ServerPilot internal project cannot own an endpoint",
                    status_code=422,
                )
            next_group_id = (
                values["server_group_id"] if "server_group_id" in fields else endpoint.server_group_id
            )
            next_group = (
                session.get(ServerGroup, next_group_id) if next_group_id is not None else None
            )
            if next_group_id is not None and next_group is None:
                raise BrokerError(
                    "server_group_not_found",
                    "server group does not exist",
                    status_code=404,
                )
            next_override = (
                endpoint_data.stored_workspace_override()
                if endpoint_data.workspace_override_specified()
                else endpoint.workspace_path
            )
            effective_after = self._require_effective_workspace(
                override=next_override,
                group=next_group,
            )
            effective_before = self._endpoint_effective_workspace(session, endpoint)
            protected_keepalive_fields = {
                "host",
                "port",
                "keepalive_adapter_id",
                "ssh_user",
                "ssh_alias",
                "observation_profile",
            }
            changed_protected_fields = sorted(
                field
                for field in fields.intersection(protected_keepalive_fields)
                if values[field] != getattr(endpoint, field)
            )
            if effective_before != effective_after:
                changed_protected_fields = sorted({*changed_protected_fields, "workspace_path"})
            if (
                changed_protected_fields
                and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
            ):
                raise BrokerError(
                    "keepalive_endpoint_connection_in_use",
                    "stop the active endpoint keepalive before changing its connection or verification settings",
                    status_code=409,
                    details={"fields": changed_protected_fields},
                )
            if (
                "keepalive_adapter_id" in fields
                and values["keepalive_adapter_id"] is None
                and endpoint.keepalive_policy == "idle_keepalive"
            ):
                raise BrokerError(
                    "keepalive_adapter_required",
                    "disable idle keepalive before removing its sealed endpoint adapter",
                    status_code=409,
                )
            changed = False
            for field in fields:
                if field in {"workspace_path", "workspace_path_override"}:
                    continue
                value = values[field]
                attribute = "labels_json" if field == "labels" else field
                if field == "labels":
                    value = json_dump(value)
                if getattr(endpoint, attribute) != value:
                    setattr(endpoint, attribute, value)
                    changed = True
            if (
                endpoint_data.workspace_override_specified()
                and endpoint.workspace_path != next_override
            ):
                endpoint.workspace_path = next_override
                changed = True
            now = utcnow()
            if changed:
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._project_endpoint(session, endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.updated",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "changed": changed,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.update",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def pause_endpoint(
        self,
        actor: ActorContext,
        endpoint_id: str,
        *,
        idempotency_key: str,
        _idempotency_action: str = "endpoint.pause",
    ) -> dict[str, Any]:
        """Move active -> draining without changing collection or existing leases."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action=_idempotency_action, key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = self._project_endpoint(session, endpoint)
            changed = endpoint.lifecycle_state == "active"
            if changed:
                endpoint.lifecycle_state = "draining"
                endpoint.enabled = False
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._project_endpoint(session, endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.paused",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    summary={"collection_continues": True, "history_retained": True},
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "endpoint_id": endpoint.id,
                "changed": changed,
                "history_retained": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action=_idempotency_action,
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def resume_endpoint(
        self, actor: ActorContext, endpoint_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Move draining -> active."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.resume", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            before = self._project_endpoint(session, endpoint)
            changed = endpoint.lifecycle_state == "draining"
            if changed:
                endpoint.lifecycle_state = "active"
                endpoint.enabled = True
                endpoint.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._project_endpoint(session, endpoint)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="endpoint.resumed",
                    resource_type="endpoint",
                    resource_id=endpoint.id,
                    result="success",
                    before=before,
                    after=payload,
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "endpoint": payload,
                "endpoint_id": endpoint.id,
                "changed": changed,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.resume",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def delete_endpoint(
        self, actor: ActorContext, endpoint_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Hard-delete one endpoint from the local control plane.

        Active leases fail closed. Released history that would block SQLite
        RESTRICT is removed first; telemetry and GPUs then follow the endpoint
        CASCADE.
        """

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.delete", key=idempotency_key
            )
            if existing is not None:
                return existing
            endpoint = session.get(Endpoint, endpoint_id)
            if endpoint is None:
                raise BrokerError("endpoint_not_found", "endpoint does not exist", status_code=404)
            self._require_endpoint_manager(actor, endpoint)
            now = utcnow()
            unreachable = self._endpoint_is_unreachable(session, endpoint.id, now)
            if not unreachable and self._endpoint_has_active_leases(session, endpoint.id):
                raise BrokerError(
                    "endpoint_has_active_leases",
                    "这台服务器上还有进行中的租约，请先释放后再删除。",
                    status_code=409,
                )
            # Deleting a server that stopped answering is how an operator says
            # the machine is gone. Refusing until its leases are released was a
            # deadlock: releasing them needs proof from that same machine. The
            # leases are settled here instead, named for what actually
            # happened, so no lease is left pointing at an endpoint row that no
            # longer exists.
            settled = (
                self._settle_leases_for_deleted_endpoint(session, endpoint.id, actor, now=now)
                if unreachable
                else []
            )
            before = self._project_endpoint(session, endpoint)
            tombstone = session.get(EndpointDeletion, endpoint.id)
            if tombstone is None:
                session.add(
                    EndpointDeletion(
                        endpoint_id=endpoint.id,
                        host=endpoint.host,
                        port=endpoint.port,
                        deleted_at=now,
                    )
                )
            else:
                tombstone.host = endpoint.host
                tombstone.port = endpoint.port
                tombstone.deleted_at = now
            self._purge_endpoint_restrict_history(session, endpoint.id, now=now)
            session.delete(endpoint)
            session.flush()
            revision = self._bump_revision(session, now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.deleted",
                resource_type="endpoint",
                resource_id=endpoint_id,
                result="success",
                before=before,
                summary={"unreachable": unreachable, "settled_lease_ids": settled},
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint_id": endpoint_id,
                "endpoint": before,
                "settled_lease_ids": settled,
                "changed": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.delete",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def upsert_endpoint(
        self, actor: ActorContext, endpoint_data: EndpointUpsert, *, idempotency_key: str
    ) -> dict[str, Any]:
        """Create/update inventory metadata while keeping endpoint id and host:port immutable."""

        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="endpoint.upsert", key=idempotency_key
            )
            if existing is not None:
                return existing
            now = utcnow()
            revision = self._bump_revision(session, now)
            endpoint = session.get(Endpoint, endpoint_data.id)
            before = self._project_endpoint(session, endpoint) if endpoint else None
            same_address = session.scalar(
                select(Endpoint).where(
                    Endpoint.host == endpoint_data.host,
                    Endpoint.port == endpoint_data.port,
                )
            )
            if same_address is not None and same_address.id != endpoint_data.id:
                raise BrokerError(
                    "endpoint_address_exists",
                    "an immutable endpoint already owns this host:port",
                    status_code=409,
                )
            if endpoint is None:
                lifecycle_state = endpoint_data.lifecycle_state or "active"
                if lifecycle_state != "active":
                    raise BrokerError(
                        "endpoint_lifecycle_invalid_transition",
                        "a new endpoint must begin active",
                        status_code=409,
                    )
                group = None
                if endpoint_data.server_group_id is not None:
                    group = session.get(ServerGroup, endpoint_data.server_group_id)
                    if group is None:
                        raise BrokerError(
                            "server_group_not_found",
                            "server group does not exist",
                            status_code=404,
                        )
                stored_override = endpoint_data.stored_workspace_override()
                self._require_effective_workspace(
                    override=stored_override,
                    group=group,
                )
                endpoint = Endpoint(
                    id=endpoint_data.id,
                    host=endpoint_data.host,
                    port=endpoint_data.port,
                    ssh_user=endpoint_data.ssh_user,
                    ssh_alias=endpoint_data.ssh_alias,
                    workspace_path=stored_override,
                    observation_profile=endpoint_data.observation_profile,
                    keepalive_adapter_id=endpoint_data.keepalive_adapter_id,
                    keepalive_policy=endpoint_data.keepalive_policy,
                    labels_json=json_dump(endpoint_data.labels),
                    storage_group=endpoint_data.storage_group,
                    server_group_id=endpoint_data.server_group_id,
                    expected_gpu_count=endpoint_data.expected_gpu_count,
                    expected_gpu_total_vram_mib=endpoint_data.expected_gpu_total_vram_mib,
                    owner_project_id=endpoint_data.owner_project_id,
                    lifecycle_state=lifecycle_state,
                    enabled=True,
                    created_at=now,
                    updated_at=now,
                )
                session.add(endpoint)
            else:
                self._require_endpoint_manager(actor, endpoint)
                if (endpoint.host, endpoint.port) != (endpoint_data.host, endpoint_data.port):
                    raise BrokerError(
                        "endpoint_identity_immutable",
                        "existing endpoint id cannot change host:port; create a new endpoint id",
                        status_code=409,
                    )
                next_group_id = (
                    endpoint_data.server_group_id
                    if "server_group_id" in endpoint_data.model_fields_set
                    else endpoint.server_group_id
                )
                next_group = (
                    session.get(ServerGroup, next_group_id) if next_group_id is not None else None
                )
                if next_group_id is not None and next_group is None:
                    raise BrokerError(
                        "server_group_not_found",
                        "server group does not exist",
                        status_code=404,
                    )
                stored_override = endpoint_data.stored_workspace_override()
                effective_after = self._require_effective_workspace(
                    override=stored_override,
                    group=next_group,
                )
                effective_before = self._endpoint_effective_workspace(session, endpoint)
                protected_values = {
                    "ssh_user": endpoint_data.ssh_user,
                    "ssh_alias": endpoint_data.ssh_alias,
                    "workspace_path": effective_after,
                    "observation_profile": endpoint_data.observation_profile,
                    "keepalive_adapter_id": endpoint_data.keepalive_adapter_id,
                }
                current_protected = {
                    "ssh_user": endpoint.ssh_user,
                    "ssh_alias": endpoint.ssh_alias,
                    "workspace_path": effective_before,
                    "observation_profile": endpoint.observation_profile,
                    "keepalive_adapter_id": endpoint.keepalive_adapter_id,
                }
                changed_protected_fields = sorted(
                    field
                    for field, value in protected_values.items()
                    if value != current_protected[field]
                )
                if (
                    changed_protected_fields
                    and self._active_keepalive_for_endpoint(session, endpoint.id) is not None
                ):
                    raise BrokerError(
                        "keepalive_endpoint_connection_in_use",
                        "stop the active endpoint keepalive before changing its connection or verification settings",
                        status_code=409,
                        details={"fields": changed_protected_fields},
                    )
                if endpoint_data.owner_project_id is not None:
                    endpoint.owner_project_id = endpoint_data.owner_project_id
                endpoint.workspace_path = stored_override
                requested_lifecycle = endpoint_data.lifecycle_state
                if requested_lifecycle is None and endpoint_data.enabled is False:
                    requested_lifecycle = "draining"
                if (
                    requested_lifecycle is not None
                    and requested_lifecycle != endpoint.lifecycle_state
                ):
                    allowed_transitions = {
                        "active": {"draining"},
                        "draining": set(),
                    }
                    if requested_lifecycle not in allowed_transitions[endpoint.lifecycle_state]:
                        raise BrokerError(
                            "endpoint_lifecycle_invalid_transition",
                            "endpoint lifecycle can transition only from active to draining",
                            status_code=409,
                        )
                    endpoint.lifecycle_state = requested_lifecycle
                endpoint.ssh_user = endpoint_data.ssh_user
                endpoint.ssh_alias = endpoint_data.ssh_alias
                endpoint.observation_profile = endpoint_data.observation_profile
                endpoint.keepalive_adapter_id = endpoint_data.keepalive_adapter_id
                endpoint.keepalive_policy = endpoint_data.keepalive_policy
                endpoint.labels_json = json_dump(endpoint_data.labels)
                endpoint.storage_group = endpoint_data.storage_group
                if "server_group_id" in endpoint_data.model_fields_set:
                    endpoint.server_group_id = endpoint_data.server_group_id
                endpoint.expected_gpu_count = endpoint_data.expected_gpu_count
                endpoint.expected_gpu_total_vram_mib = endpoint_data.expected_gpu_total_vram_mib
                endpoint.enabled = endpoint.lifecycle_state == "active"
                endpoint.updated_at = now
            session.flush()
            event = self._audit(
                session,
                actor_id=actor.id,
                action="endpoint.upserted",
                resource_type="endpoint",
                resource_id=endpoint.id,
                result="success",
                before=before,
                after=self._project_endpoint(session, endpoint),
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "endpoint": self._project_endpoint(session, endpoint),
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="endpoint.upsert",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def create_server_group(
        self, actor: ActorContext, group_data: ServerGroupCreate, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="server_group.create", key=idempotency_key
            )
            if existing is not None:
                return existing
            if session.get(ServerGroup, group_data.id) is not None:
                raise BrokerError(
                    "server_group_exists", "server group id already exists", status_code=409
                )
            now = utcnow()
            revision = self._bump_revision(session, now)
            group = ServerGroup(
                id=group_data.id,
                display_name=group_data.display_name,
                workspace_path=group_data.workspace_path,
                environment_notes=group_data.environment_notes,
                description=group_data.description,
                created_at=now,
                updated_at=now,
            )
            session.add(group)
            session.flush()
            payload = self._server_group_dict(group)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="server_group.created",
                resource_type="server_group",
                resource_id=group.id,
                result="success",
                after=payload,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "server_group": payload,
                "changed": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="server_group.create",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def update_server_group(
        self,
        actor: ActorContext,
        group_id: str,
        group_data: ServerGroupUpdate,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="server_group.update", key=idempotency_key
            )
            if existing is not None:
                return existing
            group = session.get(ServerGroup, group_id)
            if group is None:
                raise BrokerError(
                    "server_group_not_found", "server group does not exist", status_code=404
                )
            before = self._server_group_dict(group)
            fields = group_data.model_fields_set
            values = group_data.model_dump()
            if "workspace_path" in fields and values["workspace_path"] != group.workspace_path:
                self._reject_group_workspace_change_if_keepalive(
                    session, group.id, fields=["workspace_path"]
                )
            changed = False
            for field in fields:
                value = values[field]
                if getattr(group, field) != value:
                    setattr(group, field, value)
                    changed = True
            now = utcnow()
            if changed:
                group.updated_at = now
                revision = self._bump_revision(session, now)
                session.flush()
                payload = self._server_group_dict(group)
                event = self._audit(
                    session,
                    actor_id=actor.id,
                    action="server_group.updated",
                    resource_type="server_group",
                    resource_id=group.id,
                    result="success",
                    before=before,
                    after=payload,
                    now=now,
                )
                event_id: int | None = event.id
            else:
                revision = self._revision(session)
                payload = before
                event_id = None
            result = {
                "event_id": event_id,
                "snapshot_revision": revision,
                "server_group": payload,
                "changed": changed,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="server_group.update",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    def delete_server_group(
        self, actor: ActorContext, group_id: str, *, idempotency_key: str
    ) -> dict[str, Any]:
        self._require_role(actor, MUTATING_ROLES)

        def operation(session: Session) -> dict[str, Any]:
            existing = self._idempotent(
                session, actor=actor, action="server_group.delete", key=idempotency_key
            )
            if existing is not None:
                return existing
            group = session.get(ServerGroup, group_id)
            if group is None:
                raise BrokerError(
                    "server_group_not_found", "server group does not exist", status_code=404
                )
            member_ids = list(
                session.scalars(
                    select(Endpoint.id).where(Endpoint.server_group_id == group.id)
                ).all()
            )
            if member_ids:
                raise BrokerError(
                    "server_group_has_members",
                    "server group still has member endpoints",
                    status_code=409,
                    details={"endpoint_ids": member_ids},
                )
            now = utcnow()
            before = self._server_group_dict(group)
            session.delete(group)
            session.flush()
            revision = self._bump_revision(session, now)
            event = self._audit(
                session,
                actor_id=actor.id,
                action="server_group.deleted",
                resource_type="server_group",
                resource_id=group_id,
                result="success",
                before=before,
                now=now,
            )
            result = {
                "event_id": event.id,
                "snapshot_revision": revision,
                "server_group_id": group_id,
                "changed": True,
            }
            self._remember_idempotency(
                session,
                actor=actor,
                action="server_group.delete",
                key=idempotency_key,
                response=result,
                now=now,
            )
            return result

        return self._write(operation)

    # ---- filtered read surfaces ------------------------------------------------

    def list_requests(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            requests = session.scalars(
                select(AllocationRequest).order_by(AllocationRequest.created_at.desc())
            ).all()
            visible = [
                self._request_dict(request) for request in requests if request.actor_id == actor.id
            ]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_leases(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            leases = session.scalars(select(Lease).order_by(Lease.issued_at.desc())).all()
            visible = [
                self._lease_dict(session, lease) for lease in leases if lease.actor_id == actor.id
            ]
            return self.envelope(session, visible)

        return self._read(operation)

    def list_reservations(self, actor: ActorContext) -> dict[str, Any]:
        def operation(session: Session) -> dict[str, Any]:
            values = [
                self._reservation_dict(reservation)
                for reservation in session.scalars(
                    select(Reservation).order_by(Reservation.start_at, Reservation.id)
                ).all()
            ]
            return self.envelope(session, values)

        return self._read(operation)

    def list_events(
        self,
        actor: ActorContext,
        *,
        after_id: int = 0,
        limit: int = 200,
        latest_first: bool = False,
    ) -> dict[str, Any]:
        if limit < 1 or limit > 1000:
            raise BrokerError("invalid_limit", "limit must be between 1 and 1000", status_code=422)

        def operation(session: Session) -> dict[str, Any]:
            events = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.id > after_id)
                .order_by(AuditEvent.id.desc() if latest_first else AuditEvent.id)
                .limit(limit)
            ).all()
            values = []
            for event in events:
                # Non-admins see their own event stream plus events for their project leases/requests.
                visible = actor.is_admin or event.actor_id == actor.id
                if not visible and event.resource_type == "lease":
                    lease = session.get(Lease, event.resource_id)
                    visible = lease is not None and lease.project_id in actor.project_ids
                if not visible and event.resource_type == "request":
                    request = session.get(AllocationRequest, event.resource_id)
                    visible = request is not None and request.project_id in actor.project_ids
                if (
                    not visible
                    and event.resource_type == "endpoint"
                    and event.action in {"telemetry.failed", "telemetry.recovered"}
                ):
                    endpoint = session.get(Endpoint, event.resource_id)
                    visible = endpoint is not None and actor.role in {
                        "viewer",
                        "allocator",
                        "operator",
                        "admin",
                    }
                if not visible:
                    continue
                values.append(
                    {
                        "id": event.id,
                        "actor_id": event.actor_id,
                        "action": event.action,
                        "resource_type": event.resource_type,
                        "resource_id": event.resource_id,
                        "result": event.result,
                        "before": json_load(event.before_json) if event.before_json else None,
                        "after": json_load(event.after_json) if event.after_json else None,
                        "summary": json_load(event.summary_json),
                        "created_at": _iso(event.created_at),
                    }
                )
            return self.envelope(session, values)

        return self._read(operation)

    def doctor(self, actor: ActorContext) -> dict[str, Any]:
        self._require_role(actor, {"viewer", "allocator", "operator", "admin"})

        def operation(session: Session) -> dict[str, Any]:
            from serverpilot.mcp_entry import mcp_entry_status
            from serverpilot.plugins import discover_plugins_with_failures

            now = utcnow()
            gpus = session.scalars(select(GPUDevice)).all()
            present_gpus = [gpu for gpu in gpus if gpu.present]
            stale = sum(
                1
                for gpu in present_gpus
                if self._gpu_state(session, gpu, now)[0] in {"UNKNOWN_STALE", "UNKNOWN_RECOVERING"}
            )
            provider_states = session.scalars(
                select(ProviderState).order_by(ProviderState.endpoint_id)
            ).all()
            endpoints = session.scalars(select(Endpoint).order_by(Endpoint.id)).all()
            plugins, plugin_failures = discover_plugins_with_failures()
            plugin_ids = {plugin.plugin_id for plugin in plugins}
            collectors = []
            version_mismatches: list[dict[str, Any]] = []
            next_steps: list[str] = []
            for endpoint in endpoints:
                applies = (
                    endpoint.observation_profile in REMOTE_COLLECTOR_PROFILES
                    or endpoint.observation_profile in plugin_ids
                )
                setting = session.get(RuntimeSetting, self._collector_version_key(endpoint.id))
                reported = setting.value if setting is not None and setting.value else None
                if not applies:
                    status = "not_applicable"
                elif reported is None:
                    status = "unreported"
                    version_mismatches.append(
                        {
                            "component": "collector",
                            "id": endpoint.id,
                            "expected": __version__,
                            "actual": None,
                            "next_step": f"在 {endpoint.id} 上重装与控制面同版本的 collector",
                        }
                    )
                    next_steps.append(f"在 {endpoint.id} 上重装与控制面同版本的 collector")
                elif reported != __version__:
                    status = "mismatch"
                    version_mismatches.append(
                        {
                            "component": "collector",
                            "id": endpoint.id,
                            "expected": __version__,
                            "actual": reported,
                            "next_step": f"在 {endpoint.id} 上重装与控制面同版本的 collector",
                        }
                    )
                    next_steps.append(f"在 {endpoint.id} 上重装与控制面同版本的 collector")
                else:
                    status = "ok"
                collectors.append(
                    {
                        "endpoint_id": endpoint.id,
                        "observation_profile": endpoint.observation_profile,
                        "implementation_version": reported,
                        "reported": reported is not None,
                        "applies": applies,
                        "status": status,
                    }
                )
            mcp = mcp_entry_status()
            mcp_version = __version__ if mcp["available"] else None
            if not mcp["available"]:
                version_mismatches.append(
                    {
                        "component": "mcp",
                        "id": None,
                        "expected": __version__,
                        "actual": None,
                        "next_step": "未找到 serverpilot-mcp，请安装或检查本机 MCP 入口",
                    }
                )
                next_steps.append("未找到 serverpilot-mcp，请安装或检查本机 MCP 入口")
            failure_payload = [
                {
                    "path": str(failure.path),
                    "source": failure.source,
                    "error": failure.error,
                    "plugin_id": failure.plugin_id,
                }
                for failure in plugin_failures
            ]
            for failure in failure_payload:
                label = failure["plugin_id"] or failure["path"]
                next_steps.append(f"插件 {label} 发现失败：{failure['error']}")
            return self.envelope(
                session,
                {
                    "database_ready": self.database.ready(),
                    "snapshot_revision": self._revision(session),
                    "inventory_endpoints": len(endpoints),
                    "discovered_gpus": len(present_gpus),
                    "historical_gpu_records": len(gpus),
                    "stale_or_recovering_gpus": stale,
                    "collector_enabled": self.inventory.collector.enabled,
                    "providers": [
                        {
                            "provider": state.provider,
                            "endpoint_id": state.endpoint_id,
                            "last_success_at": _iso(state.last_success_at),
                            "last_attempt_at": _iso(state.last_attempt_at),
                            "has_error": state.last_error is not None,
                            "revision": state.revision,
                        }
                        for state in provider_states
                    ],
                    "versions": {
                        "control_plane": __version__,
                        "mcp": {
                            "available": mcp["available"],
                            "command": mcp["command"],
                            "version": mcp_version,
                        },
                        "collectors": collectors,
                    },
                    "plugin_discovery_failures": failure_payload,
                    "version_mismatches": version_mismatches,
                    "next_steps": next_steps,
                },
            )

        return self._read(operation)

    def metrics(self) -> str:
        """Small Prometheus-compatible exposition without exposing secrets or task purposes."""

        def operation(session: Session) -> str:
            now = utcnow()
            gpus = session.scalars(select(GPUDevice).where(GPUDevice.present.is_(True))).all()
            states: dict[str, int] = defaultdict(int)
            for gpu in gpus:
                states[self._gpu_state(session, gpu, now)[0]] += 1
            active_leases = session.scalar(
                select(func.count()).select_from(Lease).where(Lease.state.in_(ACTIVE_LEASE_STATES))
            )
            queued = session.scalar(
                select(func.count())
                .select_from(AllocationRequest)
                .where(AllocationRequest.state == "QUEUED")
            )
            lines = [
                "# HELP serverpilot_gpus Number of GPUs by derived state",
                "# TYPE serverpilot_gpus gauge",
            ]
            lines.extend(
                f'serverpilot_gpus{{state="{state}"}} {count}'
                for state, count in sorted(states.items())
            )
            lines.extend(
                [
                    "# HELP serverpilot_active_leases Number of active exclusive leases",
                    "# TYPE serverpilot_active_leases gauge",
                    f"serverpilot_active_leases {active_leases or 0}",
                    "# HELP serverpilot_queued_requests Number of queued allocation requests",
                    "# TYPE serverpilot_queued_requests gauge",
                    f"serverpilot_queued_requests {queued or 0}",
                    "# HELP serverpilot_snapshot_revision Monotonic control-plane revision",
                    "# TYPE serverpilot_snapshot_revision gauge",
                    f"serverpilot_snapshot_revision {self._revision(session)}",
                    "",
                ]
            )
            return "\n".join(lines)

        return self._read(operation)

    def backup(self, actor: ActorContext, destination: str) -> dict[str, Any]:
        self._require_role(actor, ADMIN_ROLES)
        path = self.database.backup(destination=Path(destination))
        return {"path": str(path), "created_at": _iso(utcnow())}
