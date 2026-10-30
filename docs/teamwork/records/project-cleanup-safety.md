---
status: active
superseded-by:
created: 2026-08-17
updated: 2026-08-17
---

# Record: ServerPilot project cleanup sweep plus delete-server reliability

## Current synthesis

- Subject identity: 清理扫除候选计划的审查这一持续目标（verdict REVISE）

Frozen pre-revision plan candidate reviewed; verdict REVISE. Decisive basis: delete-server is hidden because the live daemon is 1.5.10 without `endpoint_delete`, and stale-runtime checks omit that capability; plus high-severity protected-boundary and resurrection risks. Residual uncertainty on read-only reservation/maintenance pages was owner-resolved: keep them. Next action: execute the revised successor plan only after user approval.

## Candidate identity and review contract

- Frozen candidate: `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` as it existed before revision on 2026-08-17
- Authoritative owner and independence: Root agent; labelled non-independent (Root-owned review)
- Scope and exclusions: ServerPilot project cleanup sweep plus delete-server reliability; no execution of cleanup or deletion changes in this review record
- Successor: `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` after revision (plan checkpoint: `docs/teamwork/plans/project-cleanup-sweep.md`)

## Criteria and protected boundaries

- Live edit, delete, and keepalive must not be accidentally removed by plan wording
- Reservation/maintenance domain and GET paths must remain
- Pause/resume domain methods must remain
- Dead Swift removal must not sweep shared helpers
- Web delete non-action regression should remain
- Read-only reservation/maintenance pages: keep (Root-resolved)

## Direct evidence

- Frozen candidate: host plan file `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` before revision on 2026-08-17
- Live daemon reported as 1.5.10 without `endpoint_delete`; stale-runtime checks omit the capability (owner-certified review evidence)

## Findings

| Severity | Finding | Precise evidence | Impact / failed criterion | Smallest correction |
| --- | --- | --- | --- | --- |
| Critical | delete-server is hidden | live daemon is 1.5.10 without `endpoint_delete`; stale-runtime checks omit the capability | delete-server unavailable / capability gate incomplete | add `endpoint_delete` to stale-daemon gates and verify live health |
| High | deletion can be undone by YAML bootstrap | owner-certified review of frozen plan | deleted servers can resurrect | persistent deletion tombstone; explicit create clears it |
| High | plan wording could accidentally remove live edit/keepalive | owner-certified review of frozen plan | live edit/keepalive at risk | explicit protected boundaries for edit/delete/keepalive |
| High | reservation/maintenance domain and GET paths must remain | owner-certified review of frozen plan | domain/GET surfaces at risk | keep reservation/maintenance domain and GET/read-only paths |
| High | pause/resume domain methods must remain | owner-certified review of frozen plan | pause/resume at risk | keep domain pause/resume methods |
| Medium | entry gate depends only on `endpoint_update` | owner-certified review of frozen plan | incomplete capability gating | gate on delete-related capability as well |
| Medium | destructive UX/tests incomplete | owner-certified review of frozen plan | incomplete destructive path proof | human-friendly scrollable Danger Zone UX; stronger tests |
| Medium | dead Swift removal must not sweep shared helpers | owner-certified review of frozen plan | shared helpers at risk | narrow dead-Swift removal; do not sweep shared helpers |
| Medium | web delete non-action regression should remain | owner-certified review of frozen plan | regression coverage at risk | keep web-delete non-action regression |

## Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| delete-server visible / `endpoint_delete` stale-runtime gate | not met | live daemon 1.5.10 without `endpoint_delete`; stale-runtime checks omit capability |
| deletion survives YAML bootstrap | not met | deletion can be undone by YAML bootstrap |
| live edit/keepalive protected in plan wording | not met | plan wording could accidentally remove live edit/keepalive |
| reservation/maintenance domain and GET paths preserved | not met | must remain; frozen plan insufficiently protected |
| pause/resume domain methods preserved | not met | must remain; frozen plan insufficiently protected |
| entry gate includes delete capability (not only `endpoint_update`) | not met | entry gate depends only on `endpoint_update` |
| destructive UX/tests complete | not met | destructive UX/tests incomplete |
| dead Swift removal spares shared helpers | not met | must not sweep shared helpers |
| web delete non-action regression retained | not met | should remain |
| read-only reservation/maintenance pages disposition | resolved keep | residual uncertainty resolved by Root: keep them |

## Verdict

REVISE

## Residual uncertainty and bounded recheck

Whether read-only reservation/maintenance pages should remain was residual uncertainty; Root resolved safely: keep them. No further bounded recheck of the unchanged frozen pre-revision candidate is required for that item. Next action: execute revised successor plan only after user approval. Changed content belongs to the successor plan/candidate, not a recheck of this frozen candidate.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-17 — initial REVISE verdict on frozen pre-revision plan

Owner-certified review of `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` as it existed before revision on 2026-08-17. Verdict REVISE. Critical: delete-server hidden (daemon 1.5.10 without `endpoint_delete`; stale-runtime checks omit capability). High: YAML bootstrap can undo deletion; plan wording risk to live edit/keepalive; reservation/maintenance domain and GET paths must remain; pause/resume domain methods must remain. Medium: entry gate only `endpoint_update`; destructive UX/tests incomplete; dead Swift removal must not sweep shared helpers; web delete non-action regression should remain. Residual uncertainty on read-only reservation/maintenance pages resolved by Root: keep them. Successor link: same host plan path after revision; Teamwork plan checkpoint `docs/teamwork/plans/project-cleanup-sweep.md`. Next action: execute revised successor plan only after user approval.
