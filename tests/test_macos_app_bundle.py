"""App updates overwrite one canonical bundle and reject unsafe cleanup."""

from __future__ import annotations

import importlib.util
import plistlib
import shutil
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "serverpilot_macos_app_bundle", Path(__file__).resolve().parents[1] / "scripts/macos_app_bundle.py"
)
assert _spec is not None and _spec.loader is not None
bundle = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bundle)


def _checkout(root: Path) -> None:
    files = {
        "src/serverpilot/__init__.py": b'__version__ = "4.1.2"\n',
        "src/serverpilot/service.py": b"result = 1\n",
        "desktop/App.swift": b"struct App {}\n",
        "desktop/ServerPilotCore/Sources/Core/Models.swift": b"struct Models {}\n",
        "desktop/Info.plist": plistlib.dumps({"CFBundleIdentifier": bundle.APP_BUNDLE_ID}),
        "desktop/assets/ServerPilot.icns": b"icon",
        "desktop/Fixtures/dashboard.json": b"{}\n",
        "configs/inventory.yaml": b"schema_version: 1\n",
    }
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)


def _app(path: Path, *, bundle_id: str = bundle.APP_BUNDLE_ID, identity=None) -> Path:
    path.mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id}
    if identity is not None:
        info.update({
            "CFBundleShortVersionString": identity["version"],
            "ServerPilotBuildID": identity["app_build_id"],
            "ServerPilotBackendBuildID": identity["backend_build_id"],
        })
    (path / "Contents").mkdir()
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    return path


def test_identity_changes_with_runtime_and_desktop_inputs_but_not_location(tmp_path: Path) -> None:
    root = tmp_path / "source"
    _checkout(root)
    copied = tmp_path / "copied"
    shutil.copytree(root, copied)
    initial = bundle.build_identity(root)

    assert initial == bundle.build_identity(copied)
    assert initial["version"] == "4.1.2"
    (root / "desktop/App.swift").write_text("struct NewApp {}\n")
    changed_app = bundle.build_identity(root)
    assert changed_app["app_build_id"] != initial["app_build_id"]
    assert changed_app["backend_build_id"] == initial["backend_build_id"]
    (root / "src/serverpilot/service.py").write_text("result = 2\n")
    changed_backend = bundle.build_identity(root)
    assert changed_backend["app_build_id"] != changed_app["app_build_id"]
    assert changed_backend["backend_build_id"] != initial["backend_build_id"]


@pytest.mark.parametrize("relative", [
    "desktop/ServerPilotCore/Sources/Core/Models.swift",
    "desktop/Info.plist",
    "desktop/assets/ServerPilot.icns",
    "desktop/Fixtures/dashboard.json",
    "configs/inventory.yaml",
])
def test_packaged_inputs_all_participate_in_app_identity(tmp_path: Path, relative: str) -> None:
    _checkout(tmp_path)
    original = bundle.build_identity(tmp_path)
    path = tmp_path / relative
    path.write_bytes(path.read_bytes() + b"changed")

    assert bundle.build_identity(tmp_path)["app_build_id"] != original["app_build_id"]


def test_duplicate_discovery_includes_nested_and_external_apps_without_following_links(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    _app(root / "ServerPilot.app")
    duplicates = [
        _app(root / "ServerPilot 2.app"),
        _app(root / "build/nested/ServerPilot-old.app"),
        _app(home / "Applications/ServerPilot.app"),
        _app(system_apps / "ServerPilot.app"),
    ]
    # App internals and a linked directory are not independent installations.
    _app(root / "ServerPilot.app/Contents/ServerPilot-helper.app")
    outside = _app(tmp_path / "outside/ServerPilot-outside.app")
    (root / "linked-directory").symlink_to(outside.parent, target_is_directory=True)
    alias = root / "dist/ServerPilot.app"
    alias.parent.mkdir()
    alias.symlink_to(root / "ServerPilot.app", target_is_directory=True)
    broken_alias = root / "ServerPilot-broken.app"
    broken_alias.symlink_to(root / "missing.app")

    assert bundle.duplicate_apps(root, home, system_apps) == sorted([*duplicates, alias, broken_alias])


def test_cleanup_preflights_all_bundles_before_removing_any(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    canonical = _app(root / "ServerPilot.app")
    owned = _app(root / "ServerPilot 2.app")
    unrelated = _app(root / "nested/ServerPilot-unrelated.app", bundle_id="other.vendor.app")

    with pytest.raises(bundle.BundleError, match="unrelated app"):
        bundle.clean_duplicate_apps(root, home, system_apps)

    assert canonical.exists() and owned.exists() and unrelated.exists()


def test_discovery_does_not_follow_a_linked_applications_directory(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    canonical = _app(root / "ServerPilot.app")
    home.mkdir()
    (home / "Applications").symlink_to(root, target_is_directory=True)

    assert bundle.clean_duplicate_apps(root, home, system_apps) == []
    assert canonical.exists()


def test_cleanup_refuses_malformed_identity_before_removing_other_apps(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    owned = _app(root / "ServerPilot 2.app")
    malformed = _app(root / "nested/ServerPilot-invalid.app")
    (malformed / "Contents/Info.plist").write_text('<?xml version="1.0"?><plist><dict>')

    with pytest.raises(bundle.BundleError, match="Cannot read app identity"):
        bundle.clean_duplicate_apps(root, home, system_apps)

    assert owned.exists() and malformed.exists()


def test_cleanup_removes_owned_copies_and_only_unlinks_aliases(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    canonical = _app(root / "ServerPilot.app")
    old = _app(root / "build/ServerPilot 2.app", bundle_id="local.gpu-broker.desktop")
    external_target = _app(tmp_path / "unrelated.app", bundle_id="other.vendor.app")
    alias = root / "ServerPilot-alias.app"
    alias.symlink_to(external_target, target_is_directory=True)

    assert bundle.clean_duplicate_apps(root, home, system_apps) == sorted([old, alias])
    assert canonical.exists() and external_target.exists()
    assert not old.exists() and not alias.is_symlink()


def test_verify_requires_current_stamps_and_no_duplicate_apps(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    _checkout(root)
    identity = bundle.build_identity(root)
    _app(root / "ServerPilot.app", identity=identity)

    assert bundle.verify_bundle(root, home, system_apps) == {
        "app_path": str(root / "ServerPilot.app"), **identity,
    }
    duplicate = _app(root / "nested/ServerPilot 2.app")
    with pytest.raises(bundle.BundleError, match="Duplicate"):
        bundle.verify_bundle(root, home, system_apps)
    shutil.rmtree(duplicate)
    (root / "desktop/App.swift").write_text("struct PatchedApp {}\n")
    with pytest.raises(bundle.BundleError, match="ServerPilotBuildID"):
        bundle.verify_bundle(root, home, system_apps)


@pytest.mark.parametrize("field", ["CFBundleIdentifier", "CFBundleShortVersionString", "ServerPilotBackendBuildID"])
def test_verify_rejects_incorrect_bundle_metadata(tmp_path: Path, field: str) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    _checkout(root)
    app = _app(root / "ServerPilot.app", identity=bundle.build_identity(root))
    path = app / "Contents/Info.plist"
    info = plistlib.loads(path.read_bytes())
    info[field] = "wrong"
    path.write_bytes(plistlib.dumps(info))

    with pytest.raises(bundle.BundleError, match=field):
        bundle.verify_bundle(root, home, system_apps)


def test_verify_rejects_a_canonical_alias(tmp_path: Path) -> None:
    root, home, system_apps = tmp_path / "repo", tmp_path / "home", tmp_path / "Applications"
    _checkout(root)
    target = _app(tmp_path / "target.app", identity=bundle.build_identity(root))
    (root / "ServerPilot.app").symlink_to(target, target_is_directory=True)

    with pytest.raises(bundle.BundleError, match="real directory"):
        bundle.verify_bundle(root, home, system_apps)
