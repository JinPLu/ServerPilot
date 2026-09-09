"""The one source for the contract every agent-facing surface states.

The MCP server's instructions, the block installed into an agent client's
global rules, ``docs/AGENT_MCP_policy.en.md`` and ``.cursor/rules/serverpilot.mdc``
are all rendered from the tuples below.  They used to be six hand-kept copies
of the same paragraphs, and a copy is a thing that drifts: substring tests can
only notice afterwards, while a renderer cannot produce a disagreement at all.
"""

from __future__ import annotations

import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

# The generated files are tracked in the source checkout. An installed copy of
# the package has no checkout around it, and `parents[2]` there is a directory
# inside the tool's virtualenv -- so this is only a repository root when it
# actually looks like one. `source_checkout_root()` is what callers should ask.
_PACKAGE_PARENT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = _PACKAGE_PARENT


def source_checkout_root(root: Path = _PACKAGE_PARENT) -> Path | None:
    """The checkout this package was imported from, or None if there is none.

    Drift between the contract and the files generated from it is a question
    only a checkout can answer. Asked from an installed copy it used to name
    two paths inside the virtualenv and report them missing, which reads as a
    broken installation rather than as a question that does not apply.
    """

    if (root / "pyproject.toml").is_file() and (root / "src" / "serverpilot").is_dir():
        return root
    return None


@dataclass(frozen=True)
class AgentTool:
    """One tool of the agent surface.

    ``purpose`` is the gloss the opening sentence gives the tool.  gpu_status
    and gpu_release carry none: the standing rules below spend whole lines on
    both, and glossing them again would buy nothing and cost the budget twice.
    """

    name: str
    parameters: tuple[str, ...]
    purpose: str = ""

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(self.parameters)})"

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(
            token.split("=", 1)[0].rstrip("?") for token in self.parameters if token != "..."
        )


AGENT_TOOLS: tuple[AgentTool, ...] = (
    AgentTool("gpu_status", ("server_id?", "lease_id?")),
    AgentTool(
        "gpu_apply",
        ("duration_seconds", "server_group_id?", "server_id?", "gpu_count=1", "task?"),
        "picks the cards itself and keeps one lease on one server "
        "(task=what this lease is for, never the client UI title)",
    ),
    AgentTool("gpu_release", ("lease_id",)),
    AgentTool("gpu_add_server", ("...",), "registers a host"),
    AgentTool("gpu_update_server", ("...",), "updates safe host metadata"),
)

# The statuses a busy card can report that the contract has to spell out.  A
# caller deciding "can I use this card" needs both axes at once: who holds it,
# and whether anything is computing on it.  The other projected states
# (maintenance, unhealthy, and the rest) leave the caller the same move as
# "not yours", so naming them would add vocabulary rather than choices.
BUSY_GPU_STATUSES: tuple[str, ...] = (
    "running",
    "busy_unmanaged",
    "held_idle",
    "ownership_conflict",
)

# Prose names the size of the surface in words, so the word is derived instead
# of typed: a sixth tool has to change this sentence rather than survive it.
_COUNT_WORDS = ("", "One", "Two", "Three", "Four", "Five", "Six")

STANDING_RULES: tuple[str, ...] = (
    "Call gpu_status before allocating; capacity and busy_gpus change between calls. Assess group workspace/environment/data-weight notes, capacity and limits first; choose server_group_id for grouped hosts; the broker best-fits within that group. server_id is for ungrouped compatibility and must not pin a grouped host.",
    "Connection and working directory are projected once per server: ssh=how to connect; workspace.path (workspace_path)=the cwd to enter; code_location=not_provided means workspace_path is never a code repository. Allocation gpus[] point back with server_id.",
    "For direct SSH entries, cuda_device_order=PCI_BUS_ID; cuda_visible_devices=the whole lease, gpu_cuda_visible_devices=one card. Allocation entries require entering the existing provider allocation first; follow entry.argv and its verification status. Physical indexes and in-allocation device selectors are distinct; never guess missing mappings.",
    "gpu_status gives grouped allocatable capacity (name/vram_mib/total_count/available_count), allocation/limits and busy_gpus; server_id narrows to one server. Delegated clusters sit in their server_group; largest_allocatable_block is one apply's max cards.",
    "gpu_status returns resource_nodes for direct hosts and provider compute nodes with the same GPU/process and CPU/memory fields, sample times and freshness. Node utilization describes the node or declared container scope, not a job's exclusive contribution. Provider allocation counts are scheduling facts, never measured utilization. Job accounting RSS and CPU time are separate from node telemetry. Do not infer availability from idle provider GPUs; allocation still goes through the provider. A node GPU's state=KEEPALIVE is ServerPilot's own hold on a card nobody has claimed: it counts as available capacity and is stopped before allocation, so its processes, memory and utilization are never another caller's work.",
    "Telemetry for workload tuning is only meaningful on cards you hold: gpu_status(lease_id=...) returns leased_gpus with recent_average per card plus a lease summary (min_memory_free_mib, slowest_gpu) for tuning batch size and parallelism. Covers your hold only: null until your work is observed; current reads the card now.",
    "Declare duration_seconds when applying. no_capacity and group_selection_required are answers; nothing queues. allocation_unconfirmed means reconcile the original request, never create a replacement allocation. Read open_leases before applying. Release when your task ends or fails and confirm released; zero running GPUs alone does not mean a task is finished. gpu_status(lease_id=...) is your heartbeat during preparation and quiet phases. Whole-allocation providers reclaim only after lost heartbeats and continuously observed idle resources; stale data cannot prove idle. Respect the provider deadline: local renewal cannot extend it.",
    "busy_gpus[]: task=who holds the card, status=whether it is working — running (a compute process under that lease), busy_unmanaged (a process ServerPilot does not own), held_idle (held, nothing computing: ask the holder or pick elsewhere), ownership_conflict (holder unverifiable). Only running and busy_unmanaged mean work is on the card.",
    "ServerPilot only coordinates GPUs. Do not use SSH, SQLite, inventory or nvidia-smi to work around it. Non-GPU remote work such as syncing a repository needs no lease.",
)

# What the contract costs an agent is paid every turn, so it stays bounded.
# The bound has moved fifteen times, always because a caller that is not told
# something re-derives it wrongly: per-GPU idle reclaim (or it over-claims),
# lease-scoped telemetry and state=KEEPALIVE named as the token a caller
# actually reads (or it reads a free card's load -- ServerPilot's own hold --
# as taken), grouped capacity and server_group_id (or two 4-GPU hosts
# read as one 8-GPU menu), the five-tool surface, largest_allocatable_block as
# one apply's ceiling, open_leases (a caller that cannot name its lease cannot
# release it, so reclaim read as the mechanism), the busy_gpus status
# vocabulary (or a held card reads as a running one), gpu_status(lease_id=...)
# as the heartbeat (or a quiet phase settles a live claim), and release on
# success rather than only on failure.  One move was not a rule at all: the
# text became English, longer in characters and cheaper in tokens.  Contract
# sentences are never cut to fit this bound; when the contract needs more
# words, this number moves and the reason is written here.
# Node-scope telemetry and provider lifecycle are now part of the common surface.
MCP_INSTRUCTION_BUDGET_CHARS = 4200

POLICY_TITLE = "# ServerPilot — Agent MCP Rules"
POLICY_INTRO = "Use the local `serverpilot` MCP."
# The one sentence the installed rules say and the runtime instructions do
# not.  Installed rules are read where a client is being set up, so they can
# afford to say where the work this surface does not cover happens; the
# runtime text pays for every word in every turn.
POLICY_SURFACE_NOTE = (
    "tools are the whole MCP surface; server deletion and other lifecycle work "
    "happen in the app or through REST."
)

CURSOR_RULE_FRONTMATTER = (
    "---\ndescription: ServerPilot MCP usage — routine GPU allocation\nalwaysApply: true\n---\n"
)
POLICY_BLOCK_MARKERS = ("<!-- SERVERPILOT_GLOBAL_START -->", "<!-- SERVERPILOT_GLOBAL_END -->")
# Global rule files written before the gpu-broker rename still carry the old
# markers, and an install has to replace that block instead of adding a second.
LEGACY_POLICY_BLOCK_MARKERS = ("<!-- GPU_BROKER_GLOBAL_START -->", "<!-- GPU_BROKER_GLOBAL_END -->")


def render_mcp_instructions() -> str:
    """The text the MCP server hands an agent when it connects."""

    surface = "; ".join(f"{tool.name} {tool.purpose}".strip() for tool in AGENT_TOOLS)
    opening = f"{_COUNT_WORDS[len(AGENT_TOOLS)]} tools cover GPU work: {surface}."
    return "\n".join((opening, *STANDING_RULES))


def render_agent_policy() -> str:
    """The same contract as Markdown, for a client's global rules."""

    lines = [POLICY_TITLE, "", POLICY_INTRO, ""]
    for number, tool in enumerate(AGENT_TOOLS, start=1):
        gloss = f" — {tool.purpose}" if tool.purpose else ""
        lines.append(f"{number}. `{tool.signature}`{gloss}")
    surface_word = _COUNT_WORDS[len(AGENT_TOOLS)].lower()
    lines += ["", f"These {surface_word} {POLICY_SURFACE_NOTE}"]
    for rule in STANDING_RULES:
        lines += ["", rule]
    return "\n".join(lines) + "\n"


def render_policy_block(policy: str) -> str:
    start, end = POLICY_BLOCK_MARKERS
    return f"{start}\n{policy.rstrip()}\n{end}\n"


def generated_agent_files(root: Path = REPOSITORY_ROOT) -> dict[Path, str]:
    """The tracked files that are renderings of this contract, not sources."""

    policy = render_agent_policy()
    return {
        root / "docs" / "AGENT_MCP_policy.en.md": policy,
        root / ".cursor" / "rules" / "serverpilot.mdc": (
            CURSOR_RULE_FRONTMATTER + render_policy_block(policy)
        ),
    }


def policy_destination(client: str) -> Path | None:
    """The global rule file to edit, or None where the client owns no file."""

    if client == "codex":
        return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "AGENTS.md"
    if client == "claude":
        return Path.home() / ".claude" / "CLAUDE.md"
    return None


def merge_policy_block(existing: str, block: str) -> str:
    """Replace this contract's own block, leaving the rest of the file alone."""

    current_start, current_end = POLICY_BLOCK_MARKERS
    legacy_start, legacy_end = LEGACY_POLICY_BLOCK_MARKERS
    current_count = existing.count(current_start)
    legacy_count = existing.count(legacy_start)

    if current_count != existing.count(current_end) or legacy_count != existing.count(legacy_end):
        raise ValueError("existing ServerPilot policy markers are incomplete")
    if current_count > 1 or legacy_count > 1:
        raise ValueError("existing ServerPilot policy markers are duplicated")
    if current_count and legacy_count:
        raise ValueError(
            "existing ServerPilot policy markers are duplicated across legacy and current blocks"
        )

    if current_count or legacy_count:
        start, end = (current_start, current_end) if current_count else (legacy_start, legacy_end)
        begin = existing.find(start)
        finish = existing.find(end)
        if finish < begin:
            raise ValueError("existing ServerPilot policy markers are malformed")
        finish += len(end)
        return existing[:begin].rstrip() + "\n\n" + block.rstrip() + "\n" + existing[finish:].lstrip()
    if not existing.strip():
        return block
    return existing.rstrip() + "\n\n" + block


def install_policy(client: str, policy: str) -> Path:
    """Write this contract's block into one client's global rule file."""

    path = policy_destination(client)
    if path is None:
        raise ValueError(
            f"{client} keeps its rules in its own settings UI; "
            f"use --print --client {client} and paste them there"
        )
    if path.is_symlink():
        raise ValueError(f"refusing to replace symlink: {path}")

    mode: int | None = None
    if path.exists():
        path_stat = path.stat()
        if not stat.S_ISREG(path_stat.st_mode):
            raise ValueError(f"refusing to replace non-regular file: {path}")
        mode = stat.S_IMODE(path_stat.st_mode)
        existing = path.read_text(encoding="utf-8")
    else:
        existing = ""
    updated = merge_policy_block(existing, render_policy_block(policy))
    path.parent.mkdir(parents=True, exist_ok=True)
    staged: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            handle.write(updated)
            staged = Path(handle.name)
        if mode is not None:
            staged.chmod(mode)
        if path.is_symlink():
            raise ValueError(f"refusing to replace symlink: {path}")
        os.replace(staged, path)
        staged = None
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return path

