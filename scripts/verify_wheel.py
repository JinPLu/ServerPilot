#!/usr/bin/env python3
"""Assert a built wheel carries every asset the installed package needs at runtime.

The package reaches users three ways (PyPI, the macOS app, the Windows archive)
and each one has already shipped a wheel that imported fine and then failed on
first use: a missing Alembic script directory takes the database down, and a
bundled plugin that arrives without its executable bit is silently invisible to
discovery rather than failing loudly. Both release workflows call this so the
two cannot drift apart.
"""

from __future__ import annotations

import argparse
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from zipfile import ZipFile

REQUIRED_FILES = frozenset(
    {
        "serverpilot/migrations/env.py",
        "serverpilot/migrations/script.py.mako",
    }
)
REQUIRED_TREES = (
    ("serverpilot/migrations/versions/", ".py"),
)
PLUGIN_TREE = "serverpilot/bundled_plugins/"


def _resolve(target: Path) -> Path:
    if target.is_dir():
        wheels = sorted(target.glob("*.whl"))
        if len(wheels) != 1:
            raise SystemExit(f"expected exactly one wheel in {target}, found {len(wheels)}")
        return wheels[0]
    if target.suffix != ".whl":
        raise SystemExit(f"{target} is not a wheel")
    return target


def verify(wheel: Path) -> list[str]:
    with ZipFile(wheel) as archive:
        entries = archive.infolist()
    names = {entry.filename for entry in entries}
    problems = [f"missing {name}" for name in sorted(REQUIRED_FILES - names)]
    for prefix, suffix in REQUIRED_TREES:
        if not any(name.startswith(prefix) and name.endswith(suffix) for name in names):
            problems.append(f"missing {prefix}*{suffix}")
    plugins = [
        entry
        for entry in entries
        if entry.filename.startswith(PLUGIN_TREE) and not entry.filename.endswith("/")
    ]
    if not plugins:
        problems.append(f"missing {PLUGIN_TREE}*")
    problems.extend(
        f"{entry.filename} is not executable"
        for entry in plugins
        if not (entry.external_attr >> 16) & stat.S_IXUSR
    )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="a wheel, or a directory holding exactly one")
    arguments = parser.parse_args(argv)

    wheel = _resolve(arguments.target)
    problems = verify(wheel)
    if problems:
        for problem in problems:
            print(f"{wheel.name}: {problem}", file=sys.stderr)
        return 1
    print(f"{wheel.name}: runtime assets complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
