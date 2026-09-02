"""move the derived endpoint facts out of runtime_settings

Revision ID: 20260903_0038
Revises: 20260903_0037
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260903_0038"
down_revision = "20260903_0037"
branch_labels = None
depends_on = None

# `runtime_settings` was three stores in one table, told apart by a key prefix:
# a real user knob, a `pc:` plugin-capacity cache and a `civ:` collector-version
# cache. Both caches are facts about one endpoint, so the endpoint id had to be
# hashed into the key to fit a 64-character column, which is why they could not
# carry a foreign key and why nothing cleaned them up when the endpoint went.
# Rows carry that hash and not the id they came from, so they cannot be moved --
# the next observation of each endpoint writes the fact back under its real id.
_PREFIXES = ("pc:", "civ:")


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "endpoint_observation_facts" not in tables:
        op.create_table(
            "endpoint_observation_facts",
            sa.Column("endpoint_id", sa.String(length=128), primary_key=True),
            sa.Column("plugin_capacity_json", sa.Text()),
            sa.Column("collector_version", sa.String(length=64)),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        )
    if "runtime_settings" in tables:
        for prefix in _PREFIXES:
            op.execute(
                sa.text("DELETE FROM runtime_settings WHERE key LIKE :pattern").bindparams(
                    pattern=f"{prefix}%"
                )
            )


def downgrade() -> None:
    # The caches are rebuilt by observation either way, so going back only has
    # to remove the table; there is nothing to put back under the old prefixes.
    if "endpoint_observation_facts" in set(inspect(op.get_bind()).get_table_names()):
        op.drop_table("endpoint_observation_facts")
