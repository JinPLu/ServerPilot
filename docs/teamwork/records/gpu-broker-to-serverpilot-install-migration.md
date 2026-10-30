---
status: active
superseded-by:
created: 2026-08-12
updated: 2026-08-12
---

# Record: 本机 install/keepalive 集成迁到 ServerPilot

## Current synthesis

- Subject identity: 本机 install/keepalive 集成从旧 gpu-broker 路径迁到 ServerPilot 的整改这一持续目标
- 诊断已确认、整改已执行并验证：19 tests passed，Ruff 与 diff 通过，daemon live/ready，endpoint 2097 keepalive OFF 且两卡 AVAILABLE。
- 边界：当前 enable 行为未测；未授权任何远端 mutation。

## Failure boundary and diagnosis

The local install/keepalive integration was still carrying pre-move state. The real repository is now `<repo>`; the old `gpu-broker` path is absent. The old Codex MCP configuration still referenced `gpu-broker` 1.2.0 and left many legacy MCP processes. The global AGENTS marker was also legacy. This is the recorded local diagnosis; no remote runtime mutation is claimed.

## Corrective work and evidence

- `scripts/install_agent_policy.py` was patched to fail closed during in-place migration of a legacy marker; `tests/test_agent_policy.py` was updated.
- Targeted verification: 19 tests passed; Ruff and diff checks passed.
- Config, AGENTS, and DB were backed up. `serverpilot` 1.4.0 was force-installed and daemon installation was run.
- Codex and Cursor configs were transitioned to `serverpilot-mcp` with `SERVERPILOT_URL` and the GPU keepalive MCP tool. The new policy was installed.
- Checks found no stale config/rule references; installed package source matches the repo; daemon is live/ready.
- Endpoint `2097` reports keepalive configured `OFF` and two GPUs `AVAILABLE`.

## Keepalive verification boundary

Historical keepalive begin/confirm/stop and exact-PID safe-stop behavior were verified read-only. No remote mutation or current enable test was authorized, so current enable behavior remains untested.

Local installation/configuration checks passed. Codex and Cursor must restart for old stdio processes to exit naturally; old tool cleanup can occur after those restarts.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-12 — 整改与验证落盘

记录本机 install/keepalive 集成的迁移诊断、纠正工作、验证结果与 keepalive 验证边界。
