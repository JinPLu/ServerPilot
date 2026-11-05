"""Remember a keepalive start whose cleanup failed, so the worker it left is retried, not lost."""

import sqlalchemy as sa
from alembic import op

revision = "20261105_0043"
down_revision = "20261104_0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("keepalive_current") as batch:
        batch.add_column(
            sa.Column(
                "cleanup_pending",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("keepalive_current") as batch:
        batch.drop_column("cleanup_pending")
