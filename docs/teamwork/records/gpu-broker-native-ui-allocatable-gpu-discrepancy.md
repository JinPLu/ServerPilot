---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-11
---

# Record: Gpu Broker Native Ui Workflow 2026 08 01 — Allocatable GPU Discrepancy

## Current synthesis

- Subject identity: 「6 张 A800 可见但只有 4 张可分配」的原因定位与已验修复这一持续目标
- fix-verified：原因是 compact MCP/coordination 漏掉内部 keepalive 归属，窄修后 6 passed / 115 passed / 54 passed。
- 边界：未对真实 181 进程执行任何 stop mutation，那仍需单独的人工授权与 approval_ref。

## Debug state

fix-verified. The bounded discrepancy was that six physical A800 GPUs were observed while only four were allocatable.

## Failure boundary and decisive observations

- Direct live status reported total 6 and available 4.
- After the GUI start/stop cycle, 203 had 4 AVAILABLE and 181 had 2 CONFLICT.
- Full state showed each 181 GPU as LEGACY_STOP_REQUIRED with a real process using about 25.6 GiB and variable high utilization. Fail-closed behavior makes them non-allocatable. Stopping them requires distinct current human authorization plus approval_ref; no such stop mutation was performed.

## Supported cause

The cause was not availability calculation. Compact MCP/coordination omitted internal keepalive ownership from visible workload lease counts and compact per-GPU data, making claimed=2 and active_leases=0 misleading.

## Narrow corrective change

Compact MCP now carries workload_claimed_gpus, keepalive_owned_gpus, verified_keepalive_gpus, and safe keepalive reason/health. Occupied, HELD, and CONFLICT GPUs remain unavailable.

## Verification

- Local focused test: 6 passed.
- Agent test suite: 115 passed.
- Independent API/MCP/keepalive suite: 54 passed.
- Ruff and git diff --check passed.
- The live global package and daemon were updated, and a fresh serverpilot-mcp process returned the expected fields.
- GUI start -> HELD -> stop -> 4/4 available was independently tested. The GUI change fixed only a false UI orphan-cleanup prompt.

## Scope and remaining authority

No stop mutation was performed on the real 181 processes. Any future stop of those processes remains a separate human-authorized action requiring approval_ref.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 定位与修复验证落盘

记录失败边界与决定性观测、支持的原因、窄修改动、验证结果与剩余授权边界。
