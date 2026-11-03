"""Record when a card was last seen idle, so a gap in looking can be told from idleness."""

import sqlalchemy as sa
from alembic import op

revision = "20261104_0042"
down_revision = "20261030_0041"
branch_labels = None
depends_on = None

TABLES = ("lease_resources", "resource_allocations")


def upgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("idle_checked_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("idle_checked_at")
