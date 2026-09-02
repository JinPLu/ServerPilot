"""Release metadata is derived from the package literal rather than copied by hand."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import plistlib
import sys
import tomllib
from pathlib import Path

import yaml

from serverpilot import __version__
from serverpilot.keepalive_protocol import KEEPALIVE_IMPLEMENTATION_VERSION

ROOT = Path(__file__).resolve().parents[1]
PLIST_PLACEHOLDER = "0.0.0"

_spec = importlib.util.spec_from_file_location(
    "serverpilot_release_metadata", ROOT / "scripts" / "release_metadata.py"
)
assert _spec is not None and _spec.loader is not None
release_metadata = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release_metadata
_spec.loader.exec_module(release_metadata)


def test_release_version_is_derived_from_the_package_literal() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_script = (ROOT / "desktop" / "build-macos-app.sh").read_text(encoding="utf-8")
    info = plistlib.loads((ROOT / "desktop" / "Info.plist").read_bytes())

    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert project["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "serverpilot.__version__"
    }
    assert importlib.metadata.version("serverpilot") == __version__
    assert __version__ == KEEPALIVE_IMPLEMENTATION_VERSION
    assert "src/serverpilot/__init__.py" in build_script
    assert 'plutil -replace CFBundleShortVersionString -string "${release_version}"' in build_script
    assert info["CFBundleShortVersionString"] == PLIST_PLACEHOLDER
    assert str(info["CFBundleVersion"]).isdigit()


def test_the_cli_can_report_its_version() -> None:
    """The bug report template asks a reporter to run this."""

    from typer.testing import CliRunner

    from serverpilot.cli import app

    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_both_changelogs_describe_the_same_release_history() -> None:
    """The two files drifted once: English kept a 1.5.4 that was never tagged.

    A reader who picks the Chinese changelog must not see a different set of
    releases than a reader who picks the English one.
    """

    def headings(name: str) -> list[str]:
        text = (ROOT / name).read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.startswith("## ")]

    assert headings("CHANGELOG.md") == headings("CHANGELOG.en.md")


def test_the_package_reports_its_version_without_installed_metadata() -> None:
    """The remote collector and keepalive helpers run from a plain source tree.

    Reading distribution metadata there raises PackageNotFoundError at import,
    which would take the sealed helper down before it can answer --protocol-info.
    """

    source = (ROOT / "src" / "serverpilot" / "__init__.py").read_text(encoding="utf-8")

    assert "importlib.metadata" not in source
    assert f'__version__ = "{__version__}"' in source


CHANGELOG_UNRELEASED = """# Changelog

## Unreleased

Pending work.

## 2.0.0 - 2026-08-28

Shipped notes.
"""

CHANGELOG_SEALED = """# Changelog

## 2.0.0 - 2026-08-28

**Source is now the daily product.**

- Routine MCP apply is gpu_apply.

## 1.9.1 - 2026-08-27

Older notes.
"""

LS_REMOTE = """\
c4720af93997404ac70e2d25e1548f599b28bd08\trefs/tags/v2.0.0
f864fc96bd67aa389bb7e7f2024a6a2e54fd2dfc\trefs/tags/v2.0.0^{}
abc123\trefs/tags/v1.9.1
"""


def test_the_package_version_is_read_from_the_literal() -> None:
    assert release_metadata.read_package_version('__version__ = "2.0.0"\n') == "2.0.0"


def test_an_unreleased_heading_is_not_an_unpublished_seal() -> None:
    heading = release_metadata.first_changelog_heading(CHANGELOG_UNRELEASED)

    assert heading == "Unreleased"
    assert release_metadata.sealed_version(heading) is None
    assert (
        release_metadata.unpublished_seal_message(heading, "2.0.0", frozenset()) is None
    )


def test_a_sealed_version_without_its_tag_is_an_unpublished_seal() -> None:
    heading = release_metadata.first_changelog_heading(CHANGELOG_SEALED)
    message = release_metadata.unpublished_seal_message(heading, "2.0.0", frozenset())

    assert heading == "2.0.0 - 2026-08-28"
    assert message is not None
    assert "版本已封条目但未发布" in message
    assert "v2.0.0" in message
    assert "git tag v2.0.0" in message


def test_a_sealed_version_with_its_remote_tag_is_published() -> None:
    heading = release_metadata.first_changelog_heading(CHANGELOG_SEALED)

    assert (
        release_metadata.unpublished_seal_message(heading, "2.0.0", frozenset({"v2.0.0"}))
        is None
    )


def test_a_sealed_heading_that_is_not_the_package_version_is_ignored() -> None:
    heading = release_metadata.first_changelog_heading(CHANGELOG_SEALED)

    assert (
        release_metadata.unpublished_seal_message(heading, "2.0.1", frozenset()) is None
    )


def test_release_notes_are_the_english_entry_body() -> None:
    notes = release_metadata.extract_release_notes(CHANGELOG_SEALED, "2.0.0")

    assert notes.startswith("**Source is now the daily product.**")
    assert "gpu_apply" in notes
    assert "1.9.1" not in notes
    assert "Older notes" not in notes


def test_a_mismatched_tag_is_refused() -> None:
    assert release_metadata.tag_mismatch_message("v2.0.0", "2.0.0") is None
    assert (
        release_metadata.tag_mismatch_message("v1.9.1", "2.0.0")
        == "tag v1.9.1 does not match __version__ 2.0.0"
    )


def test_peeled_ls_remote_rows_are_not_duplicate_tags() -> None:
    assert release_metadata.parse_ls_remote_tags(LS_REMOTE) == frozenset(
        {"v2.0.0", "v1.9.1"}
    )


def test_pytest_reads_src_without_reinstalling_the_package() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src"]


def test_ci_still_reinstalls_the_package_and_refuses_an_unpublished_seal() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "--reinstall-package serverpilot" in workflow
    assert "scripts/release_metadata.py unpublished-seal" in workflow
    assert "git fetch --tags" in workflow
    assert "fetch-depth: 0" in workflow


def test_a_version_tag_creates_the_github_release_and_not_a_pypi_upload() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/release_metadata.py check-tag" in workflow
    assert "scripts/release_metadata.py notes" in workflow
    assert "gh release create" in workflow
    assert "Compress-Archive" not in workflow
    assert "pypi" not in workflow.lower()


def test_check_tag_and_notes_cli_use_the_shared_rules(tmp_path: Path) -> None:
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "2.0.0"\n', encoding="utf-8")
    changelog = tmp_path / "CHANGELOG.en.md"
    changelog.write_text(CHANGELOG_SEALED, encoding="utf-8")

    assert release_metadata.main(["check-tag", "v2.0.0", "--init", str(init)]) == 0
    assert release_metadata.main(["check-tag", "v1.9.1", "--init", str(init)]) == 1
    assert (
        release_metadata.main(
            ["notes", "--changelog", str(changelog), "--version", "2.0.0"]
        )
        == 0
    )


def test_release_workflows_are_valid_yaml() -> None:
    for name in ("release.yml", "ci.yml"):
        yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
