"""first-class server groups and optional endpoint membership

Revision ID: 20260827_0032
Revises: 20260822_0031
Create Date: 2026-08-27
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260827_0032"
down_revision = "20260822_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if "server_groups" not in inspector.get_table_names():
        op.create_table(
            "server_groups",
            sa.Column("id", sa.String(length=128), primary_key=True),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("workspace_path", sa.String(length=2000), nullable=False),
            sa.Column("environment_notes", sa.Text(), nullable=True),
            sa.Column("description", sa.String(length=1000), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "endpoints" not in inspect(op.get_bind()).get_table_names():
        return
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("endpoints")}
    if "server_group_id" in columns:
        return
    with op.batch_alter_table("endpoints") as batch:
        batch.add_column(sa.Column("server_group_id", sa.String(length=128), nullable=True))
        batch.create_index("ix_endpoints_server_group_id", ["server_group_id"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if "endpoints" in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns("endpoints")}
        indexes = {index["name"] for index in inspector.get_indexes("endpoints")}
        if "server_group_id" in columns:
            with op.batch_alter_table("endpoints") as batch:
                if "ix_endpoints_server_group_id" in indexes:
                    batch.drop_index("ix_endpoints_server_group_id")
                batch.drop_column("server_group_id")
    if "server_groups" in inspect(op.get_bind()).get_table_names():
        op.drop_table("server_groups")
