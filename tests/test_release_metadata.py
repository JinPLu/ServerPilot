"""Release metadata is derived from the package literal rather than copied by hand."""

from __future__ import annotations

import importlib.metadata
import plistlib
import tomllib
from pathlib import Path

from serverpilot import __version__
from serverpilot.keepalive_protocol import KEEPALIVE_IMPLEMENTATION_VERSION

ROOT = Path(__file__).resolve().parents[1]
PLIST_PLACEHOLDER = "0.0.0"


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
