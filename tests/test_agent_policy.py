from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from serverpilot.agent_contract import (
    AGENT_TOOLS,
    BUSY_GPU_STATUSES,
    MCP_INSTRUCTION_BUDGET_CHARS,
    POLICY_BLOCK_MARKERS,
    generated_agent_files,
    install_policy,
    merge_policy_block,
    render_agent_policy,
    render_mcp_instructions,
    render_policy_block,
)
from serverpilot.cli import app
from serverpilot.mcp_entry import MCP_CLIENTS
from serverpilot.mcp_server import MCP_INSTRUCTIONS, ROUTINE_GPU_STATUS, mcp

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_RULE = ROOT / "CLAUDE.md"
runner = CliRunner()

# Vocabulary the agent surface used to carry and no longer does.  A retired
# name in the contract sends a caller looking for a tool or a field that is
# not there, which is worse than saying nothing.
RETIRED_VOCABULARY = (
    "gpu_claim",
    "gpu_bind_observed_workload",
    "gpu_renew_lease",
    "gpu_set_keepalive",
    "gpu_scheduler",
    "gpu_coordination",
    "coordination_uri",
    "agent_url",
    "codex_thread_id",
    "idempotency_key",
    "advanced",
)
# The rules an agent must not talk itself out of.  Everything else in the
# contract tells a caller how to get work done; these four say which paths
# around ServerPilot are closed, so they are checked by name.
PROHIBITION_TOKENS = ("ssh", "sqlite", "inventory", "nvidia-smi")


def _rendered_contract() -> tuple[str, str]:
    return render_mcp_instructions(), render_agent_policy()


def _tool_schemas() -> dict[str, dict]:
    return {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}


def test_generated_files_are_exactly_what_the_contract_renders() -> None:
    for path, expected in generated_agent_files(ROOT).items():
        assert path.read_text(encoding="utf-8") == expected, path


def test_the_mcp_server_serves_the_rendered_contract() -> None:
    assert render_mcp_instructions() == MCP_INSTRUCTIONS
    assert mcp.instructions == MCP_INSTRUCTIONS


def test_the_contract_covers_the_tool_surface_and_nothing_retired() -> None:
    served = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert served == {tool.name for tool in AGENT_TOOLS}

    for instructions in _rendered_contract():
        lowered = instructions.lower()
        for name in served:
            assert name in lowered
        for retired in RETIRED_VOCABULARY:
            assert retired not in lowered


def test_the_contract_names_only_parameters_the_tools_have() -> None:
    schemas = _tool_schemas()
    policy = render_agent_policy()
    for tool in AGENT_TOOLS:
        schema = schemas[tool.name]
        properties = set(schema.get("properties", {}))
        for parameter in tool.parameter_names:
            assert parameter in properties, (tool.name, parameter)
        # A signature that spells its parameters out spells out every required
        # one, so a new mandatory argument cannot land without the caller
        # being told to pass it.
        if "..." not in tool.parameters:
            assert set(schema.get("required", ())) <= set(tool.parameter_names), tool.name
        assert f"`{tool.signature}`" in policy


def test_the_busy_card_vocabulary_is_spelled_out_and_real() -> None:
    projected = set(ROUTINE_GPU_STATUS.values())
    for status in BUSY_GPU_STATUSES:
        assert status in projected, status
        for instructions in _rendered_contract():
            assert status in instructions, status


def test_the_prohibitions_survive_every_rendering() -> None:
    for instructions in _rendered_contract():
        lowered = instructions.lower()
        for token in PROHIBITION_TOKENS:
            assert token in lowered, token


def test_the_contract_names_no_client_product() -> None:
    # One contract is served to whoever connects. A client's name in it turns
    # a shared rule into that client's rule and invites a second copy for the
    # next one.
    for instructions in _rendered_contract():
        lowered = instructions.lower()
        for client in MCP_CLIENTS:
            assert client not in lowered, client


def test_the_contract_stays_within_its_budget() -> None:
    assert len(render_mcp_instructions()) <= MCP_INSTRUCTION_BUDGET_CHARS


def test_the_repository_rule_stays_a_pointer_rather_than_a_second_contract() -> None:
    # Claude inherits the repository rule instead of maintaining a second,
    # drift-prone ServerPilot contract. A Teamwork bridge block is allowed
    # alongside it, but only as a pointer: everything outside the bridge
    # markers must be exactly "@AGENTS.md", and everything inside them must
    # be either a comment or an "@" reference -- never rule prose.
    claude_text = CLAUDE_RULE.read_text(encoding="utf-8")
    bridge_start, bridge_end = (
        "<!-- TEAMWORK_CLAUDE_BRIDGE_START -->",
        "<!-- TEAMWORK_CLAUDE_BRIDGE_END -->",
    )
    if bridge_start not in claude_text:
        assert claude_text.strip() == "@AGENTS.md"
        return
    before, remainder = claude_text.split(bridge_start, 1)
    bridge_body, after = remainder.split(bridge_end, 1)
    assert before.strip() == "@AGENTS.md"
    assert after.strip() == ""
    lines = [line.strip() for line in bridge_body.strip().splitlines() if line.strip()]
    assert [line for line in lines if line.startswith("@")] == ["@docs/teamwork/README.md"]
    for line in lines:
        if line.startswith("@"):
            continue
        # Prose smuggled into a single HTML comment would reach the model
        # exactly like rules do, so the comment is length-bounded rather than
        # merely comment-shaped.
        assert line.startswith("<!--") and line.endswith("-->"), line
        assert len(line) <= 120, line


def test_policy_block_is_marked_and_carries_the_policy() -> None:
    start, end = POLICY_BLOCK_MARKERS
    block = render_policy_block("# shared policy")
    assert block.startswith(start)
    assert block.endswith(f"{end}\n")
    assert "# shared policy" in block


def test_merge_replaces_only_its_owned_block() -> None:
    old = "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nold\n<!-- SERVERPILOT_GLOBAL_END -->\n\nafter\n"
    merged = merge_policy_block(old, render_policy_block("new"))
    assert merged == "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nnew\n<!-- SERVERPILOT_GLOBAL_END -->\nafter\n"


def test_merge_migrates_a_legacy_gpu_broker_block_in_place() -> None:
    old = "before\n\n<!-- GPU_BROKER_GLOBAL_START -->\nlegacy\n<!-- GPU_BROKER_GLOBAL_END -->\n\nafter\n"
    merged = merge_policy_block(old, render_policy_block("new"))
    assert merged == "before\n\n<!-- SERVERPILOT_GLOBAL_START -->\nnew\n<!-- SERVERPILOT_GLOBAL_END -->\nafter\n"


def test_merge_is_idempotent() -> None:
    block = render_policy_block("new")
    once = merge_policy_block("before\nafter\n", block)
    assert merge_policy_block(once, block) == once


def test_merge_into_an_empty_file_is_just_the_owned_block() -> None:
    block = render_policy_block("new")
    assert merge_policy_block("", block) == block


@pytest.mark.parametrize(
    "existing, message",
    [
        ("<!-- SERVERPILOT_GLOBAL_START -->\nmissing end", "incomplete"),
        ("<!-- SERVERPILOT_GLOBAL_END -->\nmissing start", "incomplete"),
        ("<!-- SERVERPILOT_GLOBAL_END -->\n<!-- SERVERPILOT_GLOBAL_START -->", "malformed"),
        (
            "<!-- SERVERPILOT_GLOBAL_START -->\none\n<!-- SERVERPILOT_GLOBAL_END -->\n"
            "<!-- SERVERPILOT_GLOBAL_START -->\ntwo\n<!-- SERVERPILOT_GLOBAL_END -->",
            "duplicated",
        ),
        (
            "<!-- GPU_BROKER_GLOBAL_START -->\nlegacy\n<!-- GPU_BROKER_GLOBAL_END -->\n"
            "<!-- SERVERPILOT_GLOBAL_START -->\ncurrent\n<!-- SERVERPILOT_GLOBAL_END -->\n",
            "duplicated",
        ),
    ],
)
def test_merge_rejects_invalid_markers(existing: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        merge_policy_block(existing, render_policy_block("new"))


def test_install_refuses_a_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = tmp_path / "actual.md"
    target.write_text("keep me\n", encoding="utf-8")
    (codex_home / "AGENTS.md").symlink_to(target)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    with pytest.raises(ValueError, match="refusing to replace symlink"):
        install_policy("codex", "new policy")

    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert (codex_home / "AGENTS.md").is_symlink()


@pytest.mark.skipif(
    os.name != "posix",
    reason="Windows has no POSIX mode bits for chmod to preserve",
)
def test_install_preserves_an_existing_file_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    policy_path = codex_home / "AGENTS.md"
    policy_path.write_text("existing\n", encoding="utf-8")
    policy_path.chmod(0o640)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    install_policy("codex", "new policy")

    assert stat.S_IMODE(policy_path.stat().st_mode) == 0o640


def test_cli_requires_exactly_one_action() -> None:
    assert runner.invoke(app, ["mcp", "policy"]).exit_code == 2
    assert runner.invoke(app, ["mcp", "policy", "--print", "--check"]).exit_code == 2


def test_cli_print_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = runner.invoke(app, ["mcp", "policy", "--print"])

    assert result.exit_code == 0
    for client in MCP_CLIENTS:
        assert f"[{client}] rules block" in result.stdout
    assert POLICY_BLOCK_MARKERS[0] in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_cli_install_writes_the_file_clients_and_explains_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    # Path.home() reads USERPROFILE on Windows, so HOME alone would let the
    # installer write into the real home directory.
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))

    result = runner.invoke(app, ["mcp", "policy", "--install"])

    assert result.exit_code == 0
    assert "[cursor] keeps its rules in its own settings UI" in result.stdout
    installed = (tmp_path / "codex-home" / "AGENTS.md").read_text(encoding="utf-8")
    assert installed == render_policy_block(render_agent_policy())
    assert (tmp_path / ".claude" / "CLAUDE.md").read_text(encoding="utf-8") == installed


def test_cli_check_passes_on_this_checkout() -> None:
    result = runner.invoke(app, ["mcp", "policy", "--check"])

    assert result.exit_code == 0, result.output
    assert "match the contract" in result.stdout


def test_cli_check_fails_and_shows_the_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = tmp_path / "AGENT_MCP_policy.en.md"
    stale.write_text("someone edited the rendering\n", encoding="utf-8")
    monkeypatch.setattr(
        "serverpilot.cli.generated_agent_files",
        lambda root=None: {stale: "the contract's own text\n"},
    )

    result = runner.invoke(app, ["mcp", "policy", "--check"])

    assert result.exit_code == 1
    assert "someone edited the rendering" in result.output
    assert "the contract's own text" in result.output


def test_cli_check_declines_where_there_is_no_checkout_to_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asked from an installed copy, the question does not apply.

    It used to derive a repository root from the package's own location, which
    inside a tool virtualenv names two files that were never meant to be there
    and reports them missing -- indistinguishable from a broken install.
    """

    monkeypatch.setattr("serverpilot.cli.source_checkout_root", lambda: None)

    result = runner.invoke(app, ["mcp", "policy", "--check"])

    assert result.exit_code == 2
    assert "no source checkout" in result.output
