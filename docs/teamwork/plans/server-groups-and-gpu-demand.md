---
status: active
superseded-by:
created: 2026-08-27
updated: 2026-08-27
---

# Plan: Server groups and task-declared GPU demand

## Intended result and selected direction

- Subject identity: ServerPilot first-class server groups and routine GPU demand selection.
- Authoritative owner: Root agent supervises integration and acceptance; bounded Grok workers own non-overlapping implementation surfaces.
- Intended result: an agent must assess a server group's path, environment notes, data/weight notes, and live GPU shape before the broker best-fits a host inside that group; a one-GPU task must never become an eight-GPU lease.
- Selected direction: add first-class `server_group_id`; aggregate free capacity by group, server, and GPU shape; keep `gpu_count=1` as the safe default and require its explicit multi-GPU value to come from the launch script or configuration.
- Scope and protected behavior: ServerPilot coordinates but does not launch workloads; environment metadata is never executed or injected; external scheduler/plugin targeting and ungrouped legacy endpoints remain compatible; legacy `storage_group` is not silently promoted into a scheduling boundary.

## Current execution plan

1. Backend owner adds the group model, migration, strict inventory/API contracts, effective workspace inheritance, CRUD, and group-aware eligibility before existing host best-fit.
2. MCP owner replaces the flat free-card menu with grouped per-server capacity, adds `server_group_id`, and records launch configuration as the sole semantic source of `gpu_count`.
3. macOS Core and UI owners add group decoding, CRUD, assignment, grouped table sections, details, and group-first human claims without changing the 44 pt server comparison row.
4. Windows/Web owner adds compatible group rendering, assignment, and bridge allowlists.
5. Root integrates the contracts, repairs cross-surface failures, freezes a candidate, obtains an independent Teamwork review, and accepts only after direct verification.

## Authoritative project records

- Project constraints: `AGENTS.md`.
- Accepted host plan: `~/.cursor/plans/server-group-gpu-demand_71883659.plan.md`.
- This durable plan: `docs/teamwork/plans/server-groups-and-gpu-demand.md`.
- Completion evidence will be recorded in `docs/teamwork/records/server-groups-and-gpu-demand-review.md`.

## Dependencies and parallel tracks

The backend, routine MCP, macOS Core, macOS UI, and Windows/Web tracks may edit only their assigned paths in parallel. Integration tests depend on the backend contract. Desktop application build and verification run only after all Swift surfaces are merged. Independent review starts only after the integrated candidate and verification evidence are frozen.

## Verification

- Multiple configured groups require an explicit suitable group; allocation never falls through to another group.
- The existing allocator best-fits a single host only after group filtering.
- A one-card request on an eight-card host creates a one-card lease and leaves seven cards advertised as capacity.
- Free capacity is grouped per server/SKU and cannot be mistaken for task demand; MCP text says task launch configuration, not capacity, determines `gpu_count`.
- Migration, Python tests/Ruff, Swift package tests, relevant Windows/Web checks, `desktop/build-macos-app.sh`, and `desktop/verify-macos-app.sh` pass.
- An independent Reviewer returns `ACCEPT` against the accepted plan and standing project constraints.

## Risks and replan conditions

- Replan if group workspace inheritance cannot preserve keepalive safety, if plugin apply can no longer target one scheduler endpoint, or if routine group enforcement would silently strand existing ungrouped capacity.
- Do not infer semantic groups from `storage_group`; production meaning is unverified.
- Tests support acceptance but do not replace the final root application build and MCP protocol/schema observation.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-27 23:38 UTC+8 — plan accepted for execution

The user selected first-class groups with group suitability evaluated before host best-fit, and selected agent-derived GPU count without a hard cap, proof field, or command parser. Parallel Grok execution was authorized with Root retaining Teamwork supervision and acceptance.
