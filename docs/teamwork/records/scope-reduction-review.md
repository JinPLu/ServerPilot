---
status: active
superseded-by:
created: 2026-08-28
updated: 2026-08-29
---

# Record: Scope reduction 审查判决

## Current synthesis

- Subject identity: 范围缩减候选的审查判决这一持续目标

Candidate 2 is `ACCEPT`. Both failures from candidate 1 are closed: the live
adapter contract and the desktop design system no longer describe the deleted
external scheduler as a current capability, and the browser-UI packaging,
session setting, dead types, dead projection and the inert `profile_id` column
are gone. The protected runtime paths still hold after the corrections, and the
corrections themselves introduced no new boundary break.

## Candidate identity and review contract

- Frozen candidate: uncommitted working tree on top of commit `7a1b065`, after the corrections that followed candidate 1; 91 files changed, +3829 / −25005.
- Predecessor: `docs/teamwork/reviews/2026-08-28-scope-reduction-candidate-1.md` (merged into this file's History), verdict `REVISE`.
- Authoritative owner and independence: Root owns acceptance; the same independent read-only Reviewer opened this successor identity rather than rewriting the earlier record, informed by a parallel read-only adversarial pass.
- Scope and exclusions: whether the candidate-1 failures are genuinely closed, whether the protected boundaries still hold, and whether the corrections introduced anything new. Excluded: Swift unit tests (no XCTest here) and redeployment of the running daemon or installed MCP connector.

## Criteria and protected boundaries

Unchanged from candidate 1: the four core functions; the accepted server-group
invariants; the desktop REST contract for both clients; safety behaviour around
roles, idempotency, deletion refusal and keepalive fail-closed; clusters usable
through the local plugin mechanism; server deletion in the app and REST but not
as an MCP tool; and nothing left in code, packaging or documentation that refers
to a removed surface.

## Direct evidence

- Root observed the full suite green at 547 tests, `ruff check .`, `git diff --check`, `swift build`, `zsh desktop/build-macos-app.sh`, `zsh desktop/verify-macos-app.sh` and `node --check` all passing, with the canonical root app rebuilt from the edited migration.
- Root confirmed on a database upgraded to `20260827_0032` that no surviving table holds a foreign key into any of the thirteen dropped tables, and that the production `Database.migrate()` path reached head with all thirteen gone, twenty-eight tables remaining, unrelated rows intact and `PRAGMA foreign_key_check` clean.
- Root separately seeded a project, an actor, a workload profile and an allocation request referencing that profile at `20260827_0032`, then ran `Database.migrate()`: the profile table, the `profile_id` column and its index disappeared while the historical request row survived intact with no foreign key violation.
- The Reviewer traced each closed finding to its source evidence and re-checked the protected boundaries rather than assuming they held.

## Findings

No material findings.

Non-blocking residuals recorded without failing a criterion:

- `_eligible_gpus` still computes an always-empty generic host usage map that
  contributes nothing and no longer reaches any projection.
- The Windows app still has no server-deletion path. This is a pre-existing gap
  recorded in candidate 1, not a regression.
- A local database that already ran candidate 1's version of `20260828_0033`
  would keep the `profile_id` column, because Alembic will not re-run a revision
  it has already applied. The revision is unreleased and the running daemon has
  not been restarted, so the upgrade path from `20260827_0032` is the one that
  matters.

## Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Four core functions | met | Collection, snapshot, routine and human claims, operator reassignment and release, keepalive identity checks all untouched by the corrections |
| Server-group invariants | met | Pre-persistence selection gate, partitioned selection, plugin exemption all intact |
| Desktop REST contract | met | Every macOS and Windows call site still resolves, including operator GPU reassignment, release-empty and endpoint deletion |
| Safety boundaries | met | Role checks, idempotency, deletion refusal with active leases, no MCP delete tool |
| Plugin cluster path | met | Plugin-derived capacity still projected into `gpu_status`; plugin apply path intact |
| Drop migration on a populated database | met | Thirteen tables dropped before the batch column drop; historical allocation requests survive; Root's live upgrade clean |
| Removing `profile_id` did not break requests | met | Model, construction, projection and audit no longer carry it; no client ever sent it |
| Remaining design coherent for a reader or agent | met | Adapter contract, design system and implementation status now match the code |
| No misleading leftovers | met | Packaging, session setting, dead types, dead projection, alias and inert column all removed |
| MCP reduced to five tools, no delete tool | met | Single registry with the five intended names |
| Swift unit tests / live redeployment | out of scope | Environment limitation and a separate deployment step |

## Verdict

`ACCEPT` for candidate 2.

## Residual uncertainty and bounded recheck

The Reviewer did not re-execute the suite or the desktop build; Root's direct
observations stand as the verification status. A machine that already applied
the earlier form of `20260828_0033` was not reproduced. Accepting this source
candidate does not accept a deployment: restarting the daemon, upgrading the
running database and reinstalling the MCP connector remain separate steps.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-28 03:05 UTC+8 — successor verdict

The Reviewer returned `ACCEPT` with no material findings, confirming that both
candidate-1 failures are closed and that the corrections introduced no new
break in a protected boundary.

### 2026-08-28 — candidate 1 的 REVISE 判决并入

##### Current synthesis

Candidate 1 is `REVISE`. Every protected runtime path survives the deletion:
the five-tool MCP, group-partitioned allocation, the plugin cluster path and its
capacity projection, the desktop REST contract, endpoint-deletion refusal,
keepalive identity checks, and the ordering of the new drop migration. What
fails is coherence of the remaining product for a reader or an agent: two live
documents still describe the deleted external-scheduler surface as current, and
the browser-UI packaging plus some dead types survived the code removal.

##### Candidate identity and review contract

- Frozen candidate: uncommitted working tree on top of commit `7a1b065`; 83 files changed, +3794 / −24701.
- Authoritative owner and independence: Root owns acceptance; an independent read-only Reviewer produced the verdict and a separate read-only Challenger probed the same tree.
- Scope and exclusions: whether the reduced product remains coherent and functional. Excluded: Swift unit tests (no XCTest on this machine) and redeployment of the running daemon or the installed MCP connector.
- Successor: pending candidate 2 after the findings below are corrected.

##### Criteria and protected boundaries

- The four core functions stay intact: read-only collection, human monitoring
  and correction in the app, agent claim/reassign/release, and per-GPU keepalive
  occupancy with fail-closed identity checks.
- The accepted server-group invariants stay intact.
- Every REST path the macOS app and the Windows launcher call still exists and
  behaves the same.
- Clusters remain usable through the local plugin mechanism.
- Server deletion stays in the app and REST and is deliberately not an MCP tool.
- Nothing left in code, packaging, or documentation refers to a removed surface.

##### Direct evidence

- Root observed the full suite green at 547 tests in about 56 seconds, down from about 100 seconds at 581 tests.
- Root observed `ruff check .`, `git diff --check`, `swift build`, `node --check`, and both the macOS app build and standalone verification passing, with the canonical root app rebuilt.
- Root observed the live MCP server listing exactly the five intended tools.
- Root confirmed every `service.` attribute referenced anywhere in `src/**` still exists, that the CLI never calls the HTTP client, and that the migration suite already upgrades a database containing scheduler rows through the drop.
- The Reviewer traced desktop call sites, the service and MCP registries, the plugin path, and the drop-migration ordering against the surviving models.

##### Findings

| Severity | Finding | Precise evidence | Impact / failed criterion | Smallest correction |
| --- | --- | --- | --- | --- |
| Medium | The adapter contract still presents the deleted external scheduler as current | `docs/ADAPTERS_zh.md` lists a `slurm-command` scheduler adapter and an external-scheduler section; both READMEs link to it as the live adapter reference, while `adapters.py` registers only `raw-ssh` and `server-script-v1` | A reader or agent would try to register a cluster through a surface that no longer exists | Rewrite the file around the plugin path and delete the scheduler adapter and section |
| Medium | The design system still mandates UI rules for deleted scheduler targets | `docs/DESIGN_SYSTEM.md` requires external scheduler targets to keep their own claim path and to stay separated from ordinary servers | Later desktop work would rebuild UI for an object the product no longer has | Remove those rules; the claim path is groups and ungrouped hosts only |
| Low | Browser-UI packaging and session configuration outlived the UI | `pyproject.toml` still declares `jinja2` and `itsdangerous` with no importer left; `config.py` still reads `SERVERPILOT_SESSION_SECRET` while no session or CSRF machinery remains | The distribution still implies a template and session UI | Drop the dependencies and the setting, refresh the lock |
| Low | Dead scheduler types and an unreachable projection stayed behind | `adapters.py` `OperationId` contains only scheduler operations with no caller; `mcp_server.py` keeps an unreachable compact-status helper describing scheduler and profile collections; the snapshot still emits `generic_claim_*` counters that are always zero | A reader concludes the planning surface still exists | Delete the dead type, the dead helper, and the always-zero snapshot fields |
| Non-blocking | `routine_mcp` remains an alias of the single registry, and one docstring still mentions provider cascade | `mcp_server.py`; `service.delete_endpoint` docstring | Neither creates a second registry nor changes behaviour | Optional tidy |

##### Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| Four core functions | met | Collector, state projection, claim/reassign/release, and keepalive attestation paths intact |
| Server-group invariants | met | Group partitioning, pre-persistence selection gate, one-card lease, safe default all still asserted |
| Desktop REST contract | met | Every macOS and Windows call site resolves to a surviving route |
| Safety boundaries | met | Roles, idempotency, deletion refusal with active leases, keepalive fail-closed |
| Plugin cluster path | met | Observation profiles, plugin apply/release, plugin-derived capacity in `gpu_status` |
| Drop migration on a populated database | met | Child tables dropped before parents; an existing migration test upgrades a populated database through it |
| Deleted tests removed only dead-code coverage | met | Behavioural coverage rewritten onto surfaces the product still exposes |
| MCP reduced to five tools, no delete tool | met | Live registry listing matches |
| Remaining design coherent for a reader or agent | not met | Two live documents describe deleted capabilities as current |
| No misleading leftovers | not met | Packaging, setting, dead type, dead projection |
| Automated and desktop build gates | met | Root-observed runs |
| Swift unit tests / live redeployment | out of scope | Environment limitation and a separate deployment step |

##### Verdict

`REVISE` for candidate 1.

##### Residual uncertainty and bounded recheck

The Reviewer did not re-execute the suite or the builds; Root's direct
observations stand as the verification status. A populated database containing
scheduler job and resource allocation child rows was not upgraded as a live
reproduction, though drop ordering and the surviving models support safety.
Whether the Windows app ever offered server deletion is a pre-existing gap
rather than a regression introduced here.

##### History

###### 2026-08-28 02:50 UTC+8 — independent verdict

The Reviewer returned `REVISE`: no runtime path is broken, but the reduced
product is not yet self-consistent in its live documentation and packaging.

### 2026-08-29 — 恢复被 relink 抹掉的前序文档名

`:24` 的 `Predecessor` 指向了本文件自己的路径，即声称本文件是本文件的前序。按备份原文恢复为 candidate 1 的评审记录 `docs/teamwork/reviews/2026-08-28-scope-reduction-candidate-1.md`，并注明它已并入本文件的 History。
