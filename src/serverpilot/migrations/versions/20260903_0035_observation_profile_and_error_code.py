"""collapse observation profiles to one, and record a closed failure code

Revision ID: 20260903_0035
Revises: 20260831_0034
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260903_0035"
down_revision = "20260831_0034"
branch_labels = None
depends_on = None

# The three built-in profiles became one. Two of them differed only in a shell
# string that the surviving probe already decides for itself, and the third
# required a collector script installed on the remote host, which no endpoint
# used. A plugin id is never rewritten: it names a delegated cluster.
RETIRED_PROFILES = ("linux-nvidia", "linux-host", "server-script-v1")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    if "endpoints" in tables:
        bind.execute(
            sa.text(
                "UPDATE endpoints SET observation_profile = 'linux' "
                "WHERE observation_profile IN (:a, :b, :c)"
            ),
            {"a": RETIRED_PROFILES[0], "b": RETIRED_PROFILES[1], "c": RETIRED_PROFILES[2]},
        )

    if "provider_states" in tables:
        columns = {column["name"] for column in inspector.get_columns("provider_states")}
        if "last_error_code" not in columns:
            op.add_column(
                "provider_states",
                sa.Column("last_error_code", sa.String(length=32), nullable=True),
            )
            # Existing rows keep their free text and get no code. Guessing one
            # would name a cause the migration cannot know -- a plugin failure
            # would be backfilled as an SSH failure -- and every consumer
            # branches on the code. An endpoint with no code reads as "silent"
            # rather than "failing" for the one interval it takes the next
            # attempt to classify it properly, and both answers fail closed.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    if "provider_states" in tables:
        columns = {column["name"] for column in inspector.get_columns("provider_states")}
        if "last_error_code" in columns:
            op.drop_column("provider_states", "last_error_code")
    if "endpoints" in tables:
        bind.execute(
            sa.text(
                "UPDATE endpoints SET observation_profile = 'linux-nvidia' "
                "WHERE observation_profile = 'linux'"
            )
        )
