"""drop scheduler, planning, profile tables, and allocation_requests.profile_id

Revision ID: 20260828_0033
Revises: 20260827_0032
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_0033"
down_revision = "20260827_0032"
branch_labels = None
depends_on = None

_REMOVED_TABLES = (
    "resource_plan_candidates",
    "resource_run_actuals",
    "resource_allocations",
    "resource_plan_evaluations",
    "resource_claims",
    "allocatable_units",
    "resource_providers",
    "workload_profile_grants",
    "scheduler_job_events",
    "scheduler_transfers",
    "scheduler_jobs",
    "workload_profiles",
    "scheduler_targets",
)


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for table in _REMOVED_TABLES:
        if table in existing:
            op.drop_table(table)
    if "allocation_requests" in existing:
        columns = {column["name"] for column in inspector.get_columns("allocation_requests")}
        indexes = {index["name"] for index in inspector.get_indexes("allocation_requests")}
        if "profile_id" in columns:
            with op.batch_alter_table("allocation_requests") as batch:
                if "ix_allocation_requests_profile_id" in indexes:
                    batch.drop_index("ix_allocation_requests_profile_id")
                batch.drop_column("profile_id")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    if "workload_profiles" not in existing:
        op.create_table(
            "workload_profiles",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("purpose", sa.String(length=1000), nullable=False),
            sa.Column("duration_seconds", sa.Integer(), nullable=False),
            sa.Column("constraints_json", sa.Text(), nullable=False),
            sa.Column("runtime_kind", sa.String(length=32), nullable=False, server_default="direct-gpu"),
            sa.Column("scheduler_target_id", sa.String(length=64)),
            sa.Column("scheduler_spec_json", sa.Text()),
            sa.Column("scheduler_script", sa.Text()),
            sa.Column("grant_all_projects", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("retain_submission_body", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        )
        op.create_index("ix_workload_profiles_project_enabled", "workload_profiles", ["project_id", "enabled"])
        op.create_index("ix_workload_profiles_scheduler_target_id", "workload_profiles", ["scheduler_target_id"])
    if "workload_profile_grants" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "workload_profile_grants",
            sa.Column("profile_id", sa.String(length=64), primary_key=True),
            sa.Column("project_id", sa.String(length=64), primary_key=True),
            sa.ForeignKeyConstraint(["profile_id"], ["workload_profiles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        )
    if "scheduler_targets" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "scheduler_targets",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("adapter", sa.String(length=40), nullable=False),
            sa.Column("connection_json", sa.Text(), nullable=False),
            sa.Column("credential_refs_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("capabilities_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("access_hint", sa.String(length=2000), nullable=False),
            sa.Column("access_status", sa.String(length=40)),
            sa.Column("access_message", sa.String(length=2000)),
            sa.Column("access_checked_at", sa.DateTime(timezone=True)),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "scheduler_jobs" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "scheduler_jobs",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("target_id", sa.String(length=64), nullable=False),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("profile_id", sa.String(length=64)),
            sa.Column("submission_key", sa.String(length=255), nullable=False),
            sa.Column("task_ref", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=1000), nullable=False),
            sa.Column("approval_ref", sa.String(length=500)),
            sa.Column("request_json", sa.Text(), nullable=False),
            sa.Column("script_body", sa.Text()),
            sa.Column("retain_submission_body", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("scheduler_job_id", sa.String(length=128)),
            sa.Column("state", sa.String(length=40), nullable=False),
            sa.Column("raw_state", sa.String(length=80)),
            sa.Column("allocated_tres_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("node_list", sa.String(length=2000)),
            sa.Column("stdout_path", sa.String(length=2000)),
            sa.Column("stderr_path", sa.String(length=2000)),
            sa.Column("exit_code", sa.String(length=80)),
            sa.Column("error_message", sa.String(length=2000)),
            sa.Column("submitted_at", sa.DateTime(timezone=True)),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["target_id"], ["scheduler_targets.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
            sa.UniqueConstraint("actor_id", "submission_key", name="uq_scheduler_job_submission"),
        )
        op.create_index("ix_scheduler_job_target_state", "scheduler_jobs", ["target_id", "state"])
        op.create_index("ix_scheduler_job_project_created", "scheduler_jobs", ["project_id", "created_at"])
        op.create_index("ix_scheduler_jobs_scheduler_job_id", "scheduler_jobs", ["scheduler_job_id"])
        op.create_index("ix_scheduler_jobs_profile_id", "scheduler_jobs", ["profile_id"])
    if "scheduler_job_events" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "scheduler_job_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=40), nullable=False),
            sa.Column("raw_state", sa.String(length=80)),
            sa.Column("detail_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["job_id"], ["scheduler_jobs.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_scheduler_job_event_job_time", "scheduler_job_events", ["job_id", "created_at"])
    if "scheduler_transfers" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "scheduler_transfers",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("target_id", sa.String(length=64), nullable=False),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("submission_key", sa.String(length=255), nullable=False),
            sa.Column("approval_ref", sa.String(length=500), nullable=False),
            sa.Column("local_source_path", sa.String(length=4000), nullable=False),
            sa.Column("remote_directory", sa.String(length=2000), nullable=False),
            sa.Column("remote_staged_path", sa.String(length=2000)),
            sa.Column("source_size_bytes", sa.Integer()),
            sa.Column("state", sa.String(length=40), nullable=False),
            sa.Column("error_message", sa.String(length=2000)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.ForeignKeyConstraint(["target_id"], ["scheduler_targets.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
            sa.UniqueConstraint("actor_id", "submission_key", name="uq_scheduler_transfer_submission"),
        )
        op.create_index(
            "ix_scheduler_transfer_project_created",
            "scheduler_transfers",
            ["project_id", "created_at"],
        )
    if "resource_providers" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_providers",
            sa.Column("id", sa.String(length=260), primary_key=True),
            sa.Column("provider_type", sa.String(length=32), nullable=False),
            sa.Column("display_name", sa.String(length=160), nullable=False),
            sa.Column("endpoint_id", sa.String(length=128)),
            sa.Column("scheduler_target_id", sa.String(length=64)),
            sa.Column("native_ref_json", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scheduler_target_id"], ["scheduler_targets.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_resource_provider_type_enabled", "resource_providers", ["provider_type", "enabled"])
        op.create_index("ix_resource_providers_endpoint_id", "resource_providers", ["endpoint_id"])
        op.create_index(
            "ix_resource_providers_scheduler_target_id",
            "resource_providers",
            ["scheduler_target_id"],
        )
    if "allocatable_units" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "allocatable_units",
            sa.Column("id", sa.String(length=320), primary_key=True),
            sa.Column("provider_id", sa.String(length=260), nullable=False),
            sa.Column("unit_key", sa.String(length=260), nullable=False),
            sa.Column("unit_type", sa.String(length=32), nullable=False),
            sa.Column("endpoint_id", sa.String(length=128)),
            sa.Column("gpu_id", sa.String(length=160)),
            sa.Column("scheduler_target_id", sa.String(length=64)),
            sa.Column("total_gpu_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_cpu_cores", sa.Float()),
            sa.Column("total_memory_mib", sa.Integer()),
            sa.Column("total_vram_mib", sa.Integer()),
            sa.Column("labels_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("native_ref_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="available"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["provider_id"], ["resource_providers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["gpu_id"], ["gpu_devices.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["scheduler_target_id"], ["scheduler_targets.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("provider_id", "unit_key", name="uq_allocatable_unit_provider_key"),
        )
        op.create_index("ix_allocatable_units_provider_id", "allocatable_units", ["provider_id"])
        op.create_index("ix_allocatable_units_endpoint_id", "allocatable_units", ["endpoint_id"])
        op.create_index("ix_allocatable_units_gpu_id", "allocatable_units", ["gpu_id"])
        op.create_index("ix_allocatable_units_scheduler_target_id", "allocatable_units", ["scheduler_target_id"])
        op.create_index("ix_allocatable_unit_type_state", "allocatable_units", ["unit_type", "state"])
    if "resource_claims" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_claims",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("task_ref", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=1000), nullable=False),
            sa.Column("provider_type", sa.String(length=32)),
            sa.Column("requested_quantities_json", sa.Text(), nullable=False),
            sa.Column("forecast_json", sa.Text()),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="pending"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        )
        op.create_index("ix_resource_claims_actor_id", "resource_claims", ["actor_id"])
        op.create_index("ix_resource_claims_project_id", "resource_claims", ["project_id"])
        op.create_index("ix_resource_claim_project_state", "resource_claims", ["project_id", "state"])
        op.create_index("ix_resource_claim_actor_created", "resource_claims", ["actor_id", "created_at"])
    if "resource_plan_evaluations" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_plan_evaluations",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("claim_id", sa.String(length=64)),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("task_ref", sa.String(length=255), nullable=False),
            sa.Column("baseline_runtime_seconds", sa.Integer(), nullable=False),
            sa.Column("marginal_min_saved_seconds", sa.Integer(), nullable=False, server_default="120"),
            sa.Column("marginal_min_saved_ratio", sa.Float(), nullable=False, server_default="0.1"),
            sa.Column("selected_candidate_key", sa.String(length=120)),
            sa.Column("forecast_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        )
        op.create_index("ix_resource_plan_evaluations_claim_id", "resource_plan_evaluations", ["claim_id"])
        op.create_index("ix_resource_plan_evaluations_actor_id", "resource_plan_evaluations", ["actor_id"])
        op.create_index("ix_resource_plan_evaluations_project_id", "resource_plan_evaluations", ["project_id"])
        op.create_index("ix_resource_plan_eval_project_created", "resource_plan_evaluations", ["project_id", "created_at"])
        op.create_index("ix_resource_plan_eval_actor_created", "resource_plan_evaluations", ["actor_id", "created_at"])
    if "resource_plan_candidates" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_plan_candidates",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("evaluation_id", sa.String(length=64), nullable=False),
            sa.Column("candidate_key", sa.String(length=120), nullable=False),
            sa.Column("provider_type", sa.String(length=32)),
            sa.Column("quantities_json", sa.Text(), nullable=False),
            sa.Column("predicted_runtime_seconds", sa.Integer(), nullable=False),
            sa.Column("predicted_saved_seconds", sa.Integer(), nullable=False),
            sa.Column("predicted_saved_ratio", sa.Float(), nullable=False),
            sa.Column("satisfies_marginal_threshold", sa.Boolean(), nullable=False),
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("rejection_reason", sa.String(length=500)),
            sa.ForeignKeyConstraint(["evaluation_id"], ["resource_plan_evaluations.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("evaluation_id", "candidate_key", name="uq_resource_plan_candidate_eval_key"),
        )
        op.create_index("ix_resource_plan_candidate_selected", "resource_plan_candidates", ["evaluation_id", "selected"])
    if "resource_allocations" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_allocations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("claim_id", sa.String(length=64), nullable=False),
            sa.Column("unit_id", sa.String(length=320), nullable=False),
            sa.Column("native_lease_id", sa.String(length=64)),
            sa.Column("native_scheduler_job_id", sa.String(length=64)),
            sa.Column("quantities_json", sa.Text(), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["unit_id"], ["allocatable_units.id"], ondelete="RESTRICT"),
            sa.ForeignKeyConstraint(["native_lease_id"], ["leases.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["native_scheduler_job_id"], ["scheduler_jobs.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_resource_allocation_claim_state", "resource_allocations", ["claim_id", "state"])
        op.create_index("ix_resource_allocation_unit_state", "resource_allocations", ["unit_id", "state"])
    if "resource_run_actuals" not in inspect(op.get_bind()).get_table_names():
        op.create_table(
            "resource_run_actuals",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("evaluation_id", sa.String(length=64)),
            sa.Column("claim_id", sa.String(length=64)),
            sa.Column("actor_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("task_ref", sa.String(length=255), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("actual_duration_seconds", sa.Integer()),
            sa.Column("quantities_json", sa.Text(), nullable=False),
            sa.Column("outcome", sa.String(length=32), nullable=False),
            sa.Column("notes_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["evaluation_id"], ["resource_plan_evaluations.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["claim_id"], ["resource_claims.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        )
        op.create_index("ix_resource_run_actuals_evaluation_id", "resource_run_actuals", ["evaluation_id"])
        op.create_index("ix_resource_run_actuals_claim_id", "resource_run_actuals", ["claim_id"])
        op.create_index("ix_resource_run_actuals_actor_id", "resource_run_actuals", ["actor_id"])
        op.create_index("ix_resource_run_actuals_project_id", "resource_run_actuals", ["project_id"])
        op.create_index("ix_resource_run_actual_project_created", "resource_run_actuals", ["project_id", "created_at"])
        op.create_index("ix_resource_run_actual_task_ref", "resource_run_actuals", ["task_ref"])
    inspector = inspect(op.get_bind())
    if "allocation_requests" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("allocation_requests")}
        indexes = {index["name"] for index in inspector.get_indexes("allocation_requests")}
        if "profile_id" not in columns:
            with op.batch_alter_table("allocation_requests") as batch:
                batch.add_column(sa.Column("profile_id", sa.String(length=64), nullable=True))
                if "ix_allocation_requests_profile_id" not in indexes:
                    batch.create_index("ix_allocation_requests_profile_id", ["profile_id"])
