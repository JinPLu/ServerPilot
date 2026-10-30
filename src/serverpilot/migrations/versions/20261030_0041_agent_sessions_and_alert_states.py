"""Give agents an identity, leases an epoch, and alerts a lifecycle."""
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "20261030_0041"
down_revision = "20260908_0040"
branch_labels = None
depends_on = None

CATEGORY_BY_ALERT_TYPE = (
    ("idle_lease", "resource_idle"),
    ("collector_unreachable", "server_abnormal"),
    ("orphaned_busy", "resource_idle"),
    ("unknown_process_gpu", "resource_idle"),
)


def upgrade() -> None:
    op.create_table("agent_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("client", sa.String(120), nullable=False),
        sa.Column("client_version", sa.String(64)),
        sa.Column("actor_id", sa.String(128), sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_session_project", "agent_sessions", ["project_id", "last_seen_at"])
    with op.batch_alter_table("leases") as batch:
        batch.add_column(sa.Column("owner_session_id", sa.String(64)))
        batch.add_column(sa.Column("resource_epoch", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("agent_seen_audit_id", sa.Integer()))
    op.create_index(
        "uq_active_owned_task", "leases", ["project_id", "task_ref"], unique=True,
        sqlite_where=sa.text("owner_session_id IS NOT NULL AND released_at IS NULL"),
    )
    with op.batch_alter_table("lease_resources") as batch:
        batch.add_column(sa.Column("added_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("release_reason", sa.String(500)))
        batch.add_column(sa.Column("released_by", sa.String(128)))
        batch.add_column(sa.Column("keep_idle_windows", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_audit_resource", "audit_events", ["resource_type", "resource_id", "id"])
    with op.batch_alter_table("alerts") as batch:
        batch.add_column(sa.Column("category", sa.String(32), nullable=False, server_default="resource_idle"))
        batch.add_column(sa.Column("dedupe_key", sa.String(320)))
        batch.add_column(sa.Column("state", sa.String(16), nullable=False, server_default="resolved"))
        batch.add_column(sa.Column("pending_since", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("notified_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("lease_id", sa.String(64)))
        batch.add_column(sa.Column("project_id", sa.String(64)))
        batch.add_column(sa.Column("task_ref", sa.String(255)))
        batch.add_column(sa.Column("payload_json", sa.Text()))
    op.create_index("ix_alert_state", "alerts", ["state", "category", "last_seen_at"])
    op.create_index("ix_alert_dedupe", "alerts", ["dedupe_key"])
    connection = op.get_bind()
    for alert_type, category in CATEGORY_BY_ALERT_TYPE:
        connection.execute(sa.text("UPDATE alerts SET category=:category WHERE alert_type=:alert_type"),
                           {"category": category, "alert_type": alert_type})
    connection.execute(sa.text("UPDATE alerts SET state='active' WHERE active=1"))
    connection.execute(sa.text("UPDATE alerts SET state='acked' WHERE active=1 AND acknowledged_at IS NOT NULL"))
    # Nobody can heartbeat an old lease until an agent reconnects and claims
    # it, so the upgrade itself must not count as the holder going quiet.
    now = datetime.now(UTC)
    connection.execute(
        sa.text("UPDATE leases SET last_heartbeat_at=:now, idle_since=NULL "
                "WHERE released_at IS NULL AND kind='workload'"),
        {"now": now.strftime("%Y-%m-%d %H:%M:%S.%f")},
    )
    connection.execute(sa.text(
        "UPDATE lease_resources SET idle_since=NULL, added_at=(SELECT issued_at FROM leases "
        "WHERE leases.id=lease_resources.lease_id) WHERE active=1"
    ))
    connection.execute(sa.text(
        "UPDATE resource_allocations SET idle_since=NULL WHERE lease_id IN "
        "(SELECT id FROM leases WHERE released_at IS NULL)"
    ))


def downgrade() -> None:
    op.drop_index("ix_alert_dedupe", table_name="alerts")
    op.drop_index("ix_alert_state", table_name="alerts")
    with op.batch_alter_table("alerts") as batch:
        for column in ("payload_json", "task_ref", "project_id", "lease_id", "notified_at",
                       "pending_since", "state", "dedupe_key", "category"):
            batch.drop_column(column)
    op.drop_index("ix_audit_resource", table_name="audit_events")
    with op.batch_alter_table("lease_resources") as batch:
        for column in ("keep_idle_windows", "released_by", "release_reason", "added_at"):
            batch.drop_column(column)
    op.drop_index("uq_active_owned_task", table_name="leases")
    with op.batch_alter_table("leases") as batch:
        for column in ("agent_seen_audit_id", "resource_epoch", "owner_session_id"):
            batch.drop_column(column)
    op.drop_index("ix_agent_session_project", table_name="agent_sessions")
    op.drop_table("agent_sessions")
