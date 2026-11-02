"""Content identity shared by a source checkout and its installed runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path


def package_build_id(package_root: Path | None = None) -> str:
    """Hash runtime files, independent of install location and Python caches.

    Call this when a process starts and retain its result. Recomputing it for a
    health request would let an old process advertise files installed after it
    started, even though its imported code was never reloaded.
    """

    root = package_root if package_root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(
            part.startswith(".")
            or part == "__pycache__"
            or part.endswith((".egg-info", ".dist-info"))
            for part in relative.parts
        ):
            continue
        if path.suffix in {".pyc", ".pyo"} or not path.is_file():
            continue
        contents = path.read_bytes()
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)
    return digest.hexdigest()
