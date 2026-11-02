"""An update must distinguish running code from newer files on disk."""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from serverpilot.runtime_identity import package_build_id


def _package(root: Path) -> None:
    root.mkdir()
    (root / "__init__.py").write_text('__version__ = "1.0.0"\n')
    (root / "service.py").write_text("def result():\n    return 1\n")
    (root / "bundled_plugins").mkdir()
    (root / "bundled_plugins" / "provider").write_text("#!/bin/sh\nexit 0\n")
    (root / "migrations").mkdir()
    (root / "migrations" / "script.py.mako").write_text("${message}\n")


def test_build_id_is_independent_of_install_location(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _package(source)
    installed = tmp_path / "installed"
    shutil.copytree(source, installed)

    assert package_build_id(source) == package_build_id(installed)
    assert len(package_build_id(source)) == 64


def test_runtime_changes_are_detected_without_a_version_bump(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _package(source)
    original = package_build_id(source)

    (source / "service.py").write_text("def result():\n    return 2\n")
    patched = package_build_id(source)
    assert patched != original
    (source / "bundled_plugins" / "provider").write_text("#!/bin/sh\nexit 1\n")
    assert package_build_id(source) != patched


def test_python_caches_hidden_files_and_package_metadata_do_not_change_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _package(source)
    original = package_build_id(source)
    for folder in ["__pycache__", ".cache", "serverpilot.egg-info", "serverpilot.dist-info"]:
        (source / folder).mkdir()
        (source / folder / "generated").write_bytes(b"generated data")
    for filename in ["service.pyc", "service.pyo", ".DS_Store"]:
        (source / filename).write_bytes(b"generated data")

    assert package_build_id(source) == original


def test_health_reports_the_build_loaded_at_app_creation(build_app, monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "package"
    _package(source)
    original = package_build_id(source)
    monkeypatch.setattr("serverpilot.api.package_build_id", lambda: package_build_id(source))
    app = build_app("identity")
    (source / "service.py").write_text("def result():\n    return 2\n")
    assert package_build_id(source) != original
    client = TestClient(app)

    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == ready.status_code == 200
    assert live.json()["build_id"] == ready.json()["build_id"] == original
    new_app = build_app("updated-identity")
    assert TestClient(new_app).get("/health/live").json()["build_id"] == package_build_id(source)
