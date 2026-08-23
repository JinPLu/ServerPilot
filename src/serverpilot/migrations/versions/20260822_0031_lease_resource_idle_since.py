"""record when each leased GPU was first observed idle

Revision ID: 20260822_0031
Revises: 20260821_0030
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260822_0031"
down_revision = "20260821_0030"
branch_labels = None
depends_on = None

_TABLE = "lease_resources"
_COLUMN = "idle_since"


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        return
    # Per-GPU idleness so a claim that uses one card does not keep the rest.
    # NULL means "no observed idle streak", which the reconcile loop only
    # replaces from a fresh observation showing no process on that GPU.
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        return
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
