"""Let each server set the CPU its held cards keep busy, none by default."""

import sqlalchemy as sa
from alembic import op

revision = "20261121_0047"
down_revision = "20261119_0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        batch.add_column(
            sa.Column("keepalive_target_cpu_pct", sa.Integer(), nullable=False, server_default="0")
        )
    # A check naming a column added in the same batch makes the batch's table
    # copy order circular, so the constraint goes in a second batch.
    with op.batch_alter_table("endpoints") as batch:
        batch.create_check_constraint(
            "ck_endpoint_keepalive_target_cpu",
            "keepalive_target_cpu_pct >= 0 AND keepalive_target_cpu_pct <= 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        batch.drop_constraint("ck_endpoint_keepalive_target_cpu", type_="check")
        batch.drop_column("keepalive_target_cpu_pct")
