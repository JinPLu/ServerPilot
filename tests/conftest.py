from __future__ import annotations

import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI

from serverpilot.api import create_app
from serverpilot.config import EndpointConfig, InventoryConfig, ProjectConfig, Settings
from serverpilot.database import Database
from serverpilot.models import Actor
from serverpilot.service import ActorContext, BrokerService

# The keepalive helper runs on the Linux GPU servers and imports fcntl at module
# level. Where fcntl does not exist the helper cannot run at all, so collecting
# its tests only breaks the run. The control plane guards its own fcntl import.
collect_ignore = (
    [] if importlib.util.find_spec("fcntl") is not None else ["test_keepalive_adapter.py"]
)


@pytest.fixture
def inventory() -> InventoryConfig:
    return InventoryConfig(
        schema_version=1,
        projects=[
            ProjectConfig(id="project-a", display_name="Project A", weight=1),
            ProjectConfig(id="project-b", display_name="Project B", weight=1),
        ],
        endpoints=[
            EndpointConfig(
                id="endpoint-a",
                host="127.0.0.1",
                port=2201,
                ssh_user="gpu",
                workspace_path="/srv/project-a",
                labels=["direct-ssh", "test"],
                storage_group="test-storage",
                project_ids=["project-a", "project-b"],
            ),
            EndpointConfig(
                id="endpoint-b",
                host="127.0.0.1",
                port=2202,
                ssh_user="gpu",
                workspace_path="/srv/project-b",
                labels=["direct-ssh", "test"],
                storage_group="test-storage",
                project_ids=["project-a", "project-b"],
            ),
        ],
    )


@pytest.fixture
def build_app(
    tmp_path: Path, inventory: InventoryConfig
) -> Callable[..., FastAPI]:
    """Build an app on its own inventory file and database.

    Writing the inventory, naming a database and repeating the session secret
    took eight lines in front of nearly every API test, which buried what the
    test was actually about. Only ``name``, the ``Settings`` extras and the
    injected collector ever differed.
    """

    def build(
        name: str = "app",
        *,
        inventory_config: InventoryConfig | None = None,
        **overrides: Any,
    ) -> FastAPI:
        config = inventory if inventory_config is None else inventory_config
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(config.model_dump(mode="json")), encoding="utf-8")
        app_kwargs = {
            key: overrides.pop(key)
            for key in ("collector", "keepalive_adapter_resolver")
            if key in overrides
        }
        return create_app(
            Settings(
                database_url=f"sqlite:///{tmp_path / f'{name}.sqlite3'}",
                inventory_path=path,
                **overrides,
            ),
            **app_kwargs,
        )

    return build


@pytest.fixture
def service(tmp_path: Path, inventory: InventoryConfig) -> BrokerService:
    project_root = Path(__file__).resolve().parents[1]
    broker = BrokerService(Database(f"sqlite:///{tmp_path / 'broker.sqlite3'}", project_root), inventory)
    broker.initialize()
    return broker


@pytest.fixture
def admin(service: BrokerService) -> ActorContext:
    service.local_actor("test-admin")
    with service.database.session() as session:
        actor = session.get(Actor, "test-admin")
        assert actor is not None
        actor.role = "admin"
        session.commit()
    return ActorContext(
        id="test-admin",
        role="admin",
        project_ids=frozenset({"project-a", "project-b"}),
    )
