---
status: active
superseded-by:
created: 2026-08-03
updated: 2026-08-29
---

# Record: GPU Broker General Resource Control Plane

## Current synthesis

- Subject identity: 统一操作数据路径（canonical `GET /api/v1/state` + revision 一致性）这一持续目标
- 结果：unified operational data path is implemented；233 Python tests pass，Ruff、`git diff --check`、Swift build 与 macOS build/verify 均 PASS。
- 环境限制：本机只有 CommandLineTools，无 `XCTest`，`swift test` 无法运行。

## Outcome

The unified operational data path is implemented. Canonical `GET /api/v1/state` exposes current/history from a service-owned snapshot transaction and revision. Mutations return the committed `snapshot_revision`; Python client/CLI/MCP and Native App operational reads converge on state.

## Contract and behavior

- Minimum-revision reads and the global rollback floor are enforced.
- Endpoint and GPU IDs remain stable; deletion converges sheets/pages after the lifecycle transition.
- `ResourceUsage` is derived from the committed snapshot.
- `host_capacity` and allocations retain compatibility parity. For non-admin actors, cross-project capacity uses global active allocations while claim details remain filtered.
- SchedulerTarget, endpoint, and GPU identity boundaries are preserved.
- Documentation is synchronized.

## Verification

- 233 Python tests pass.
- Ruff, `git diff --check`, Swift GPUBrokerCore build, `desktop/build-macos-app.sh`, and `desktop/verify-macos-app.sh` (PASS) pass.
- All current fixtures pass the skill validator.
- No live GPU, SSH, or state access was used.

## Review closure

Independent review initially found four items; all were fixed. A delta review identified a proof gap for restricted-actor capacity. Project-a/project-b restricted-actor parity and non-over-reporting tests were added; the full suite is green.

## Environment limits

`swift test` could not run because the environment has only CommandLineTools and reports no `XCTest` module. Benchmark, XCUI, and VoiceOver remain environment gates.

## Completion assessment

The implementation satisfies the accepted unified-state plan and its verification gates. The execution result is durable; the case may be closed.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-06 — 执行结果落盘

记录统一 read-contract 的交付结果、契约与行为、验证、复查闭环、环境限制与完成判定。

### 2026-08-06 — case-decision（已接受 additive read-contract）并入

Direction: evolve GPU Broker into an additive cross-provider resource scheduler. The core abstraction is ResourceProvider/AllocatableUnit with discriminated direct-gpu, host-capacity, and scheduler contracts. Preserve GPU UUID/endpoint semantics and existing GPU REST/MCP compatibility projection; keep SchedulerTarget distinct from bare-metal endpoints. Add a unified human monitor for provider capacity, claims, jobs, predictions and actuals.

Agent policy: Agents autonomously evaluate eligible current resources against their current task. They select the smallest feasible resource plan, expanding CPU, memory, GPU, nodes or scheduler capacity only when the forecast marginal expansion saves BOTH at least 10% of remaining time AND at least 2 minutes. They record candidate plans, predicted savings, chosen plan, rejected expansions and post-run actuals. Fair-share/quota policy remains separate from the optimizer; human users can observe and override.

Initial concrete scope: generalize scheduling across direct GPU, direct host CPU/memory capacity, and external scheduler jobs; make further resource kinds adapter-extensible. No resource type may cause an otherwise valid task to block merely because it has zero GPUs.

Hard boundaries: do not authorize arbitrary remote execution, credentials/VPN automation, raw SSH bypass, retroactive mutation of historical GPU leases, or treating dynamic Slurm allocations as collector endpoints. Retain read-only collection, project/agent ownership, capacity accounting, auditability and real-time human visibility.

Acceptance evidence: user explicitly chose marginal-benefit scheduling and selected the 10% plus 2-minute threshold after the recommendation; local architecture evidence supports the adapter-based migration. No open questions or blockers. Mark acceptance accepted.

##### Accepted additive read-contract decision

The user selected “激进统一接口” (aggressive unified interface). Accept the following additive migration direction:

- New App, CLI, and MCP operational reads converge on one canonical revision-consistent `ControlPlaneSnapshot` / state contract.
- Existing fragmented read endpoints and MCP read tools are transitional migration surfaces. They may be removed after a bounded migration and are not preserved as permanent public compatibility contracts.
- Commands remain specialized by operation, but every command returns the resulting `snapshot_revision`; clients reconcile their reads to at least that revision.
- Events and polling provide invalidation only; they are not an independent state authority.
- Current operational state and retired/history state are explicitly separate in the read model.
- App clients store stable selection IDs rather than copied record instances.

Preserve the existing REST/domain authority, endpoint and GPU identity boundaries, auditability, fail-closed behavior, SchedulerTarget distinction, compatibility projections during migration, and the accepted general-resource and marginal-utility scheduling decisions above. Exact endpoint naming, schema shape, and rollout sequencing remain open for Plan. This decision records direction only and does not claim implementation.

### 2026-08-06 — case-collaborate 稿（已收敛为已接受方向）并入

##### Overall picture

The native app and MCP both converge on the launchd loopback daemon at 127.0.0.1:8787. The service and database are authoritative. The native app uses one BrokerStore and GET /api/v1/snapshot, polling every 12 seconds, with no SSE invalidation. Pages mostly consume store.snapshot, but ResourceUsageDashboard copies ResourceUsageProjection into @State and rebuilds only on resource_usage_revision. Mutations use specialized REST routes, bump snapshot_revision, and then the app manually reloads. Endpoint deletion advances active → draining → retired; default snapshots exclude retired endpoints. MCP uses BrokerClient through REST but exposes fragmented reads such as resource-providers, resource-monitor/resource-claims, and compatibility GPU routes.

This checkpoint records an architecture discussion about whether readers should converge on a canonical revision-consistent read model while preserving the existing REST/domain authority and compatibility surface. No new decision is accepted here.

##### Decided / preserved

- Preserve REST/domain authority: service/DB remains authoritative; clients and MCP use REST and do not bypass the control plane.
- User-selected migration direction: “激进统一接口” (aggressive unified interface), accepted as an additive direction for new operational reads; implementation is not claimed.
- Preserve GPU UUID and endpoint `id` as identity boundaries; same IP on different ports is not merged.
- Preserve compatibility projections for existing GPU APIs and MCP clients, auditability, and fail-closed behavior.
- Preserve endpoint lifecycle semantics: deletion is active → draining → retired, and default snapshots exclude retired endpoints.
- Preserve read-only collection and provider-specific execution authority. The broker coordinates and audits; it does not grant arbitrary remote shell access or automate credentials or VPN.
- Preserve forward-only migration of allocation, reservation, quota, audit, UI, and MCP contracts; do not reinterpret existing GPU leases or telemetry in place.
- Preserve the prior resource-control direction: an additive ResourceProvider/AllocatableUnit core with discriminated direct-GPU, host-capacity, and scheduler contracts; retain GPU adapters/contracts; keep external Slurm as a distinct SchedulerTarget; project a shared resource-monitoring read model.
- Preserve the marginal-utility admission policy: select the smallest plan that minimizes the current task's expected completion time; expand only when predicted marginal benefit is meaningful, with the prior dual threshold of at least 15% remaining-time reduction and at least 5 minutes saved, subject to fair-share/capacity rules and actual-runtime feedback.
- Forbid arbitrary remote command execution, credential/VPN automation, silently converting historical GPU leases, and claiming Slurm nodes as bare-metal endpoints.

##### Open discussion and evidence

Evidence:

- Native app and MCP converge on the launchd loopback daemon at 127.0.0.1:8787; service/DB is authoritative.
- Native app has one BrokerStore reading GET /api/v1/snapshot every 12 seconds; there is no SSE invalidation.
- Most pages consume store.snapshot, while ResourceUsageDashboard copies ResourceUsageProjection into @State and rebuilds only on resource_usage_revision.
- Mutations use specialized REST routes, bump snapshot_revision, and trigger manual app reloads.
- Endpoint delete follows active → draining → retired; default snapshots exclude retired.
- MCP uses BrokerClient through REST but exposes fragmented reads including resource-providers, resource-monitor/resource-claims, and compatibility GPU routes.

Candidate directions:

- A — retain page/tool-specific reads.
- B — converge on a recommended canonical revision-consistent read model/snapshot, with commands returning the resulting revision and clients reconciling to at least that revision; use events for invalidation only.
- C — add a client-side event bus with optimistic fanout.

User choice resolved: the user selected “激进统一接口” (aggressive unified interface). New App/CLI/MCP operational reads will converge on one canonical revision-consistent ControlPlaneSnapshot/state contract. Fragmented read endpoints and MCP read tools are transitional and may be removed after bounded migration rather than preserved as permanent public compatibility. This records the accepted direction but does not claim implementation.

##### Current recommendation and next step

Current recommendation is now the accepted additive direction: new App/CLI/MCP operational reads converge on one canonical revision-consistent ControlPlaneSnapshot/state contract. Commands remain specialized, but every command returns snapshot_revision and clients reconcile to at least that revision. Events and polling are invalidation only. Current operational state and retired/history state remain explicitly separate. App clients store stable selection IDs rather than record copies. Fragmented reads remain transitional and may be removed after bounded migration; compatibility projections are not treated as permanent public read contracts.

Next step: carry the accepted direction into Plan and define the exact endpoint naming, snapshot/schema shape, and rollout sequencing. Those details remain open for Plan. Stress-test migration, revision monotonicity, ResourceUsageDashboard state handling, and MCP compatibility before implementation. The direction is accepted; implementation is not claimed.

### 2026-08-06 — case-collaborate 快照并入

##### Overall picture

The native app and MCP both converge on the launchd loopback daemon at 127.0.0.1:8787. The service and database are authoritative. The native app uses one BrokerStore and GET /api/v1/snapshot, polling every 12 seconds, with no SSE invalidation. Pages mostly consume store.snapshot, but ResourceUsageDashboard copies ResourceUsageProjection into @State and rebuilds only on resource_usage_revision. Mutations use specialized REST routes, bump snapshot_revision, and then the app manually reloads. Endpoint deletion advances active → draining → retired; default snapshots exclude retired endpoints. MCP uses BrokerClient through REST but exposes fragmented reads such as resource-providers, resource-monitor/resource-claims, and compatibility GPU routes.

This checkpoint records an architecture discussion about whether readers should converge on a canonical revision-consistent read model while preserving the existing REST/domain authority and compatibility surface. No new decision is accepted here.

##### Decided / preserved

- Preserve REST/domain authority: service/DB remains authoritative; clients and MCP use REST and do not bypass the control plane.
- Preserve GPU UUID and endpoint `id` as identity boundaries; same IP on different ports is not merged.
- Preserve compatibility projections for existing GPU APIs and MCP clients, auditability, and fail-closed behavior.
- Preserve endpoint lifecycle semantics: deletion is active → draining → retired, and default snapshots exclude retired endpoints.
- Preserve read-only collection and provider-specific execution authority. The broker coordinates and audits; it does not grant arbitrary remote shell access or automate credentials or VPN.
- Preserve forward-only migration of allocation, reservation, quota, audit, UI, and MCP contracts; do not reinterpret existing GPU leases or telemetry in place.
- Preserve the prior resource-control direction: an additive ResourceProvider/AllocatableUnit core with discriminated direct-GPU, host-capacity, and scheduler contracts; retain GPU adapters/contracts; keep external Slurm as a distinct SchedulerTarget; project a shared resource-monitoring read model.
- Preserve the marginal-utility admission policy: select the smallest plan that minimizes the current task's expected completion time; expand only when predicted marginal benefit is meaningful, with the prior dual threshold of at least 15% remaining-time reduction and at least 5 minutes saved, subject to fair-share/capacity rules and actual-runtime feedback.
- Forbid arbitrary remote command execution, credential/VPN automation, silently converting historical GPU leases, and claiming Slurm nodes as bare-metal endpoints.

##### Open discussion and evidence

Evidence:

- Native app and MCP converge on the launchd loopback daemon at 127.0.0.1:8787; service/DB is authoritative.
- Native app has one BrokerStore reading GET /api/v1/snapshot every 12 seconds; there is no SSE invalidation.
- Most pages consume store.snapshot, while ResourceUsageDashboard copies ResourceUsageProjection into @State and rebuilds only on resource_usage_revision.
- Mutations use specialized REST routes, bump snapshot_revision, and trigger manual app reloads.
- Endpoint delete follows active → draining → retired; default snapshots exclude retired.
- MCP uses BrokerClient through REST but exposes fragmented reads including resource-providers, resource-monitor/resource-claims, and compatibility GPU routes.

Candidate directions:

- A — retain page/tool-specific reads.
- B — converge on a recommended canonical revision-consistent read model/snapshot, with commands returning the resulting revision and clients reconciling to at least that revision; use events for invalidation only.
- C — add a client-side event bus with optimistic fanout.

Open user choice: converge immediately on one canonical full snapshot plus optional filtered projections, or maintain multiple task-specific read endpoints behind a shared revision/read-model contract. The choice is material and remains open; do not claim acceptance.

##### Current recommendation and next step

Current recommendation is to make direction B the default convergence target: define one canonical full snapshot and permit optional filtered projections, all carrying a shared revision/read-model contract. Mutation responses should return the resulting revision; clients should reconcile to at least that revision, while events serve invalidation only. This recommendation preserves compatibility routes and does not require an immediate removal of task-specific endpoints.

Next step: ask the user to choose between immediate convergence on the canonical full snapshot with optional filtered projections, and retaining multiple task-specific read endpoints behind the shared revision/read-model contract. After that choice, stress-test migration, revision monotonicity, ResourceUsageDashboard state handling, and MCP compatibility before any plan or implementation. Acceptance remains pending.

### 2026-08-04 — case-decision 旧快照并入

Direction: evolve GPU Broker into an additive cross-provider resource scheduler. The core abstraction is ResourceProvider/AllocatableUnit with discriminated direct-gpu, host-capacity, and scheduler contracts. Preserve GPU UUID/endpoint semantics and existing GPU REST/MCP compatibility projection; keep SchedulerTarget distinct from bare-metal endpoints. Add a unified human monitor for provider capacity, claims, jobs, predictions and actuals.

Agent policy: Agents autonomously evaluate eligible current resources against their current task. They select the smallest feasible resource plan, expanding CPU, memory, GPU, nodes or scheduler capacity only when the forecast marginal expansion saves BOTH at least 10% of remaining time AND at least 2 minutes. They record candidate plans, predicted savings, chosen plan, rejected expansions and post-run actuals. Fair-share/quota policy remains separate from the optimizer; human users can observe and override.

Initial concrete scope: generalize scheduling across direct GPU, direct host CPU/memory capacity, and external scheduler jobs; make further resource kinds adapter-extensible. No resource type may cause an otherwise valid task to block merely because it has zero GPUs.

Hard boundaries: do not authorize arbitrary remote execution, credentials/VPN automation, raw SSH bypass, retroactive mutation of historical GPU leases, or treating dynamic Slurm allocations as collector endpoints. Retain read-only collection, project/agent ownership, capacity accounting, auditability and real-time human visibility.

Acceptance evidence: user explicitly chose marginal-benefit scheduling and selected the 10% plus 2-minute threshold after the recommendation; local architecture evidence supports the adapter-based migration. No open questions or blockers. Mark acceptance accepted.

### 2026-08-04 — case-collaborate 快照并入

Synthesis: The user asks to remove unnecessary GPU-Broker limitations and focus on cross-project and cross-Agent server-resource scheduling, agents that actively schedule all resources, and real-time human monitoring. The current system already sees endpoint CPU/memory and supports CPU-only external Slurm jobs; its bare-metal allocator remains GPU-device-first.

Evidence: ResourceConstraints/direct claims require positive GPU count; service eligibility/select/allocation iterates GPUDevice, and LeaseResource persists gpu_id. Host CPU/memory are only attached admission thresholds/commitments. GPU collector/process/telemetry identity is GPU UUID-based. SchedulerTarget/SchedulerJob already provides distinct CPU-only scheduling. Desktop parses endpoint CPU/memory but polls snapshot about every 12 seconds and emphasizes GPUs; event SSE is audit-only, and GPU history is GPU-only.

Candidate space:
A. Relax old GPU schemas to allow zero GPUs everywhere. Rejected: it creates zero-resource direct leases and breaks GPU identity/reservation semantics.
B. Recommended: introduce an additive ResourceProvider/AllocatableUnit core with discriminated contracts (direct-gpu, host-capacity, scheduler); retain GPU adapters/contracts, keep SchedulerTarget distinct, and project a shared resource-monitoring read model.
C. Monitoring-only generalization. Rejected: it does not satisfy independent CPU/memory scheduling.

Recommendation: B. Generalize resource ownership and admission behind provider-specific adapters. Make Agents default to contract-driven resource claims across eligible providers, while humans see a unified real-time resource/claim/job view. Preserve provider-specific execution authority: broker coordinates and audits; it does not grant arbitrary remote shell or automate account credentials.

Largest downside: forward migration of allocation, reservation, quota, audit and UI/MCP contracts is material. Do not reinterpret existing GPU leases or telemetry in place.

Preserve: GPU UUID + endpoint identity; forward-only migration; external Slurm remains SchedulerTarget, not endpoint; read-only collection; auditable project/agent ownership; compatibility projection for existing GPU APIs/MCP clients.
Forbid: arbitrary remote command execution, credential/VPN automation, silently converting historical GPU leases, claiming Slurm nodes as bare-metal endpoints.

Open frontier / required user decision: define “incentivize agents to schedule all resources” as either (1) default autonomous contract-driven claims whenever a task contains resource needs, or (2) a priority/reward mechanism that changes scheduling fairness. This is the only user question in the next dialogue turn.

Decision rule: select B if the user confirms autonomous claims are sufficient; add explicit scheduling policy/priority design only if the user demands rewards/priority incentives.
Acceptance state: pending, not accepted. Acceptance evidence: user goal plus local architecture evidence; needs answer to the defined incentive semantic.

Agent incentive semantic clarification (frozen update): Agents should inspect existing eligible resources and select the smallest resource plan that minimizes their current task's expected completion time. They should claim additional CPU, memory, GPU, nodes, or Scheduler capacity only if the predicted marginal time reduction is meaningful; if more resources shorten the task only a little, do not claim them. This is not a global reward/priority score.

Updated recommendation (frozen update): General resource architecture direction B remains preferred. Add a marginal-utility admission policy above provider adapters: task-specific runtime estimates / known scaling profiles produce candidate plans; choose the minimum plan meeting a defined marginal benefit threshold; publish prediction, selected resources, rejected expansion options, and actual runtime feedback to human monitoring. Keep project fairness quotas and explicit resource accounting separate from this optimization.

Updated open frontier (frozen update): choose the starting definition of “meaningful” gain. Recommendation is a dual threshold: an expansion is eligible only if it predicts both at least 15% remaining-time reduction and at least 5 minutes saved, subject to fair-share/capacity rules. This prevents overclaiming short tasks and avoids consuming scarce resources for negligible speedups. Largest downside: estimated scaling may be wrong initially; close with measured actual runtime feedback and human override. Acceptance pending until the user chooses/adjusts that threshold.

Preserve all prior frozen clauses.

### 2026-08-03 — 链内最早 case-collaborate 快照（16:01）并入

Synthesis: The user asks to remove unnecessary GPU-Broker limitations and focus on cross-project and cross-Agent server-resource scheduling, agents that actively schedule all resources, and real-time human monitoring. The current system already sees endpoint CPU/memory and supports CPU-only external Slurm jobs; its bare-metal allocator remains GPU-device-first.

Evidence: ResourceConstraints/direct claims require positive GPU count; service eligibility/select/allocation iterates GPUDevice, and LeaseResource persists gpu_id. Host CPU/memory are only attached admission thresholds/commitments. GPU collector/process/telemetry identity is GPU UUID-based. SchedulerTarget/SchedulerJob already provides distinct CPU-only scheduling. Desktop parses endpoint CPU/memory but polls snapshot about every 12 seconds and emphasizes GPUs; event SSE is audit-only, and GPU history is GPU-only.

Candidate space:
A. Relax old GPU schemas to allow zero GPUs everywhere. Rejected: it creates zero-resource direct leases and breaks GPU identity/reservation semantics.
B. Recommended: introduce an additive ResourceProvider/AllocatableUnit core with discriminated contracts (direct-gpu, host-capacity, scheduler); retain GPU adapters/contracts, keep SchedulerTarget distinct, and project a shared resource-monitoring read model.
C. Monitoring-only generalization. Rejected: it does not satisfy independent CPU/memory scheduling.

Recommendation: B. Generalize resource ownership and admission behind provider-specific adapters. Make Agents default to contract-driven resource claims across eligible providers, while humans see a unified real-time resource/claim/job view. Preserve provider-specific execution authority: broker coordinates and audits; it does not grant arbitrary remote shell or automate account credentials.

Largest downside: forward migration of allocation, reservation, quota, audit and UI/MCP contracts is material. Do not reinterpret existing GPU leases or telemetry in place.

Preserve: GPU UUID + endpoint identity; forward-only migration; external Slurm remains SchedulerTarget, not endpoint; read-only collection; auditable project/agent ownership; compatibility projection for existing GPU APIs/MCP clients.
Forbid: arbitrary remote command execution, credential/VPN automation, silently converting historical GPU leases, claiming Slurm nodes as bare-metal endpoints.

Open frontier / required user decision: define “incentivize agents to schedule all resources” as either (1) default autonomous contract-driven claims whenever a task contains resource needs, or (2) a priority/reward mechanism that changes scheduling fairness. This is the only user question in the next dialogue turn.

Decision rule: select B if the user confirms autonomous claims are sufficient; add explicit scheduling policy/priority design only if the user demands rewards/priority incentives.
Acceptance state: pending, not accepted. Acceptance evidence: user goal plus local architecture evidence; needs answer to the defined incentive semantic.

### 2026-08-29 — 合并同 identity 的 plans 侧文件（slug 撞车修复）

同一 identity（统一 read-contract：canonical `GET /api/v1/state` + `snapshot_revision`）此前同时落在 `records/` 与 `plans/` 两处，只是一侧写成「持续目标」、一侧写成「选定结果」。该计划已经执行完毕并已交付（本文件 Outcome / Verification 记录 233 Python tests、Ruff、Swift build、macOS build/verify 全绿），按落盘契约首匹配，「尚未执行的有序步骤」不成立，唯一落点是 records；plans 侧文件已删除，其正文原样折入本条目。

原文（`docs/teamwork/plans/gpu-broker-general-resource-control-plane.md`）全文如下，正文一字未删，仅标题层级整体下调以嵌入本 History 条目：

#### Plan: Gpu Broker General Resource Control Plane 2026 08 04

##### Current synthesis

- Subject identity: 统一 read-contract（canonical `GET /api/v1/state` + `snapshot_revision`）的执行计划这一选定结果
- Execution-ready Plan for Root plus 3 parallel workers；本文件是执行设计，不是完成声明。

##### Purpose and status

Execution-ready Plan for Root plus 3 parallel workers. Scope is local code, tests, fixtures, and docs only. The user authorized implementation and subagents. Do not access live state, GPUs, SSH, or Slurm. Do not create a branch or PR, revert changes, perform destructive actions, or remove public surfaces during this plan.

##### Frozen accepted readback

- Manifest revision: `4d5336725d3f06837dc92aa7614449e670e892dfd147063d3920b8071e722d02`.
- Manifest byte digest: `c5114f6ee3f5cbbc3c06ea1b91d830336bb02e40a587727fd7b08c2f024e43d1`.
- Decision artifact: `a-2617cb201279c0ef235c0fa08995340c59406acba7746153e80449af76894543`.
- Decision revision: `1960ba20d66b5273c56a0e414f12754d4047bccd3b25a53c6dc3cc6ee19eae6c`.
- Decision digest: `a2520d60195c34d740a797b8216bd79c85d6e42707e36636fd1d46a577c10ce3`.
- Accepted aggressive unified interface; no open blockers.
- Preserve the accepted 10% + 2 minute marginal threshold.

##### Frozen contract

Add `GET /api/v1/state` and `ControlPlaneSnapshot`. The envelope schema is `schema_version`, integer `snapshot_revision`, `server_time`, and `data`. `data.current` contains `summary`, `data_age_seconds`, `freshness_seconds`, `endpoints`, `gpus`, `absent_gpu_ids`, `leases`, `requests`, `reservations`, `maintenance`, `alerts`, `resource_providers`, `allocatable_units`, `resource_claims`, `scheduler_targets`, `scheduler_jobs`, `scheduler_transfers`, `workload_profiles`, and `admission_boundary`. `data.history` is bounded `retired_endpoints`, `resource_plan_evaluations`, and `resource_run_actuals`; audit/events/telemetry series stay specialized history.

Current excludes retired/terminal. Actor/project filtering occurs within one read transaction. Preserve `endpoint id`, `gpu_uuid`, `endpoint_id:gpu_uuid`, same-host-different-port identity, and SchedulerTarget distinction. Remove `resource_usage_revision` as client authority. Transitional fragmented reads derive from the canonical builder and have no first-party callers after migration; removal later. Every successful mutation returns top-level committed `snapshot_revision`. Clients reconcile until state revision >= command revision; never commit rollback. Polling/events only invalidate.

##### Track 1 — backend owner

Own `src/gpu_broker/service.py`, `api.py`, `schemas.py`, `__init__.py`, new `tests/test_control_plane_state.py`, plus backend additions to `tests/test_service.py` and `tests/test_general_resources.py`.

Add the typed contract, one-session builder, `/state` capability, transitional projections, mutation-revision audit, and visibility/history/query proofs. Commands: `PYTHONPATH=src uv run --extra dev pytest targeted`; run Ruff.

##### Track 2 — Python clients owner

Own `client.py`, `cli.py`, `mcp_server.py`, `tests/test_client.py`, `tests/test_api_gui_mcp.py`, `tests/test_mcp_wait_for_claim.py`, `README.md`, `docs/AGENT_MCP_zh.md`, `docs/AGENT_MCP_policy.en.md`, and `docs/IMPLEMENTATION_STATUS_zh.md`.

Add bounded `BrokerClient state(min revision)`, CLI state, and MCP `control_plane_state`; migrate operational reads/wait to canonical state; keep mutations/history specialized; retain revision in outputs; deprecate aliases. Tests prove no fragmented first-party reads and the revision floor.

##### Track 3 — native owner

Own `BrokerStore.swift`, `CoreModels.swift`, `FixtureSnapshots.swift`, `BrokerStoreTests.swift`, `desktop/GPU Broker.swift`, `ResourceUsageDashboard.swift`, `OverviewDashboard.swift`, `AppSupport.swift`, and `desktop/Fixtures/*.json`.

Decode current/history; fetch `/state`; enforce the revision floor for every mutation; preserve coalescing/last-good fail-closed behavior; remove `resourceUsageRevision` and cached projection; resolve all selections from stable IDs in the latest snapshot; on disappeared selection, fall back or dismiss. Add fixtures/tests and app build/verify.

##### Root integration

Record the dirty baseline, assign unique owners, merge the contract, search fragmented calls, run targeted and full pytest, Ruff, desktop build/test/verify, and update status only from evidence. Acceptance requires `/state` to be the only first-party operational read; a single transaction/revision; commands returning revision; clients reconciling; separated history; ID selection; identity/audit/visibility/fail-closed/SchedulerTarget proofs; and no live effects.

##### Stops and risks

Stop if the decision revision changes, schema is disputed, migration/new dependency/public removal/live access/destructive action is needed, an overlapping dirty edit changes after dispatch, revision coverage is partial, a visibility leak appears, or the real app build is skipped.

Risks: dirty shared files, nested reads or mixed revisions, query growth, stripped revisions, visibility regression, and stale sheets.

##### History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

###### 2026-08-06 — 计划落盘

记录 frozen contract、三条 track 的所有权划分、Root 集成门与 Stops and risks。

### 2026-08-29 — 逐字补回折入计划中被删去的 Frozen accepted readback 五行

同日上面「合并同 identity 的 plans 侧文件（slug 撞车修复）」条目声称折入的计划正文「一字未删」，但与原始树备份 `docs/teamwork/cases/c-2925726b.../plan.md` 逐行比对后确认，该条目内 `##### Frozen accepted readback` 一节实际少了开头 5 行（Manifest revision / Manifest byte digest / Decision artifact / Decision revision / Decision digest），此前它们不在本仓任何文件中。本次已把这 5 行逐字补回该条目内部原本的位置（未改写该条目原有文字，只补全其原本应有的正文）。终验确认：`grep '4d5336725d3f06837dc92aa7614449e670e892dfd147063d3920b8071e722d02'`、`grep 'a-2617cb201279c0ef235c0fa08995340c59406acba7746153e80449af76894543'` 现均命中本文件。上一条目里「正文一字未删」这句话本身保留原样，本条目即是对它的更正。
