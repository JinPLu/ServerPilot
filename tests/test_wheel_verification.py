"""The release workflows trust scripts/verify_wheel.py, so its guards must fire.

A guard that silently passes is worse than no guard: it is why a wheel shipped
without its Alembic script directory in the first place.
"""

from __future__ import annotations

import importlib.util
import stat
import sys
from pathlib import Path
from zipfile import ZipFile, ZipInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "serverpilot_verify_wheel", ROOT / "scripts" / "verify_wheel.py"
)
assert _spec is not None and _spec.loader is not None
verify_wheel = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = verify_wheel
_spec.loader.exec_module(verify_wheel)

COMPLETE = (
    *sorted(verify_wheel.REQUIRED_FILES),
    "serverpilot/migrations/versions/20260719_0001_initial.py",
    "serverpilot/web/templates/dashboard.html",
)
PLUGIN = "serverpilot/bundled_plugins/slurm-immediate"


def _wheel(
    directory: Path,
    *,
    names: tuple[str, ...] = COMPLETE,
    plugin_mode: int | None = 0o755,
) -> Path:
    path = directory / "serverpilot-9.9.9-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, b"x")
        if plugin_mode is not None:
            info = ZipInfo(PLUGIN)
            info.external_attr = plugin_mode << 16
            archive.writestr(info, b"#!/usr/bin/env python3\n")
    return path


def test_a_complete_wheel_passes(tmp_path: Path) -> None:
    assert verify_wheel.verify(_wheel(tmp_path)) == []


def test_a_plugin_without_the_executable_bit_is_rejected(tmp_path: Path) -> None:
    """Discovery runs each plugin, so a non-executable one is invisible, not loud."""

    problems = verify_wheel.verify(_wheel(tmp_path, plugin_mode=0o644))

    assert problems == [f"{PLUGIN} is not executable"]
    assert not (0o644 & stat.S_IXUSR)


def test_a_wheel_without_any_bundled_plugin_is_rejected(tmp_path: Path) -> None:
    problems = verify_wheel.verify(_wheel(tmp_path, plugin_mode=None))

    assert problems == ["missing serverpilot/bundled_plugins/*"]


@pytest.mark.parametrize(
    ("dropped", "expected"),
    [
        (
            "serverpilot/migrations/versions/20260719_0001_initial.py",
            "missing serverpilot/migrations/versions/*.py",
        ),
        ("serverpilot/web/templates/dashboard.html", "missing serverpilot/web/templates/*.html"),
        ("serverpilot/migrations/env.py", "missing serverpilot/migrations/env.py"),
    ],
)
def test_each_required_tree_and_file_is_guarded(
    tmp_path: Path, dropped: str, expected: str
) -> None:
    names = tuple(name for name in COMPLETE if name != dropped)

    assert verify_wheel.verify(_wheel(tmp_path, names=names)) == [expected]


def test_an_ambiguous_directory_is_refused(tmp_path: Path) -> None:
    """Publishing the wrong artifact is unrecoverable, so never guess which wheel."""

    with pytest.raises(SystemExit):
        verify_wheel._resolve(tmp_path)

    _wheel(tmp_path)
    (tmp_path / "serverpilot-9.9.8-py3-none-any.whl").write_bytes(b"")
    with pytest.raises(SystemExit):
        verify_wheel._resolve(tmp_path)


def test_both_release_workflows_call_the_one_script() -> None:
    """Two inline copies of this check drifted apart once already."""

    workflows = ROOT / ".github" / "workflows"
    for name in ("ci.yml", "pypi-release.yml"):
        assert "scripts/verify_wheel.py" in (workflows / name).read_text(encoding="utf-8")
