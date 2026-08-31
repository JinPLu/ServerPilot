"""add process_observations.absent_since

Revision ID: 20260831_0034
Revises: 20260828_0033
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260831_0034"
down_revision = "20260828_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "process_observations" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("process_observations")}
    if "absent_since" in columns:
        return
    op.add_column(
        "process_observations",
        sa.Column("absent_since", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "process_observations" not in set(inspector.get_table_names()):
        return
    columns = {column["name"] for column in inspector.get_columns("process_observations")}
    if "absent_since" not in columns:
        return
    op.drop_column("process_observations", "absent_since")
