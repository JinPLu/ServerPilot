#!/usr/bin/env python3
"""Identify and verify the one installed desktop bundle without copying it."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import sys
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

APP_BUNDLE_ID = "local.serverpilot.desktop"
OWNED_BUNDLE_IDS = {APP_BUNDLE_ID, "local.gpu-broker.desktop"}


class BundleError(RuntimeError):
    """The app cannot be identified, cleaned up or verified safely."""


def _absolute(path: Path) -> Path:
    # Do not resolve aliases: the canonical location itself may not be a link.
    return Path(os.path.abspath(path))


def _version(project_root: Path) -> str:
    source = project_root / "src/serverpilot/__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                return value.value
            break
    raise BundleError(f"Missing literal __version__ in {source}")


def _backend_build_id(package_root: Path) -> str:
    # This command runs before installing dependencies. Load the shared stdlib
    # fingerprint directly instead of importing an old installed backend.
    source = Path(__file__).resolve().parents[1] / "src/serverpilot/runtime_identity.py"
    spec = importlib.util.spec_from_file_location("serverpilot_update_identity", source)
    if spec is None or spec.loader is None:
        raise BundleError(f"Cannot load runtime identity from {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.package_build_id(package_root)


def build_identity(project_root: Path) -> dict[str, str]:
    """Return identities for the current checkout's app inputs and backend."""

    project_root = _absolute(project_root)
    try:
        version = _version(project_root)
        package_root = project_root / "src/serverpilot"
        backend_build_id = _backend_build_id(package_root)
        inputs = {
            project_root / "desktop/Info.plist",
            project_root / "desktop/assets/ServerPilot.icns",
            project_root / "configs/inventory.yaml",
            *(project_root / "desktop").glob("*.swift"),
            *(project_root / "desktop/ServerPilotCore/Sources").rglob("*.swift"),
            *(project_root / "desktop/Fixtures").glob("*.json"),
        }
        digest = hashlib.sha256()
        digest.update(b"backend-build-id\0" + backend_build_id.encode("ascii") + b"\0")
        for path in sorted(inputs):
            contents = path.read_bytes()
            digest.update(path.relative_to(project_root).as_posix().encode("utf-8") + b"\0")
            digest.update(len(contents).to_bytes(8, "big"))
            digest.update(contents)
    except (OSError, SyntaxError, ValueError) as exc:
        raise BundleError(f"Cannot identify desktop build: {exc}") from exc
    return {
        "version": version,
        "app_build_id": digest.hexdigest(),
        "backend_build_id": backend_build_id,
    }


def _app_entry(path: Path) -> bool:
    return path.match("ServerPilot*.app") and (path.is_dir() or path.is_symlink())


def duplicate_apps(
    project_root: Path,
    home: Path,
    applications: Path = Path("/Applications"),
) -> list[Path]:
    """Find every noncanonical app/alias without walking into app bundles."""

    project_root = _absolute(project_root)
    canonical = project_root / "ServerPilot.app"
    found: set[Path] = set()
    for current, directories, files in os.walk(project_root, followlinks=False):
        base = Path(current)
        for name in [*directories, *files]:
            path = base / name
            if path != canonical and _app_entry(path):
                found.add(path)
        directories[:] = [
            name for name in directories
            if not name.endswith(".app") and not (base / name).is_symlink()
        ]
    for folder in [_absolute(home) / "Applications", _absolute(applications)]:
        if not folder.is_symlink() and folder.is_dir():
            found.update(path for path in folder.iterdir() if path != canonical and _app_entry(path))
    return sorted(found)


def _bundle_info(app: Path) -> dict[str, Any]:
    try:
        value = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError) as exc:
        raise BundleError(f"Cannot read app identity: {app}") from exc
    if not isinstance(value, dict):
        raise BundleError(f"Invalid app identity: {app}")
    return value


def clean_duplicate_apps(
    project_root: Path,
    home: Path,
    applications: Path = Path("/Applications"),
) -> list[Path]:
    """Remove only owned bundles or aliases, after preflighting every match."""

    duplicates = duplicate_apps(project_root, home, applications)
    for app in duplicates:
        if not app.is_symlink() and _bundle_info(app).get("CFBundleIdentifier") not in OWNED_BUNDLE_IDS:
            raise BundleError(f"Refusing to remove an unrelated app: {app}")
    for app in duplicates:
        if app.is_symlink():
            app.unlink()
        else:
            shutil.rmtree(app)
    return duplicates


def verify_bundle(
    project_root: Path,
    home: Path,
    applications: Path = Path("/Applications"),
) -> dict[str, str]:
    """Require the root bundle to match this build and be the only app copy."""

    project_root = _absolute(project_root)
    canonical = project_root / "ServerPilot.app"
    if canonical.is_symlink() or not canonical.is_dir():
        raise BundleError(f"Canonical app must be a real directory: {canonical}")
    expected = build_identity(project_root)
    info = _bundle_info(canonical)
    fields = {
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleShortVersionString": expected["version"],
        "ServerPilotBuildID": expected["app_build_id"],
        "ServerPilotBackendBuildID": expected["backend_build_id"],
    }
    for key, value in fields.items():
        if info.get(key) != value:
            raise BundleError(f"Canonical app has stale or invalid {key}: {canonical}")
    duplicates = duplicate_apps(project_root, home, applications)
    if duplicates:
        raise BundleError("Duplicate ServerPilot apps remain: " + ", ".join(map(str, duplicates)))
    return {"app_path": str(canonical), **expected}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["identity", "cleanup", "verify"]:
        command = commands.add_parser(name)
        command.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "identity":
            result = build_identity(args.project_root)
        elif args.command == "cleanup":
            result = {"removed": [str(path) for path in clean_duplicate_apps(args.project_root, Path.home())]}
        else:
            result = verify_bundle(args.project_root, Path.home())
    except (BundleError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
