"""fold the allocation request into the lease it produced

Revision ID: 20260903_0037
Revises: 20260903_0036
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260903_0037"
down_revision = "20260903_0036"
branch_labels = None
depends_on = None

# Every request row this install ever wrote went straight to a lease; not one
# was ever observed in QUEUED. The queue the table existed for was never a
# queue, so the four fields a lease actually needs move onto the lease and the
# table goes. The `request.created` audit events stay: they are history.
_MOVED = ("task_ref", "purpose", "constraints_json", "duration_seconds")


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "leases" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("leases")}
    if not set(_MOVED) <= columns:
        with op.batch_alter_table("leases") as batch:
            if "task_ref" not in columns:
                batch.add_column(sa.Column("task_ref", sa.String(length=255)))
            if "purpose" not in columns:
                batch.add_column(sa.Column("purpose", sa.String(length=1000)))
            if "constraints_json" not in columns:
                batch.add_column(sa.Column("constraints_json", sa.Text()))
            if "duration_seconds" not in columns:
                batch.add_column(sa.Column("duration_seconds", sa.Integer()))
    if "allocation_requests" in tables and "request_id" in columns:
        op.execute(
            sa.text(
                "UPDATE leases SET "
                "task_ref = (SELECT r.task_ref FROM allocation_requests r WHERE r.id = leases.request_id), "
                "purpose = (SELECT r.purpose FROM allocation_requests r WHERE r.id = leases.request_id), "
                "constraints_json = (SELECT r.constraints_json FROM allocation_requests r WHERE r.id = leases.request_id), "
                "duration_seconds = (SELECT r.duration_seconds FROM allocation_requests r WHERE r.id = leases.request_id) "
                "WHERE task_ref IS NULL"
            )
        )
    # A lease whose request row is already gone still has to satisfy NOT NULL.
    op.execute(
        sa.text(
            "UPDATE leases SET task_ref = COALESCE(task_ref, id), "
            "purpose = COALESCE(purpose, ''), "
            "constraints_json = COALESCE(constraints_json, '{}'), "
            "duration_seconds = COALESCE(duration_seconds, 0)"
        )
    )
    indexes = {index["name"] for index in inspect(op.get_bind()).get_indexes("leases")}
    if "uq_leases_request_id" in indexes:
        op.drop_index("uq_leases_request_id", table_name="leases")
    with op.batch_alter_table("leases") as batch:
        batch.alter_column("task_ref", existing_type=sa.String(length=255), nullable=False)
        batch.alter_column("purpose", existing_type=sa.String(length=1000), nullable=False)
        batch.alter_column("constraints_json", existing_type=sa.Text(), nullable=False)
        batch.alter_column("duration_seconds", existing_type=sa.Integer(), nullable=False)
        if "request_id" in columns:
            batch.drop_column("request_id")
    if "allocation_requests" in inspect(op.get_bind()).get_table_names():
        op.drop_table("allocation_requests")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "allocation_requests" not in tables:
        op.create_table(
            "allocation_requests",
            sa.Column("id", sa.String(length=64), primary_key=True),
            sa.Column("actor_id", sa.String(length=64), nullable=False),
            sa.Column("project_id", sa.String(length=64), nullable=False),
            sa.Column("auto_activate", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("task_ref", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=1000), nullable=False),
            sa.Column("constraints_json", sa.Text(), nullable=False),
            sa.Column("duration_seconds", sa.Integer(), nullable=False),
            sa.Column("expected_duration_seconds", sa.Integer()),
            sa.Column("start_after", sa.DateTime(timezone=True)),
            sa.Column("deadline", sa.DateTime(timezone=True)),
            sa.Column("approval_ref", sa.String(length=500)),
            sa.Column("state", sa.String(length=40), nullable=False, server_default="QUEUED"),
            sa.Column("priority_class", sa.String(length=32), nullable=False, server_default="normal"),
            sa.Column("blocked_reason", sa.String(length=1000)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
            sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        )
        op.create_index("ix_request_queue", "allocation_requests", ["state", "created_at"])
        op.create_index("ix_allocation_requests_actor_id", "allocation_requests", ["actor_id"])
        op.create_index("ix_allocation_requests_project_id", "allocation_requests", ["project_id"])
    if "leases" not in tables:
        return
    columns = {column["name"] for column in inspector.get_columns("leases")}
    if "request_id" not in columns:
        with op.batch_alter_table("leases") as batch:
            batch.add_column(sa.Column("request_id", sa.String(length=64)))
    op.execute(
        sa.text(
            "INSERT INTO allocation_requests ("
            "id, actor_id, project_id, auto_activate, task_ref, purpose, constraints_json, "
            "duration_seconds, state, priority_class, created_at, updated_at) "
            "SELECT id, actor_id, project_id, 0, task_ref, purpose, constraints_json, "
            "duration_seconds, 'LEASED', 'normal', issued_at, issued_at FROM leases"
        )
    )
    op.execute(sa.text("UPDATE leases SET request_id = id"))
    with op.batch_alter_table("leases") as batch:
        batch.alter_column("request_id", existing_type=sa.String(length=64), nullable=False)
        for name in _MOVED:
            batch.drop_column(name)
    op.create_index("uq_leases_request_id", "leases", ["request_id"], unique=True)
