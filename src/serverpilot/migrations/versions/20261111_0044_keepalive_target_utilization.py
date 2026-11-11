"""Let each server set how hard its held cards work, defaulting to 85 percent."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0044"
down_revision = "20261105_0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        batch.add_column(
            sa.Column(
                "keepalive_target_utilization_pct",
                sa.Integer(),
                nullable=False,
                server_default="85",
            )
        )
    # A check naming a column added in the same batch makes the batch's table
    # copy order circular, so the constraint goes in a second batch.
    with op.batch_alter_table("endpoints") as batch:
        batch.create_check_constraint(
            "ck_endpoint_keepalive_target_utilization",
            "keepalive_target_utilization_pct >= 10 AND keepalive_target_utilization_pct <= 100",
        )


def downgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        batch.drop_constraint("ck_endpoint_keepalive_target_utilization", type_="check")
        batch.drop_column("keepalive_target_utilization_pct")
