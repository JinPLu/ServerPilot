---
status: active
superseded-by:
created: 2026-08-28
updated: 2026-08-29
---

# Record: Server groups and GPU demand 审查判决

## Current synthesis

- Subject identity: server group + 任务声明式 GPU 需求实现候选的审查判决这一持续目标

Candidate 2 is `ACCEPT`. It closes candidate 1's Web empty-group,
workspace-prefill, and human multi-host claim failures while preserving
ungrouped, plugin, keepalive-serialization, and explicit advanced spanning
compatibility. The routine MCP path still derives multi-GPU demand from launch
configuration, defaults safely to one GPU, and allocates exactly the requested
cards on one best-fit host inside the selected group.

## Candidate identity and review contract

- Frozen candidate: HEAD `80f4c69f01f8019b7052f5415924651a798e6311` plus the working tree frozen after the 2026-08-28 00:35 UTC+8 compatibility correction, full test run, and canonical App rebuild.
- Authoritative owner and independence: Root owns acceptance; the same independent read-only Reviewer opened a new successor review after returning `REVISE` on candidate 1. A read-only Challenger separately re-probed the allocation boundary.
- Scope and exclusions: all first-class group, workspace inheritance, allocation, MCP, macOS/Windows/Web, policy, documentation, migration, and automated/build evidence in the working tree. Live daemon restart, deployed database migration, connector regeneration, and live GPU/server mutation were excluded.
- Successor: none.

## Criteria and protected boundaries

- First-class groups carry an absolute default workspace, descriptive
  environment/data-weight notes, and description; notes are never executed or
  injected.
- Endpoint membership is singular `server_group_id`; allocation filters are
  plural `server_group_ids`; a nullable endpoint override preserves live group
  inheritance.
- Legacy `storage_group` remains inert compatibility metadata.
- Group filtering precedes best-fit, selected-group routine claims remain on
  one host, and one-GPU demand never becomes an eight-GPU lease.
- Routine requests with formal groups require a group or an explicit ungrouped
  host before persistence. Plugin/scheduler and generic advanced topology
  remain bounded compatibility paths.
- `gpu_count=1` is the safe default; explicit multi-GPU count comes from launch
  configuration, never from free capacity.
- macOS, Windows, Web, REST, inventory, MCP, policies, and public documentation
  share the same contract without claiming deployment.

## Direct evidence

- Root observed the final `uv run --reinstall-package serverpilot pytest -q` full suite pass after correcting the over-strict routine route.
- Root observed `uv run ruff check . && git diff --check` pass.
- Root observed both JavaScript syntax checks and `swift build` pass on candidate 2. `swift test` is unavailable in this Command Line Tools environment because XCTest is absent.
- Root observed the final `zsh desktop/build-macos-app.sh && zsh desktop/verify-macos-app.sh` pass after the API correction; the canonical root `ServerPilot.app` was replaced.
- Reviewer inspected the successor's actual Web JS/noscript claims, route canonicalization, plugin and advanced paths, all human payload builders, workspace forms, group domain/migration, MCP projection, policies, documentation, and behavioral tests.
- Candidate 1's immutable `REVISE` record is `docs/teamwork/reviews/2026-08-28-server-groups-and-gpu-demand-candidate-1.md` (merged into this file's History).

## Findings

No material findings.

Non-blocking residuals:

- `endpoints.server_group_id` remains service-enforced rather than a database
  foreign key because the historical initial migration imports current model
  metadata. Supported inventory/REST paths validate membership and deletion.
- An extra closing `</dialog>` in the dashboard is ignored by HTML parsers and
  did not affect the group/claim dialogs; it is unrelated to the accepted
  behavior.

## Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| First-class group domain, CRUD, inventory, migration | met | Domain/service/API source and full tests |
| Live workspace inheritance and explicit endpoint override | met | Blank grouped Web/Windows/macOS create inherits; typed path remains override |
| `storage_group` isolation and note non-execution | met | Separate field, migration tests, collector/plugin data-flow inspection |
| Group filter before one-host best-fit | met | Eligibility and placement paths; grouped human/MCP payloads carry `same_host=true` |
| Missing-group refusal before persistence | met | JS pre-fetch refusal, noscript behavioral test, routine route canonicalization and service gate |
| Explicit ungrouped/plugin/advanced compatibility | met | Ungrouped endpoint validation, plugin regression, keepalive serialization regression, generic `/api/v1/claims` spanning test |
| One-GPU demand never becomes eight-GPU lease | met | MCP safe default and count-source contract; exact selector size and best-fit tests |
| Grouped capacity is group → server → SKU | met | MCP runtime projection and schema tests |
| macOS/Windows/Web contracts and form limits | met | Payload builders, bridge allowlist, inherited-workspace and behavioral tests |
| Public docs and changelogs | met | Unreleased entries and explicit no-deployment statements |
| Full automated/static/desktop build gates | met | Root-observed final verification commands |
| Swift unit tests | unknown / environment-limited | XCTest unavailable; Swift package and full App compilation passed |
| Live daemon/MCP connector deployment | out of scope | Documentation explicitly does not claim it |

## Verdict

`ACCEPT` for candidate 2.

## Residual uncertainty and bounded recheck

Swift XCTest execution remains unknown until a full Xcode environment is
available. The installed/running MCP connector still reflects whatever version
is currently deployed; source acceptance does not imply daemon restart,
database migration, or connector descriptor regeneration. Those are separate
authorized deployment steps, not blockers for this source candidate.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-28 00:40 UTC+8 — independent successor verdict

The Reviewer returned `ACCEPT`: candidate 1's two high and two medium findings
are corrected and covered by behavior tests, with no material successor
findings.

### 2026-08-28 00:41 UTC+8 — post-verdict adversarial evidence

The parallel Challenger found a distinct routine-topology bypass not covered by
the Reviewer: a crafted `/api/v1/routine/claims` body could retain
`nodes > 1` or exact GPU selection while the handler copied
`same_host=true` without revalidation, causing the service's routine group gate
to treat the request as advanced. It also found that the Web preset-profile
claim surface could expose an old direct profile without a group. Root withheld
final acceptance and opened candidate 3; this evidence does not rewrite the
independent candidate-2 verdict.

### 2026-08-28 — candidate 1 的 REVISE 判决并入

##### Current synthesis

Candidate 1 is `REVISE`. The MCP routine path keeps `gpu_count=1` as a safe
default, allocates exactly one GPU, rejects a grouped direct-host pin without a
group, and best-fits inside a selected group. The frozen candidate nevertheless
left two human-surface escapes: the Web claim path could submit no group with
`same_host=false`, and Web/Windows grouped server creation could persist the
stock `/srv/serverpilot-workspace` value as an override instead of inheriting
the group workspace.

##### Candidate identity and review contract

- Frozen candidate: HEAD `80f4c69f01f8019b7052f5415924651a798e6311` plus the uncommitted server-group working tree frozen after the 2026-08-28 00:13 UTC+8 full test and desktop build.
- Authoritative owner and independence: Root owns acceptance; an independent read-only Reviewer and a separate read-only Challenger inspected the frozen tree.
- Scope and exclusions: server-group model/migration/inventory/REST, effective workspace inheritance, allocation boundaries, MCP projection and tools, macOS/Windows/Web surfaces, policies, tests, and build evidence. Live server/GPU mutation and deployment were excluded.
- Successor: pending candidate 2 after the findings below are corrected.

##### Criteria and protected boundaries

- First-class groups carry an absolute default workspace, plain-text
  environment/data-weight notes, and description; notes are never executed or
  injected.
- Endpoint membership is singular `server_group_id`; claim filtering is plural
  `server_group_ids`; endpoint override wins over a live group default.
- Legacy `storage_group` is neither promoted nor used for scheduling.
- Group filtering precedes host best-fit and never crosses a selected group.
- Routine direct claims require an explicit group when formal groups exist,
  while explicit ungrouped, plugin/scheduler, and advanced topology paths retain
  their bounded compatibility.
- GPU count comes from task launch configuration, with safe default 1; capacity
  is not a demand signal and one-GPU demand must not become an eight-GPU lease.
- All human surfaces preserve the same group-first, one-host routine semantics.
- No live GPU/server state changes and no commit.

##### Direct evidence

- Root observed `uv run --reinstall-package serverpilot pytest -q` pass on the frozen candidate.
- Root observed Ruff and both JavaScript syntax checks pass.
- Root observed `swift build` pass; `swift test` was unavailable because this Command Line Tools environment has no XCTest.
- Root observed `zsh desktop/build-macos-app.sh && zsh desktop/verify-macos-app.sh` pass and replace the canonical root `ServerPilot.app`.
- Reviewer traced Web form submission, Swift/Windows claim payloads, endpoint create defaults, service admission, eligibility, and tests.
- Challenger independently traced MCP schema through request, selection, and lease creation and confirmed that the MCP 1-GPU-on-8-GPU-host path leases one card.

##### Findings

| Severity | Finding | Precise evidence | Impact / failed criterion | Smallest correction |
| --- | --- | --- | --- | --- |
| High | Web one-off claim accepted the empty group option and omitted `same_host`. | `src/serverpilot/web/static/app.js` built constraints without rejecting an empty `server_group_id`; `ResourceConstraints.same_host` defaults false; `service._is_routine_direct_claim` gates group selection on true. | A human Web claim could allocate in the wrong cluster and create request/lease/idempotency instead of returning `group_selection_required`. | Require a group or explicit ungrouped host on Web, and send `same_host=true`. Add a behavioral regression. |
| High | Web/Windows grouped endpoint create could freeze the stock workspace as an override. | Dashboard and Windows add forms prefilled `/srv/serverpilot-workspace`; grouped mutation converted the nonempty value into `workspace_path_override`. | The endpoint stopped inheriting its group’s data/weight workspace, defeating later group path changes. | Clear/disable the stock default when a group is selected; grouped inherit sends an explicit null override. Add create-path tests. |
| Medium | macOS, Windows, and Web grouped human claims omitted `same_host=true`. | `BrokerStore.claimConstraints` and both JavaScript claim builders sent pack/group/count but no one-host constraint. | Multi-GPU routine claims could span hosts inside one group instead of selecting one best-fit host. | Send and allowlist `same_host=true` for routine GUI claims. |
| Medium | Existing tests asserted payload fragments but not the two failing behaviors. | Tests covered selected groups and string shape, not empty-group submission or untouched stock path. | Green tests were insufficient evidence for the protected UI contracts. | Add behavioral tests at the actual form/bridge and service boundaries. |
| Low | `Endpoint.server_group_id` has no database foreign key. | ORM and migration use a nullable indexed string; service enforces existence and member-protected delete. | Raw SQL could create a dangling membership, although supported API/inventory paths reject it. | Optional later hardening; not a blocker for the supported contract. |
| Low | Repository README/status text and installed Cursor MCP descriptors can remain stale until the new daemon is deployed. | Runtime source and policy docs contain groups; the currently installed connector descriptor was generated from the previous running service. | Discovery may show the old contract before deployment/restart. | Update tracked user documentation now; regenerate live descriptors only as part of an authorized deployment. |

##### Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Group domain, migration, CRUD, metadata | met | Backend source and focused/full tests |
| Live workspace inheritance | not met on Web/Windows create | Stock prefill became an override |
| `storage_group` isolation | met | Separate fields and migration tests |
| Group-filtered best-fit and 1-GPU lease size | met on MCP/selected-group path | Service/MCP traces and placement tests |
| Missing-group refusal before persistence | not met on Web/default non-one-host request | Empty group plus `same_host=false` bypassed the gate |
| Human group-first one-host semantics | not met | All grouped GUI payloads omitted `same_host` |
| Policies and non-executable notes | met in source | Policy and data-flow inspection |
| Verification adequacy | not met | Missing behavioral tests for the two high findings |

##### Verdict

`REVISE` for candidate 1.

##### Residual uncertainty and bounded recheck

The successor must be reviewed as a new candidate because code and protected
behavior will change. Its minimum direct evidence is: empty Web group is
rejected without persistence; grouped Web/Windows create with untouched UI
defaults stores `workspace_path_override=null`; all grouped human claims carry
`same_host=true`; a selected-group one-card request on an eight-card host creates
one lease resource; full Python/static/Swift/app verification remains green.
Deployment and currently installed Cursor MCP descriptors remain outside this
local source candidate.

##### History

###### 2026-08-28 00:22 UTC+8 — independent verdict

Reviewer returned `REVISE`; Challenger independently confirmed the MCP
one-card path and exposed the same human/default-topology boundary. Candidate 1
was frozen and not repaired in place.

###### 2026-08-28 00:40 UTC+8 — owner-certified successor

Candidate 2 is recorded at
`docs/teamwork/records/server-groups-and-gpu-demand-review.md`
with an independent `ACCEPT` verdict.

### 2026-08-29 — 恢复被 relink 抹掉的前序文档名

`:54` 的「Candidate 1's immutable `REVISE` record」指向了本文件自己的路径，即用链尾的 ACCEPT 记录冒充 candidate 1 的 REVISE 记录。按备份原文恢复为 `docs/teamwork/reviews/2026-08-28-server-groups-and-gpu-demand-candidate-1.md`，并注明它已并入本文件的 History。
