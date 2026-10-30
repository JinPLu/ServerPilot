from __future__ import annotations

from pathlib import Path

from serverpilot.agent_identity import normalize_client, project_slug, resolve_project


def resolve(tmp_path: Path, **kwargs):  # type: ignore[no-untyped-def]
    base = {"explicit": None, "roots": [], "environment": {}, "cwd": tmp_path / "home", "home": tmp_path / "home"}
    return resolve_project(**{**base, **kwargs})


def test_explicit_beats_every_inferred_source(tmp_path: Path) -> None:
    assert resolve(tmp_path, explicit="Storyboard / MSPC", roots=["file:///work/other"]) == "storyboard-mspc"


def test_a_root_resolves_to_its_repository_not_a_subfolder(tmp_path: Path) -> None:
    repo = tmp_path / "WorldModel"
    (repo / ".git").mkdir(parents=True)
    (repo / "experiments").mkdir()
    assert resolve(tmp_path, roots=[(repo / "experiments").as_uri()]) == "worldmodel"


def test_a_worktree_belongs_to_its_repository(tmp_path: Path) -> None:
    repo = tmp_path / "Storyboard"
    (repo / ".git" / "worktrees" / "brave-otter").mkdir(parents=True)
    worktree = tmp_path / "worktrees" / "brave-otter"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {repo}/.git/worktrees/brave-otter\n", encoding="utf-8")
    assert resolve(tmp_path, cwd=worktree) == "storyboard"


def test_codex_has_no_roots_and_falls_back_to_the_environment_then_cwd(tmp_path: Path) -> None:
    project = tmp_path / "Lingbot"
    project.mkdir()
    assert resolve(tmp_path, environment={"SERVERPILOT_PROJECT": "wave-a"}, cwd=project) == "wave-a"
    assert resolve(tmp_path, cwd=project) == "lingbot"


def test_a_home_directory_is_not_a_project(tmp_path: Path) -> None:
    assert resolve(tmp_path) is None


def test_labels_stay_valid_whatever_the_client_sends() -> None:
    assert normalize_client("claude-code") == "claude-code"
    assert normalize_client("Cursor Agent!") == "Cursor-Agent"
    assert normalize_client("") == "agent-client"
    assert normalize_client("123") == "agent-client"
    assert project_slug("  ") is None
