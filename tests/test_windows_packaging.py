"""The Windows archive must carry everything the app and an agent need.

These are shape assertions on the PyInstaller spec and the release workflow.
The build itself only runs on a Windows runner, so the checks that a build can
make -- that the executables exist and that the MCP entry point starts -- live
in .github/workflows/windows-desktop-release.yml.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = (ROOT / "desktop" / "windows" / "ServerPilotWindows.spec").read_text(encoding="utf-8")
RELEASE = (
    ROOT / ".github" / "workflows" / "windows-desktop-release.yml"
).read_text(encoding="utf-8")


def test_migration_modules_ship_as_data() -> None:
    # collect_data_files skips .py by default, which produced a build that
    # could not create its own database.
    assert 'collect_data_files("serverpilot.migrations", include_py_files=True)' in SPEC


def test_the_archive_carries_an_mcp_entry_point() -> None:
    # Without this the download is GUI-only and no agent can reach ServerPilot
    # on Windows, because serverpilot-mcp is never placed on PATH.
    assert 'serverpilot" / "mcp_server.py"' in SPEC
    assert 'name="serverpilot-mcp"' in SPEC


def test_the_mcp_entry_point_is_a_console_program() -> None:
    # stdio JSON-RPC needs real stdin/stdout; a windowed build has neither.
    mcp_exe = SPEC.split('name="serverpilot-mcp"', 1)[1]
    assert "console=True" in mcp_exe.split("entitlements_file", 1)[0]


def test_both_entry_points_share_one_collect_output() -> None:
    assert "MERGE(" in SPEC
    collect = SPEC.split("coll = COLLECT(", 1)[1]
    assert "mcp_exe" in collect
    assert "mcp.binaries" in collect


def test_the_release_verifies_the_packaged_tree() -> None:
    for required in (
        "serverpilot\\migrations",
        "serverpilot-mcp.exe",
    ):
        assert required in RELEASE, required
