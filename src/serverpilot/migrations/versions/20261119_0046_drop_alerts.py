"""Remove reminders: the alerts table had no reader left."""

import sqlalchemy as sa
from alembic import op

revision = "20261119_0046"
down_revision = "20261111_0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for index in ("ix_alert_dedupe", "ix_alert_state", "ix_alert_active"):
        op.drop_index(index, table_name="alerts")
    op.drop_table("alerts")


def downgrade() -> None:
    # The rows are gone; an older build starts again from an empty inbox.
    op.create_table(
        "alerts",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("alert_type", sa.String(length=80), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=False),
        sa.Column("resource_id", sa.String(length=260), nullable=False),
        sa.Column("message", sa.String(length=1000), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(length=128)),
        sa.Column("category", sa.String(length=32), nullable=False, server_default="resource_idle"),
        sa.Column("dedupe_key", sa.String(length=320)),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="resolved"),
        sa.Column("pending_since", sa.DateTime(timezone=True)),
        sa.Column("notified_at", sa.DateTime(timezone=True)),
        sa.Column("lease_id", sa.String(length=64)),
        sa.Column("project_id", sa.String(length=64)),
        sa.Column("task_ref", sa.String(length=255)),
        sa.Column("payload_json", sa.Text()),
    )
    op.create_index("ix_alert_active", "alerts", ["active", "severity", "last_seen_at"])
    op.create_index("ix_alert_state", "alerts", ["state", "category", "last_seen_at"])
    op.create_index("ix_alert_dedupe", "alerts", ["dedupe_key"])
