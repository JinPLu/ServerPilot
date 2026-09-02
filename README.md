<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="96" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center">
  <strong>Agents take their own GPUs. You watch the whole fleet.</strong><br>
  MCP for agents · Native desktop app · Open source
</p>

<p align="center">
  <a href="https://github.com/JinPLu/ServerPilot/blob/master/README.zh-CN.md">中文</a> ·
  <a href="#-what-it-does">What it does</a> ·
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-how-an-agent-uses-it">Agent usage</a> ·
  <a href="#-boundaries-and-security">Boundaries</a> ·
  <a href="#-documentation">Docs</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-native%20App-111827?logo=apple&logoColor=white" alt="Native macOS App">
  <img src="https://img.shields.io/badge/MCP-5%20routine%20tools-7C3AED" alt="Five routine MCP tools">
  <a href="https://github.com/JinPLu/ServerPilot/blob/master/LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/assets/serverpilot-workflow-cartoon.png" width="960" alt="ServerPilot gathers scattered GPUs into a pool agents can coordinate over, with a human watching">
</p>

<p align="center"><sub>Illustration only; not a real resource state or app screen.</sub></p>

> Your agents write code and run experiments on their own. Are you still handing
> them GPUs one card at a time?

To an agent, ServerPilot is an MCP server: look at cards, take some, give them
back, and register or update a host. To you, it is a macOS app showing what
is free, what is busy, whose job is where, and what is failing
across every server you registered. There is no browser UI.

One local user, several servers, several agents. Resource state, requests, and
human correction all read the same committed snapshot from one local control
plane.

| What it gives you | How |
| --- | --- |
| One source of truth | The GUI, the CLI, and MCP all read the daemon's committed snapshot instead of each deriving state. |
| A three-step loop for agents | `gpu_status → gpu_apply → gpu_release` covers routine coordination. |
| A view for humans | Servers, tasks, ownership, and failures, with bounded manual correction when something is wrong. |
| Per-card idle holding | Optional keepalive yields only the card being requested and restores the rest. |

## ✨ What it does

### 🛰️ One picture of every server

Server state refreshes on the collection interval you set. Free cards, busy
cards, which task owns what, and collector failures are all visible in the app.
Servers can sit in a first-class group that carries a shared workspace plus
environment and data/weight notes; a member inherits that workspace or
overrides it. Environment notes are descriptive only. Allocatable capacity is
shown group → server → SKU, not as a menu of individual free cards.

### 🧩 Agents allocate for themselves

The MCP surface is exactly five tools:

- 🔍 `gpu_status` — grouped allocatable capacity, what is busy and to whom, and
  telemetry for cards you already hold
- 🔑 `gpu_apply` — take GPUs (`server_group_id?`, `server_id?`, `gpu_count=1`,
  `task?`)
- ♻️ `gpu_release` — give them back
- ➕ `gpu_add_server` — register a host
- ✏️ `gpu_update_server` — update safe host metadata

A successful request returns the SSH connection, the remote working directory,
a CUDA selector, and a `lease_id`, so an agent never has to guess a server, a
directory, or a GPU index. Deleting a server stays in the app and REST; it is
not an MCP tool, and it refuses while that server holds active leases.

### 🛠️ You step in only when something is wrong

While agents work normally you only watch. When ownership looks wrong, a
connection fails, or a hold is left behind, you confirm and correct it: settle a
stale lease, or move a task to different GPUs while keeping the card count.

### 🐶 Optional: hold idle GPUs so nobody else takes them

Per-card keepalive can keep confirmed-idle GPUs on standby. When an agent
requests one, only that card is yielded, and it returns to standby after
release. Other cards on the same machine are untouched.

Use it only on servers you administer. It never touches an unknown process or
someone else's running job.

Keepalive workers run detached from the control plane. If the control plane
stops, those GPUs stay occupied until it comes back and reconciles, or until
someone stops them. There is no automatic release on shutdown.

When the control plane is unreachable, these commands put things back. None of
them needs the daemon to be running. They act on the macOS control plane's data
directory, so they do not apply to a Windows install:

```bash
serverpilot keepalive inspect --endpoint <server-id>   # what is still holding cards
serverpilot keepalive stop --endpoint <server-id>      # stop it and free them
serverpilot daemon reclaim                             # take port 8787 back
```

`daemon reclaim` only resolves the case where a ServerPilot service answers on
8787 without launchd owning it; when the daemon is properly owned it does
nothing. The port-ownership error names the holding process and its command
line.

## 🚀 Quick start

### 1. 🧰 Start the local control plane

On macOS the CLI **is** the backend. `uv tool install` installs the control
plane the daemon will run; the desktop app is only the GUI. Opening the app
does not start or replace that process.

**macOS**, from source, with [Python 3.12+](https://www.python.org/) and
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

`daemon install` registers a user LaunchAgent that starts the `uv tool`
install, and is macOS-only. After a later upgrade, confirm the process on
`http://127.0.0.1:8787/health/live` actually reports the new version — see the
[upgrade checklist](https://github.com/JinPLu/ServerPilot/blob/master/docs/UPGRADE_CHECKLIST_zh.md)
(Chinese).

**Windows**: the desktop app is macOS-only, but the CLI and MCP entry points
work on Windows. From source, use
`serverpilot serve --db <path> --inventory <path>` and keep that process
running; there is no supervised install on Windows yet.

Either way it listens on `http://127.0.0.1:8787`.

### 2. 🖥️ Register your GPU servers

ServerPilot reads a server only through one fixed command, so that command has
to exist there first. On each Linux GPU server you administer, install the same
version of this package for the user you will SSH in as:

```bash
# ServerPilot is not on PyPI. Install the version your control plane runs;
# `serverpilot --version` prints it there.
uv tool install --force "git+https://github.com/JinPLu/ServerPilot.git@v<version>"
serverpilot-collect --schema-version 2       # must print JSON
```

The version must match your control plane, and the entry point must be on the
`PATH` of a **non-interactive** SSH session. That is the check that matters:

```bash
ssh user@host serverpilot-collect --schema-version 2
```

A `PATH` set up only for login shells is the usual reason this step looks fine
when you SSH in by hand and then fails from the control plane.

Then add the SSH connection and an absolute remote working directory in the app.
A GPU becomes allocatable only after a fresh collection succeeds. The full
protocol, including what the output must contain, is in the
[collector protocol](https://github.com/JinPLu/ServerPilot/blob/master/docs/COLLECTOR_SCRIPT_zh.md) (Chinese).

Do not register a shared cluster (Slurm, LSF, PBS) as bare metal. A local
plugin takes over observation so only your own jobs are registered, and
requests go through that plugin's `apply` / `release`. The bundled
`slurm-immediate` plugin is the reference; there is no separate scheduler
submission surface. See
[server plugins](https://github.com/JinPLu/ServerPilot/blob/master/docs/PLUGINS_zh.md)
for the contract.

### 3. 🤖 Connect an agent

```bash
serverpilot mcp install --client codex     # or claude, cursor
python3 scripts/install_agent_policy.py codex --install
```

`serverpilot mcp install` only writes the launch command: Codex and Claude
Code get their `mcp add`, and Cursor is merged into `~/.cursor/mcp.json`
without disturbing servers already there. It does not refresh a client's
cached tool list — reconnect the server (Cursor: Disable → Enable, or reload
the window) so it runs `tools/list` again.

To paste it yourself, `serverpilot mcp config --client all` prints the
registration without writing anything. The standard block is:

```json
{
  "mcpServers": {
    "serverpilot": {
      "command": "serverpilot-mcp",
      "env": { "SERVERPILOT_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

The CLI and MCP entry points work the same way on Windows; the desktop app is
macOS-only. Full per-client notes are in the
[Agent / MCP guide](https://github.com/JinPLu/ServerPilot/blob/master/docs/AGENT_MCP_zh.md).

## 🤖 How an agent uses it

```text
gpu_status → gpu_apply(server_group_id=<group>, gpu_count=<launch config>, task="my task") → use what it returns → gpu_release(lease_id)
```

- Routine apply is `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)`.
  Agents never pass GPU IDs; `gpu_apply` picks the cards, and one lease always
  lands on a single server. `gpu_count` is exact job parallelism from the launch
  script or config. The safe default is 1. Never infer it from free capacity.
- On a grouped host — bare-metal `direct` or plugin `delegated` — pass
  `server_group_id`; the broker then best-fits one host inside that group.
  `server_id` is only for ungrouped hosts. A plugin-adapted cluster appears as
  an ordinary group, with `allocation`, `limits`, and
  `largest_allocatable_block` (one apply's max cards, not remaining pool size;
  `null` means unknown — do not invent a number).
- Inventory uses `gpu_add_server` and `gpu_update_server`. There is no MCP
  delete; remove a server in the app or REST, which refuses while it holds
  active leases.
- Assess a group's workspace, environment notes, and data/weight notes before
  claiming. Endpoints inherit the group workspace or override it. Environment
  notes are descriptive only — they are not executed or injected.
- Allocatable capacity is grouped group → server → SKU (`name`, `vram_mib`,
  `total_count`, `available_count`), not a per-free-card menu, and carries no
  telemetry. Load observable on a free card comes from ServerPilot's own
  keepalive hold, which is stopped before the card is handed over, so it is not
  evidence that the card is busy. Telemetry follows the lease:
  `gpu_status(lease_id=…)` returns rolling ten-minute averages for
  `leased_gpus` plus a `lease` summary (`min_memory_free_mib`, `slowest_gpu`)
  for judging whether your own job is using the cards well.
- SSH in, `cd` to the returned `workspace.path`, then apply the CUDA selector.
  That path is a working directory, not a code repository.
- Release immediately if CUDA fails to initialise or the workload does not
  start.
- `no_capacity` means nothing was allocated and nothing was queued. It comes
  back as data, not as an error, and it is not worth retrying in the same turn.

## 🖥️ The desktop app

The desktop app is macOS-only; the CLI and MCP entry points work on Windows
too. The Settings page shows this installation's MCP entry as an absolute path
and the pasteable `mcpServers` JSON. Copy either into Codex, Claude Code, or
Cursor. If the executable is missing, the same panel says so and repeats the
install hint instead of inventing a path.

### macOS

Install the CLI first — that is the backend. Then open the desktop app to
watch state, ownership, and failures. The app does not run its own control
plane.

```bash
open "./ServerPilot.app"
```

There is no browser UI.
Manual reassignment updates the lease and the CUDA selector; it does not
migrate a running process, so the agent has to restart its workload against the
new selector.

## 🛡️ Boundaries and security

- ServerPilot manages the lifecycle of its own occupancy processes and of
  plugin-side allocations. It does not start, stop, migrate, or preempt your
  workloads. Keepalive starts and stops a per-GPU CUDA process on the remote
  host holding about 80% of that card's VRAM, and `gpu_apply` stops it before
  handing the card over. A plugin that declares `apply` / `release` performs the
  matching cluster allocation on request and release.
- Server state comes from fixed collection: a built-in SSH probe, or a local
  plugin's `observe`. The plugin calling contract is four fixed verbs — `info`,
  `observe`, `apply`, `release`. No arbitrary remote command is accepted, and no
  password or private key is provided. See [PLUGINS_zh.md](https://github.com/JinPLu/ServerPilot/blob/master/docs/PLUGINS_zh.md).
- Stale collection, connection errors, unknown processes, and resource conflicts
  all refuse allocation locally. That holds for what collection **reports**: the
  SSH user and the remote `serverpilot-collect` entry point are trusted. A
  replaced or malicious collector that omits compute processes will make a card
  look allocatable.
- The control plane listens on loopback by default. **There is no
  authentication.** `X-ServerPilot-Actor` is an audit label; any local process
  can send it, take the `allocator` role, create endpoints, change keepalive
  policy, claim GPUs, or name another actor and release that actor's leases.
  Local processes under the same user account are not isolated from each other.
  GPU UUID and endpoint are the resource identity boundary.

## 📚 Documentation

In English:

- [Agent operating rules](https://github.com/JinPLu/ServerPilot/blob/master/docs/AGENT_MCP_policy.en.md) — the short contract you
  can paste into an agent's global rules
- [Security](https://github.com/JinPLu/ServerPilot/blob/master/SECURITY.md) · [Contributing](https://github.com/JinPLu/ServerPilot/blob/master/CONTRIBUTING.md) · [Code of conduct](https://github.com/JinPLu/ServerPilot/blob/master/CODE_OF_CONDUCT.md) · [Changelog](https://github.com/JinPLu/ServerPilot/blob/master/CHANGELOG.en.md)

In Chinese:

- [Agent / MCP guide](https://github.com/JinPLu/ServerPilot/blob/master/docs/AGENT_MCP_zh.md)
- [Collector protocol](https://github.com/JinPLu/ServerPilot/blob/master/docs/COLLECTOR_SCRIPT_zh.md)
- [Server plugins](https://github.com/JinPLu/ServerPilot/blob/master/docs/PLUGINS_zh.md)
- [Keepalive and adapters](https://github.com/JinPLu/ServerPilot/blob/master/docs/ADAPTERS_zh.md)
- [Upgrade checklist](https://github.com/JinPLu/ServerPilot/blob/master/docs/UPGRADE_CHECKLIST_zh.md)
- [Implementation and verification status](https://github.com/JinPLu/ServerPilot/blob/master/docs/IMPLEMENTATION_STATUS_zh.md)

Reference documentation is currently written in Chinese, and so are the desktop
app and the descriptions an agent reads over MCP. This README, the changelog,
and the agent operating rules are the English surfaces today.

## License

[MIT](https://github.com/JinPLu/ServerPilot/blob/master/LICENSE)
