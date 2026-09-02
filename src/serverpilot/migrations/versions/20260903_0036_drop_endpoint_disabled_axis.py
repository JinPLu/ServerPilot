"""drop the endpoint disabled/draining axis

Revision ID: 20260903_0036
Revises: 20260903_0035
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260903_0036"
down_revision = "20260903_0035"
branch_labels = None
depends_on = None

# Nothing ever wrote these after row creation: the three methods that set them
# were removed once it was clear no route, CLI verb or MCP tool called them.
# A column only a migration can change is not state, so the states derived from
# it -- DISABLED and DRAINING -- were unreachable too.
_DROPPED = {
    "endpoints": ("lifecycle_state", "enabled"),
    "gpu_devices": ("enabled",),
}


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, names in _DROPPED.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        present = [name for name in names if name in columns]
        if not present:
            continue
        # SQLite rebuilds the table to drop a column.
        with op.batch_alter_table(table) as batch:
            for name in present:
                batch.drop_column(name)


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table, names in _DROPPED.items():
        if table not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        with op.batch_alter_table(table) as batch:
            if "lifecycle_state" in names and "lifecycle_state" not in columns:
                batch.add_column(
                    sa.Column(
                        "lifecycle_state",
                        sa.String(length=16),
                        nullable=False,
                        server_default="active",
                    )
                )
            if "enabled" in names and "enabled" not in columns:
                batch.add_column(
                    sa.Column(
                        "enabled",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.true(),
                    )
                )
