"""Let each server set the VRAM and host memory its held cards fill, 85 percent by default."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_0045"
down_revision = "20261111_0044"
branch_labels = None
depends_on = None

_TARGETS = (
    ("keepalive_target_gpu_memory_pct", "ck_endpoint_keepalive_target_gpu_memory", 10),
    ("keepalive_target_memory_pct", "ck_endpoint_keepalive_target_memory", 0),
)


def upgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        for column, _check, _minimum in _TARGETS:
            batch.add_column(
                sa.Column(column, sa.Integer(), nullable=False, server_default="85")
            )
    # A check naming a column added in the same batch makes the batch's table
    # copy order circular, so the constraints go in a second batch.
    with op.batch_alter_table("endpoints") as batch:
        for column, check, minimum in _TARGETS:
            batch.create_check_constraint(check, f"{column} >= {minimum} AND {column} <= 100")


def downgrade() -> None:
    with op.batch_alter_table("endpoints") as batch:
        for column, check, _minimum in _TARGETS:
            batch.drop_constraint(check, type_="check")
            batch.drop_column(column)
