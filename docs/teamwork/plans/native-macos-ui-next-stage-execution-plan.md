---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-29
---

# Plan: 原生 macOS UI 方向的下一阶段可执行计划

## Current synthesis

- Subject identity: 已接受的原生 macOS UI 方向的下一阶段可执行计划这一选定结果
- This document is planning only：UI 代码与 MCP URI 自动登记/协调读均未实现。

## Intended result

Deliver an executable, verifiable next-stage plan for the accepted ServerPilot native UI direction. This document is planning only: no Swift, MCP, migration, build, release, or runtime changes are performed by this transaction. The accepted GUI language/layout contract and the MCP coordination boundary remain the source direction.

## Selected direction and protected contract

- Keep the native SwiftUI/AppKit surface, REST/domain truth boundary, GPU UUID plus endpoint identity, fail-closed behavior, and the root-only `ServerPilot.app`.
- Public GUI uses the accepted concise vocabulary and layout: `服务器 / 使用情况 / 设置`; `空闲`, `任务占用`, `占卡`; `申请 GPU / 释放 GPU`; adaptive master-detail (wide right inspector, narrow full-page detail with return); project is the Usage default view and task switching is allowed.
- Usage shows current task plus GPU; empty state is one sentence plus `申请 GPU`. GUI does not show Agent pages, links, coordination URIs, chat, inbox, unread state, or message composers. Project + task is the only stable work identity.
- Settings exposes only local service address, data-update interval, and version. Intervals are 5/10/30 seconds and the persisted interval must drive timeout judgment. Add-server and Claim-GPU forms show fields/buttons in normal state; explanatory copy is reserved for field errors.
- Preserve accepted detail order and actions, concrete error copy, confirmation copy, accessibility/Reduce Motion, and operator density. Do not treat accepted wording as permission to implement it in this plan transaction.

## Ordered work

### 0. Protect the dirty worktree and app identity

- Owner: Root-directed implementation owner.
- Target: repository state, existing rename work, root `ServerPilot.app`.
- Action: record a read-only baseline of the dirty worktree and existing rename; preserve every user change; do not reset, clean, branch, stash, or overwrite unrelated work. Keep exactly one root `ServerPilot.app`; do not create app copies in build/dist/Applications or elsewhere.
- Dependencies: none.
- Verification: baseline paths and intended edits are recorded before mutation; after every desktop change run root build/verify and confirm only the root app is used.
- Stop: any ambiguity about ownership of a dirty file, rename, or app path stops execution and returns to Root.

### 1. Establish fact gates before implementation

- Owner: Root-directed fact-gate owner.
- Target: stdio MCP launch, actor identity, history sources, collection settings.
- Action and evidence:
  1. Verify stdio MCP inherits `CODEX_THREAD_ID` from the launching process and that the observed value, not an invented fallback, is the task identity input.
  2. Measure and document default-actor override risk; no implicit actor replacement may silently change registration identity.
  3. Identify historical source records needed for migration and preserve them; history is evidence, not disposable input.
  4. Verify the collection interval is a real persisted write contract and that timeout judgment reads the same effective 5/10/30-second value.
- Dependencies: step 0 baseline.
- Verification: each gate has direct observed evidence, a named source path, and a pass/fail record before steps 2–4 begin.
- Stop: any gate is unverifiable, contradicts the accepted contract, or would require inventing an actor, history, or timeout value.

### 2. MCP coordination/identity track

- Owner: MCP/API implementation owner.
- Target: actor persistence and registration path, migrations, coordination read surface, tests.
- Ordered outcomes:
  1. Add nullable `coordination_uri` to Actor through an additive migration; preserve existing rows and rollback data.
  2. Enforce strict UUID validation at the identity boundary.
  3. Make internal-header registration idempotent; repeated registration of the same actor/task must not duplicate or overwrite unrelated identity.
  4. Ensure coordination reads return the required agents, leases, and consumers relationship data, including Actor plus URI where the MCP contract exposes it.
  5. Keep coordination metadata out of authorization: URI/actor discovery never grants claim, lease, start, stop, or remote-control authority.
  6. Add complete tests for additive migration, nullable values, strict UUID rejection, idempotent internal-header registration, CODEX_THREAD_ID inheritance, default-actor override protection, agents/leases/consumers reads, and the non-authorization boundary.
- Dependencies: fact gates 0–1; migration source inventory before schema mutation.
- Verification: focused migration/API/MCP tests plus a clean readback of persisted rows and repeated-registration behavior; no live GPU or remote workload is required.

### 3. GUI track: serial copy/layout sequence

- Owner: native UI implementation owner; Root retains integration ownership.
- Target: SwiftUI/AppKit desktop surface. Keep `ServerPilot.swift` as the single composition owner. `Usage` and `Store` may proceed in limited, explicitly bounded parallel work only when their inputs/outputs do not overlap.
- Do not refactor the existing enum, router, Table, projection, or detail architecture as part of wording/layout work.
- Execute in this order:
  1. Status vocabulary: apply `空闲`, `任务占用`, `占卡`, concrete connection/collection errors and accepted normal/error/empty/confirmation rules.
  2. Navigation and server table: `服务器 / 使用情况 / 设置`, summary/model behavior, filters, columns, neutral numbers, meter color, single sort arrow, SSH error treatment.
  3. Detail and history: compact GPU list, one-line metrics, accepted action/menu placement, expanded history, adaptive inspector/full-page return behavior.
  4. Usage: project default, current task + GPU summary, task switching, one-sentence empty state plus `申请 GPU`; no Agent UI.
  5. Forms: SSH command-only server add; Claim GPU fields only project/task/GPU count/server; normal fields/buttons, field-error explanations only.
  6. Settings last: local service address, 5/10/30-second update interval, version; remove operation identifiers and internal terms; ensure interval and timeout copy/behavior share one contract.
- Dependencies: step 1 fact gates; GUI steps are serial in the order above. Usage/Store parallelism is limited to non-overlapping bounded work.
- Verification: per-step focused Swift tests/readback and deterministic UI evidence; no production data or live GPU control. Any requested architecture refactor or Agent/URI/message UI is out of scope and stops for replanning.

### 4. Swift core, UI, accessibility, and evidence gates

- Owner: native UI implementation owner with Root integration gate.
- Target: testable Swift core/UI, accessibility surfaces, screenshots and app bundle.
- Action: preserve MainActor/state truth and selection stability; validate keyboard/VoiceOver, Reduce Motion, light/dark/contrast, adaptive layout, concrete failure/empty/confirmation states, and the accepted no-Agent GUI boundary.
- Evidence: capture screenshots at 1024, 1280, and 1440 widths; run the root `zsh desktop/build-macos-app.sh` then `zsh desktop/verify-macos-app.sh`; retain build/test output and screenshot evidence tied to the accepted states.
- Dependencies: ordered GUI track; all protected behavior must have direct evidence before release.
- Stop: a screenshot-only result, missing accessibility evidence, wrong app path, or unverified build cannot pass the gate.

### 5. Release, rollback, and stop/replan

- Owner: Root release owner.
- Target: root app, migration/data changes, evidence bundle.
- Action: release only after facts, MCP tests, GUI checks, screenshots, and root build/verify are green. Keep migration rollback and pre-change worktree evidence. Do not claim UI or MCP completion from this plan.
- Rollback: if any gate regresses, restore the affected additive migration/UI change using the recorded preimage and return to the last verified root app; do not delete historical records or force-stop unrelated workloads.
- Stop/replan conditions: accepted direction changes; protected dirty worktree/rename/root app identity is threatened; owner overlap appears; fact gates fail; migration is not additive or idempotent; interval write and timeout judgment diverge; architecture refactor becomes necessary; GUI exposes Agent/URI/message affordances; tests/build/screenshots or rollback evidence are unavailable.
- Completion signal for the next stage: all gates have direct evidence, root build/verify passes, the root app is unique, and the next-stage result explicitly distinguishes implemented from deferred work.

## Dependency and verification relationship

Step 0 protects every later mutation. Step 1 gates both tracks. Step 2 can proceed independently of the GUI only after the fact gates and migration inventory. Step 3 is serial by surface; bounded Usage/Store work may overlap only without shared ownership. Step 4 consumes both tracks and is required before step 5. Step 5 is a release decision, not an implementation claim.

## Current status and handoff

This is a formal plan only. The accepted decision is recorded, the plan is now the execution design, and UI code plus MCP URI auto-registration/coordination-read implementation remain unimplemented for the next execution stage.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 计划落盘

记录已接受 GUI 语言/布局契约与 MCP 协调边界下的有序工作单元与验证门。

### 2026-08-29 — slug 从旧 case 标题改为按 identity 派生（撞车修复）

本文件此前落在 `docs/teamwork/plans/gpu-broker-native-ui-workflow.md`，与 `docs/teamwork/records/gpu-broker-native-ui-workflow.md` 同 slug。两侧 identity 并不相同：records 侧的持续目标是「原生 macOS GUI 公开 copy 与语言层级收束」，且该结果已达成；本文件的选定结果是「已接受的原生 macOS UI 方向的下一阶段可执行计划」，其有序工作单元（步骤 0–5）尚未执行。因此不合并，改为按各自 identity 派生 slug：本文件迁至 `plans/native-macos-ui-next-stage-execution-plan.md`。同时去掉 H1 里的 `2026 08 01` 日期碎片。正文未改。
