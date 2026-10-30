"""Work out who an MCP process is from what its client actually tells it.

Clients differ: Claude Code and Cursor declare roots, Codex does not, and a
long-lived client may run from a directory that is no project. The project
name also has to be derived, not chosen by the agent, or one project becomes
four spellings of itself.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ENVIRONMENT_VARIABLE = "SERVERPILOT_PROJECT"
MAX_PROJECT_LENGTH = 64


def normalize_client(name: str | None) -> str:
    """A stable actor label from clientInfo.name; a label, never a credential."""

    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", (name or "").strip()).strip("-.")
    if not cleaned or not cleaned[0].isalpha():
        return "agent-client"
    return cleaned[:120]


def project_slug(value: str) -> str | None:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-.")
    return slug[:MAX_PROJECT_LENGTH] or None


def _git_top_level(path: Path) -> Path:
    for candidate in (path, *path.parents):
        marker = candidate / ".git"
        if marker.is_file():
            return _worktree_repository(marker) or candidate
        if marker.exists():
            return candidate
    return path


def _worktree_repository(marker: Path) -> Path | None:
    """The repository a git worktree belongs to, so it stays the same project.

    A worktree's name is a branch or a generated word. Taken as the project, it
    would split one project's sessions from each other.
    """

    try:
        content = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.match(r"gitdir:\s*(.+?)/\.git/worktrees/", content.strip())
    return Path(match.group(1)) if match else None


def _from_directory(path: Path, home: Path) -> str | None:
    path = path.expanduser()
    if path in (home, Path(path.anchor)):
        return None
    return project_slug(_git_top_level(path).name)


def root_path(uri: str) -> Path | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return Path(unquote(parsed.path))


def resolve_project(
    *,
    explicit: str | None,
    roots: list[str],
    environment: Mapping[str, str],
    cwd: Path,
    home: Path,
) -> str | None:
    """Explicit, then the client's root, then the environment, then the cwd."""

    if explicit and explicit.strip():
        return project_slug(explicit)
    for uri in roots:
        path = root_path(uri)
        if path is not None:
            found = _from_directory(path, home)
            if found:
                return found
    configured = environment.get(PROJECT_ENVIRONMENT_VARIABLE, "")
    if configured.strip():
        return project_slug(configured)
    return _from_directory(cwd, home)
