"""Scope physical observations to nodes without changing existing GPU IDs."""
import sqlalchemy as sa
from alembic import op

revision = "20260908_0040"
down_revision = "20260908_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("resource_allocations") as batch:
        batch.add_column(sa.Column("name", sa.String(255)))
        batch.add_column(sa.Column("owner", sa.String(120)))
        batch.add_column(sa.Column("accounting_json", sa.Text()))
    with op.batch_alter_table("endpoint_telemetry_current", naming_convention={"pk": "pk_%(table_name)s"}) as batch:
        batch.add_column(sa.Column("node_id", sa.String(253), nullable=False, server_default="self"))
        batch.add_column(sa.Column("boot_id", sa.String(120)))
        batch.drop_constraint("pk_endpoint_telemetry_current", type_="primary")
        batch.create_primary_key("pk_endpoint_telemetry_current", ["endpoint_id", "node_id"])
    with op.batch_alter_table("endpoint_telemetry_snapshots") as batch:
        batch.add_column(sa.Column("node_id", sa.String(253), nullable=False, server_default="self"))
        batch.add_column(sa.Column("boot_id", sa.String(120)))
    with op.batch_alter_table("gpu_devices") as batch:
        batch.add_column(sa.Column("node_id", sa.String(253), nullable=False, server_default="self"))
        batch.drop_constraint("uq_endpoint_gpu_uuid", type_="unique")
        batch.create_unique_constraint("uq_endpoint_node_gpu_uuid", ["endpoint_id", "node_id", "gpu_uuid"])
    op.create_table("node_observation_states",
        sa.Column("endpoint_id", sa.String(255), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("node_id", sa.String(253), primary_key=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("message", sa.String(500)),
    )
    op.execute("INSERT INTO node_observation_states (endpoint_id,node_id,last_success_at,last_attempt_at) "
               "SELECT endpoint_id,node_id,observed_at,collected_at FROM endpoint_telemetry_current")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM gpu_devices WHERE node_id != 'self' LIMIT 1")).first():
        raise RuntimeError("cannot downgrade while multi-node GPU identities exist")
    with op.batch_alter_table("resource_allocations") as batch:
        batch.drop_column("name")
        batch.drop_column("owner")
        batch.drop_column("accounting_json")
    op.drop_table("node_observation_states")
    with op.batch_alter_table("gpu_devices") as batch:
        batch.drop_constraint("uq_endpoint_node_gpu_uuid", type_="unique")
        batch.drop_column("node_id")
        batch.create_unique_constraint("uq_endpoint_gpu_uuid", ["endpoint_id", "gpu_uuid"])
    op.execute("DELETE FROM endpoint_telemetry_current WHERE node_id != 'self'")
    op.execute("DELETE FROM endpoint_telemetry_snapshots WHERE node_id != 'self'")
    with op.batch_alter_table("endpoint_telemetry_current") as batch:
        batch.drop_constraint("pk_endpoint_telemetry_current", type_="primary")
        batch.create_primary_key("pk_endpoint_telemetry_current", ["endpoint_id"])
        batch.drop_column("node_id")
        batch.drop_column("boot_id")
    with op.batch_alter_table("endpoint_telemetry_snapshots") as batch:
        batch.drop_column("node_id")
        batch.drop_column("boot_id")
