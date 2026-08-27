#!/usr/bin/env python3
"""Single place the release workflow and CI read tag, version, and changelog facts.

2.0.0 once shipped as a sealed changelog plus a bumped ``__version__`` with no
tag and no GitHub Release. These helpers are what notice that state, what the
tag-push workflow uses to write the Release notes, and what the Windows
workflow still calls so a dispatched rebuild cannot attach a zip to the wrong
version.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Sequence, Set
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_INIT = ROOT / "src" / "serverpilot" / "__init__.py"
CHINESE_CHANGELOG = ROOT / "CHANGELOG.md"
ENGLISH_CHANGELOG = ROOT / "CHANGELOG.en.md"

VERSION_LITERAL = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
SEALED_VERSION = re.compile(r"^(\d+\.\d+\.\d+)\b")
UNRELEASED = "Unreleased"
UNPUBLISHED_SEAL = "版本已封条目但未发布"


def read_package_version(source: str) -> str:
    match = VERSION_LITERAL.search(source)
    if match is None:
        raise SystemExit("cannot read __version__ from src/serverpilot/__init__.py")
    return match.group(1)


def read_package_version_from_path(path: Path = PACKAGE_INIT) -> str:
    return read_package_version(path.read_text(encoding="utf-8"))


def first_changelog_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("## "):
            return line[3:].strip()
    return None


def sealed_version(heading: str | None) -> str | None:
    if heading is None or heading == UNRELEASED:
        return None
    match = SEALED_VERSION.match(heading)
    return match.group(1) if match else None


def expected_tag(version: str) -> str:
    return f"v{version}"


def tag_mismatch_message(tag: str, version: str) -> str | None:
    if tag != expected_tag(version):
        return f"tag {tag} does not match __version__ {version}"
    return None


def extract_release_notes(changelog: str, version: str) -> str:
    lines = changelog.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("## ") and sealed_version(line[3:].strip()) == version:
            start = index + 1
            break
    if start is None:
        raise SystemExit(f"CHANGELOG.en.md has no heading for {version}")
    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise SystemExit(f"CHANGELOG.en.md heading {version} has no notes")
    return notes


def parse_ls_remote_tags(output: str) -> frozenset[str]:
    tags: set[str] = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        ref = line.split()[-1]
        if not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            continue
        tags.add(name)
    return frozenset(tags)


def unpublished_seal_message(
    heading: str | None,
    package_version: str,
    remote_tags: Set[str],
) -> str | None:
    version = sealed_version(heading)
    if version is None or version != package_version:
        return None
    tag = expected_tag(version)
    if tag in remote_tags:
        return None
    return (
        f"{UNPUBLISHED_SEAL}。CHANGELOG.md 顶部标题已是正式版本 {version}，"
        f"且与 __version__ 一致，但远程不存在 tag {tag}。"
        f"下一步：git tag {tag} && git push origin {tag}；"
        "推送 tag 后 release.yml 会创建 GitHub Release，Windows 工作流再上传 zip。"
    )


def list_remote_tags(remote: str = "origin") -> frozenset[str]:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", remote],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise SystemExit(f"cannot list tags on {remote}: {detail}")
    return parse_ls_remote_tags(result.stdout)


def _tag_argument(explicit: str | None) -> str:
    tag = explicit or os.environ.get("RELEASE_TAG")
    if not tag:
        raise SystemExit("RELEASE_TAG is not set")
    return tag


def _cmd_check_tag(tag: str | None, init: Path) -> int:
    version = read_package_version_from_path(init)
    message = tag_mismatch_message(_tag_argument(tag), version)
    if message is not None:
        print(message, file=sys.stderr)
        return 1
    return 0


def _cmd_notes(changelog: Path, version: str | None, tag: str | None) -> int:
    resolved = version
    if resolved is None:
        resolved = _tag_argument(tag).removeprefix("v")
    print(extract_release_notes(changelog.read_text(encoding="utf-8"), resolved))
    return 0


def _cmd_unpublished_seal(changelog: Path, init: Path) -> int:
    heading = first_changelog_heading(changelog.read_text(encoding="utf-8"))
    version = read_package_version_from_path(init)
    message = unpublished_seal_message(heading, version, list_remote_tags())
    if message is None:
        return 0
    print(message, file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-tag", help="fail unless tag == v{__version__}")
    check.add_argument("tag", nargs="?", default=None)
    check.add_argument("--init", type=Path, default=PACKAGE_INIT)

    notes = sub.add_parser("notes", help="print the CHANGELOG.en.md entry for a version")
    notes.add_argument("--changelog", type=Path, default=ENGLISH_CHANGELOG)
    notes.add_argument("--version", default=None)
    notes.add_argument("--tag", default=None)

    seal = sub.add_parser(
        "unpublished-seal",
        help="fail when the top changelog heading is sealed but the tag is missing",
    )
    seal.add_argument("--changelog", type=Path, default=CHINESE_CHANGELOG)
    seal.add_argument("--init", type=Path, default=PACKAGE_INIT)

    arguments = parser.parse_args(argv)
    if arguments.command == "check-tag":
        return _cmd_check_tag(arguments.tag, arguments.init)
    if arguments.command == "notes":
        return _cmd_notes(arguments.changelog, arguments.version, arguments.tag)
    return _cmd_unpublished_seal(arguments.changelog, arguments.init)


if __name__ == "__main__":
    raise SystemExit(main())
