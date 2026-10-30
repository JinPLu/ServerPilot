---
status: active
superseded-by:
created: 2026-08-17
updated: 2026-08-17
---

# Plan: ServerPilot project cleanup sweep plus delete-server reliability

## Intended result and selected direction

- Subject identity: ServerPilot project cleanup sweep plus delete-server reliability
- Authoritative owner: Root agent
- Intended result: narrowed cleanup of verified dead artifacts plus reliable delete-server behavior, without harming protected live surfaces
- Selected direction: successor/current host plan `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` after revision on 2026-08-17
- Scope and protected behavior:
  - Explicit protected boundaries for edit/delete/keepalive
  - Domain pause/reservation/maintenance
  - GET/read-only paths
  - Current dashboards
  - Web add-server
  - Windows
  - Routine MCP
  - Keep read-only reservation/maintenance pages
  - Keep web-delete non-action regression

## Current execution plan

1. Establish delete-server reliability: add `endpoint_delete` to stale-daemon gates; introduce persistent deletion tombstone with explicit create clearing it; ship human-friendly scrollable Danger Zone UX; strengthen tests; verify daemon switch/health.
2. Narrow cleanup to verified dead artifacts/views/public wrappers and broken mutation forms only; do not remove protected live edit/delete/keepalive, domain pause/reservation/maintenance, GET/read-only paths, current dashboards, Web add-server, Windows, or routine MCP; keep read-only reservation/maintenance pages and the web-delete non-action regression.
3. Stop for user approval before executing the revised successor plan; do not execute cleanup or delete-server changes until approved.

## Dependencies and parallel tracks

- Depends on prior review verdict REVISE of the frozen pre-revision plan (`docs/teamwork/records/project-cleanup-safety.md`).
- Delete-server reliability work gates safe cleanup of deletion-related surfaces.
- Cleanup track is narrowed and must not overlap protected boundaries listed above.
- No independent parallel tracks certified beyond that dependency order.

## Verification

- Focused pytest
- Ruff
- Desktop build/verify
- `/health/live` includes `endpoint_delete`
- Restart non-resurrection
- Live edit/delete/keepalive
- Read-only pages
- Routine MCP 3 tools

## Risks and replan conditions

- Accidental removal of live edit/delete/keepalive, domain pause/reservation/maintenance, GET/read-only paths, dashboards, Web add-server, Windows, or routine MCP requires replan.
- YAML bootstrap resurrection without tombstone/create-clear semantics requires replan of deletion durability.
- Stale daemon lacking `endpoint_delete` without updated gates blocks reliable delete-server.
- Execution only after user approval of this revised successor plan; proceed without approval is a stop condition.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-17 — successor plan after REVISE

Owner-certified plan delta for subject ServerPilot project cleanup sweep plus delete-server reliability. Host plan: `~/.cursor/plans/project_cleanup_sweep_7d2b25e2.plan.md` after revision. Adds explicit protected boundaries for edit/delete/keepalive, domain pause/reservation/maintenance, GET/read-only paths, current dashboards, Web add-server, Windows, routine MCP. Adds delete-server reliability work: `endpoint_delete` stale daemon gates, persistent deletion tombstone with explicit create clearing it, human-friendly scrollable Danger Zone UX, stronger tests, daemon switch/health verification. Narrows cleanup to verified dead artifacts/views/public wrappers and broken mutation forms; keeps read-only reservation/maintenance pages and web-delete non-action regression. Verification: focused pytest, Ruff, desktop build/verify, `/health/live` `endpoint_delete`, restart non-resurrection, live edit/delete/keepalive, read-only pages, routine MCP 3 tools. Next action remains: execute only after user approval.
