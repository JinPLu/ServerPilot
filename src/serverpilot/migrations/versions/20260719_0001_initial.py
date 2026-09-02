"""initial global ServerPilot schema

Revision ID: 20260719_0001
Revises:
Create Date: 2026-07-19
"""

import sqlalchemy as sa
from alembic import op

from serverpilot.models import Base

revision = "20260719_0001"
down_revision = None
branch_labels = None
depends_on = None


# Keep the initial revision independent from future model additions. New
# tables must arrive in a later revision so upgrades remain deterministic.
INITIAL_TABLES = frozenset(
    {
        "revisions",
        "endpoints",
        "endpoint_projects",
        "gpu_devices",
        "telemetry_snapshots",
        "process_observations",
        "projects",
        "actors",
        "actor_projects",
        "api_tokens",
        "leases",
        "lease_resources",
        "workload_bindings",
        "reservations",
        "maintenance_windows",
        "audit_events",
        "alerts",
        "idempotency_records",
        "provider_states",
    }
)


def _initial_tables():
    return [table for table in Base.metadata.sorted_tables if table.name in INITIAL_TABLES]


# The tables come from the live ORM, so a column the model later loses would
# silently vanish from this revision too, and every revision in between that
# reads it would fail. These two were in the schema this revision created;
# 20260903_0036 is where they go away.
_RETIRED_COLUMNS = (("endpoints", "enabled"), ("gpu_devices", "enabled"))


# The other direction of the same drift: a claim wrote its own request row when
# this revision was written, so these four lease columns did not exist yet.
# 20260903_0037 is where they move onto the lease.
_FUTURE_LEASE_COLUMNS = ("task_ref", "purpose", "constraints_json", "duration_seconds")


def _create_retired_request_tables() -> None:
    """Rebuild the request row a claim used to write, and the lease link to it.

    Same reason as ``_RETIRED_COLUMNS``: the ORM no longer describes them, but
    revisions 0004 through 0033 read them and this revision is where they were
    born. 20260903_0037 is where they go away.
    """

    with op.batch_alter_table("leases") as batch:
        for column in _FUTURE_LEASE_COLUMNS:
            batch.drop_column(column)
    op.create_table(
        "allocation_requests",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("task_ref", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=1000), nullable=False),
        sa.Column("constraints_json", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Integer(), nullable=False),
        sa.Column("expected_duration_seconds", sa.Integer()),
        sa.Column("start_after", sa.DateTime(timezone=True)),
        sa.Column("deadline", sa.DateTime(timezone=True)),
        sa.Column("approval_ref", sa.String(length=500)),
        sa.Column("state", sa.String(length=40), nullable=False),
        sa.Column("priority_class", sa.String(length=32), nullable=False),
        sa.Column("blocked_reason", sa.String(length=1000)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["actors.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
    )
    op.create_index("ix_request_queue", "allocation_requests", ["state", "created_at"])
    op.create_index("ix_allocation_requests_actor_id", "allocation_requests", ["actor_id"])
    op.create_index("ix_allocation_requests_project_id", "allocation_requests", ["project_id"])
    # SQLite cannot add a NOT NULL column or a table constraint after the fact,
    # so the link is a unique index on a nullable column.
    op.add_column("leases", sa.Column("request_id", sa.String(length=64)))
    op.create_index("uq_leases_request_id", "leases", ["request_id"], unique=True)


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), tables=_initial_tables(), checkfirst=False)
    for table, column in _RETIRED_COLUMNS:
        op.add_column(
            table,
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    _create_retired_request_tables()


def downgrade() -> None:
    op.drop_index("uq_leases_request_id", table_name="leases")
    op.drop_table("allocation_requests")
    Base.metadata.drop_all(bind=op.get_bind(), tables=_initial_tables(), checkfirst=False)
