"""Persist provider allocations and explicit connection intent.

Revision ID: 20260908_0039
Revises: 20260903_0038
"""

import json

import sqlalchemy as sa
from alembic import op

revision = "20260908_0039"
down_revision = "20260903_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("endpoint_observation_facts", sa.Column("provider_allocations_json", sa.Text(), nullable=False, server_default="[]"))
    op.create_table(
        "resource_allocations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("lease_id", sa.String(64), sa.ForeignKey("leases.id", ondelete="CASCADE")),
        sa.Column("endpoint_id", sa.String(255), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(64), sa.ForeignKey("actors.id"), nullable=False),
        sa.Column("request_key", sa.String(255), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False),
        sa.Column("allocation_ref", sa.String(128)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("release_granularity", sa.String(24), nullable=False),
        sa.Column("locations_json", sa.Text(), nullable=False),
        sa.Column("entry_json", sa.Text()),
        sa.Column("requested_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("workload_observed_at", sa.DateTime(timezone=True)),
        sa.Column("idle_since", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(1500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_id", "request_key", "endpoint_id", name="uq_allocation_request"),
        sa.UniqueConstraint("endpoint_id", "provider_id", "allocation_ref", name="uq_provider_allocation"),
    )
    op.create_index("ix_resource_allocations_lease_id", "resource_allocations", ["lease_id"])
    op.create_index("ix_resource_allocations_endpoint_id", "resource_allocations", ["endpoint_id"])
    op.create_table(
        "endpoint_connections",
        sa.Column("endpoint_id", sa.String(255), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("desired", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("message", sa.String(500)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    connection = op.get_bind()
    allocations = sa.table("resource_allocations", *[
        sa.column(name) for name in (
            "id", "lease_id", "endpoint_id", "provider_id", "actor_id", "request_key",
            "request_json", "allocation_ref", "state", "release_granularity", "locations_json",
            "requested_duration_seconds", "expires_at", "created_at", "updated_at",
        )
    ])
    leases = connection.execute(sa.text("SELECT * FROM leases WHERE kind = 'workload'")).mappings().all()
    for lease in leases:
        constraints = json.loads(lease["constraints_json"])
        previous = constraints.pop("plugin_allocation", None)
        endpoints = connection.execute(sa.text(
            "SELECT DISTINCT g.endpoint_id FROM lease_resources r "
            "JOIN gpu_devices g ON g.id = r.gpu_id WHERE r.lease_id = :lease"
        ), {"lease": lease["id"]}).scalars().all()
        if not endpoints:
            endpoints = constraints.get("endpoint_ids", [])
        if isinstance(previous, dict) and not endpoints:
            raise RuntimeError(f"cannot migrate plugin lease {lease['id']}: endpoint identity is missing")
        for index, endpoint_id in enumerate(endpoints):
            plugin = isinstance(previous, dict)
            terminal = lease["state"] in ("RELEASED", "EXPIRED_EMPTY")
            connection.execute(allocations.insert().values(
                id=f"{lease['id']}-{index}", lease_id=lease["id"], endpoint_id=endpoint_id,
                provider_id=previous["plugin_id"] if plugin else "builtin",
                actor_id=lease["actor_id"], request_key=lease["id"],
                request_json=json.dumps({"project_id": lease["project_id"], "task_ref": lease["task_ref"],
                                         "constraints": constraints, "duration_seconds": lease["duration_seconds"]}),
                allocation_ref=previous["allocation_ref"] if plugin else lease["id"],
                state="released" if terminal else "unconfirmed",
                release_granularity="allocation" if plugin else "device", locations_json="[]",
                requested_duration_seconds=lease["duration_seconds"], expires_at=lease["expires_at"],
                created_at=lease["issued_at"], updated_at=lease["issued_at"],
            ))
        if previous is not None:
            connection.execute(sa.text("UPDATE leases SET constraints_json=:value WHERE id=:id"),
                               {"value": json.dumps(constraints), "id": lease["id"]})


def downgrade() -> None:
    # Preserve the remote release handle for code predating allocation records.
    connection = op.get_bind()
    rows = connection.execute(sa.text(
        "SELECT a.lease_id, a.provider_id, a.allocation_ref, l.constraints_json "
        "FROM resource_allocations a JOIN leases l ON l.id=a.lease_id "
        "WHERE a.provider_id != 'builtin' AND a.allocation_ref IS NOT NULL"
    )).mappings().all()
    for row in rows:
        constraints = json.loads(row["constraints_json"])
        constraints["plugin_allocation"] = {"plugin_id": row["provider_id"], "allocation_ref": row["allocation_ref"]}
        connection.execute(sa.text("UPDATE leases SET constraints_json=:value WHERE id=:id"),
                           {"value": json.dumps(constraints), "id": row["lease_id"]})
    with op.batch_alter_table("endpoint_observation_facts") as batch:
        batch.drop_column("provider_allocations_json")
    op.drop_table("endpoint_connections")
    op.drop_table("resource_allocations")
