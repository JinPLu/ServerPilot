---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-11
---

# Record: Gpu Broker Native Ui Workflow 2026 08 01 — Packaging Failure Split

## Current synthesis

- Subject identity: 桌面打包失败分叉的已验解法与残留不确定这一持续目标
- fix-verified：串行 build→verify 是已验解法；瞬时触发条件未确认，因失败暂存件已删除。
- 若再现，先记录 staged artifact hash 与并发 build PID，这是下一个判别器。

## Debug state

fix-verified. The distinct packaging failure split is resolved on the serialized desktop build-then-verify path. The exact transient trigger is not confirmed.

## Failure boundary and decisive observations

- After review found the root `ServerPilot.app` stale, one build log recorded a PyInstaller extraction failure for embedded `libpython3.12.dylib` with return `-3`.
- An independent GUI build and verify running concurrently exited `0`. Package and PYZ hashes remained stable, and the archive reader decompressed `libpython3.12.dylib` successfully.
- A single serialized desktop build followed by desktop verify exited `0`; the root `ServerPilot.app` was built and verification returned `PASS`.
- Root independently reran verify with `PASS` and directly disassembled the bundled `serverpilot.service` PYZ, confirming `workload_claimed_gpus`, `keepalive_owned_gpus`, `verified_keepalive_gpus`, and `active_workload_leases`.
- Final independent re-review accepted the candidate.

## Causal picture

The evidence supports a transient overlapping one-file extraction/read race rather than a corrupted payload. Stable package/PYZ hashes and successful archive decompression weaken the corrupted-payload explanation, but the exact transient trigger cannot be recovered because the failed staging artifact was deleted. This remains an evidence-supported causal picture, not a stronger confirmed trigger.

## Cause, next discriminator, and verification

- The verified resolution is the serialized desktop build followed by desktop verification; no source, test, desktop-app, implementation-status, or package-configuration change was made.
- If the split recurs, record the staged artifact hash and concurrent build PIDs before cleanup; those observations are the next discriminator.
- No duplicate app, GPU, SSH, or endpoint action occurred. No live system was changed.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 打包失败分叉落盘

记录失败边界与决定性观测、因果图景（瞬时重叠抽取/读取竞争）、已验解法与下一判别器。
