---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-29
---

# Record: Gpu Broker Keepalive Per Gpu 2026 08 12 — 冻结候选评审

## Current synthesis

- Subject identity: per-GPU keepalive 冻结候选（1.5.0）的严格评审判决这一持续目标
- 判决 REVISE：安装路径交付不出所要求的默认申请体验；`execution_entrypoint` 强制但行为上惰性。
- `IMPLEMENTATION_STATUS_zh.md` 中「该候选获得 ACCEPT」的说法不被本判决支持，须先更正。

## Review mode

Strict Review of the frozen 1.5.0 candidate. The candidate under review is the initial tracked diff, plus the four supplied untracked files. The material criteria are an installation-ready normal request, a non-inert project execution handoff, and preservation of the supplied managed-claim and sealed-adapter boundaries.

## Findings

- **Major — installation does not deliver the requested default-request experience.** [install_agent_policy.py](scripts/install_agent_policy.py:70) only installs Markdown policy. [cli.py](src/serverpilot/cli.py:470) refuses to write a card unless the named enabled direct-GPU profile already exists, while the guide explicitly requires manually creating that profile first at [AGENT_MCP_zh.md](docs/AGENT_MCP_zh.md:83). The tests start with an injected existing profile and provide no from-empty-install claim evidence. Therefore a newly installed Agent still cannot make a normal default request without prior profile/card setup, contrary to the supplied outcome. Smallest correction: provide one explicit user-approved onboarding/install path that provisions or selects the default profile and writes/checks the project card, without inferring authorization from names, inventory, or free GPUs; cover an empty-state install-to-claim flow with fakes.

- **Moderate — `execution_entrypoint` is mandatory but behaviorally inert.** It is required by [project_resource_card.py](src/serverpilot/project_resource_card.py:28) and `--entrypoint` is mandatory in [cli.py](src/serverpilot/cli.py:483), but repository-wide inspection found no entrypoint registry, resolver, or consumer. The installed English policy only consumes `profile_id`, and `check` validates only the profile. This adds redundant setup and does not establish the documented project execution handoff. Smallest correction: remove the field/option and associated claims if the card is only for claiming, or add a concrete project-owned resolver/check and Agent-policy consumption without giving ServerPilot autonomous workload-launch authority.

## Protected boundaries

- Managed claim reclaim is entered only after `no_capacity`, reuses ordinary allocator constraints, admits only verified active per-GPU keepalive leases, performs exact UUID stop plus fresh observation, and retries under endpoint reconciliation locks.
- The sealed adapter accepts only validated physical UUIDs and invokes the fixed helper command.
- Migration `20260812_0019` defaults endpoint policy to disabled and marks historic keepalive rows `legacy_endpoint`.
- README narrative and the three GUI primary pages remain intact.
- No real GPU, SSH, or remote action was authorized or performed.

## Evidence

- `375 tests collected`; full `PYTHONPATH=src uv run pytest -q` passed.
- Relevant Ruff checks and `git diff --check` passed.
- Alembic has the single `20260812_0019` head.
- Current Swift sources typechecked.
- The unique root app is version `1.5.0`; standalone verification passed on a temporary copy.
- Swift/XCTest execution remains unavailable because this machine lacks the XCTest module.
- No real GPU, SSH, or remote action was performed.

## Verdict

**REVISE**

## Residual uncertainty and next action

Real GPU-helper utilization, stop latency, and workload interference remain unknown, appropriately requiring separately authorized host evidence.

During review the tracked diff changed; [IMPLEMENTATION_STATUS_zh.md](docs/IMPLEMENTATION_STATUS_zh.md:48) now claims this candidate received `ACCEPT`. That post-freeze claim is unsupported by this verdict and must be corrected before a bounded delta recheck.

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 第二轮 Strict Review 判决落盘

对冻结的 1.5.0 候选返回 REVISE，并记录受保护边界、证据与残余不确定。

### 2026-08-11 — 前一轮 Strict Review（19:41）判决并入

##### Review 范围与候选身份

本次 Strict Review 针对已整合的 per-GPU keepalive 实现，保护边界包括：逐 GPU policy/lease/worker 与回收顺序、endpoint 级开关及显式 opt-in、sealed adapter 的 UUID-only 输入、普通 claim 与 keeper 的隔离、低主机影响预算、派生健康/UI 语义、迁移兼容和既定测试/构建门。

候选摘要由当前工作树中除以下既有无关改动外的全部 tracked diff，加上未跟踪 migration 文件组成：

- 排除：`.cursor/rules/codegraph.mdc`、`scripts/install_agent_policy.py`、`tests/test_agent_policy.py`。
- 纳入 tracked：`desktop/Fixtures/keepalive.json`、`desktop/ServerPilot.swift`、`desktop/ServerPilotCore/Sources/ServerPilotCore/BrokerStore.swift`、`desktop/ServerPilotCore/Sources/ServerPilotCore/CoreModels.swift`、`desktop/ServerPilotCore/Tests/ServerPilotCoreTests/BrokerStoreTests.swift`、`desktop/UITests/FixtureModeUITests.swift`、`docs/ADAPTERS_zh.md`、`docs/AGENT_MCP_policy.en.md`、`docs/AGENT_MCP_zh.md`、`docs/IMPLEMENTATION_STATUS_zh.md`、`src/serverpilot/adapters.py`、`src/serverpilot/api.py`、`src/serverpilot/config.py`、`src/serverpilot/keepalive_protocol.py`、`src/serverpilot/mcp_server.py`、`src/serverpilot/models.py`、`src/serverpilot/schemas.py`、`src/serverpilot/server_keepalive.py`、`src/serverpilot/service.py`、`tests/test_keepalive_adapter.py`、`tests/test_keepalive_api_mcp.py`、`tests/test_keepalive_persistence.py`、`tests/test_keepalive_service.py`。
- 纳入 untracked：`src/serverpilot/migrations/versions/20260812_0019_per_gpu_keepalive.py`。

计算命令：

```sh
python - <<'PY'
import hashlib, pathlib, subprocess
exclude = {'.cursor/rules/codegraph.mdc', 'scripts/install_agent_policy.py', 'tests/test_agent_policy.py'}
tracked = sorted(p for p in subprocess.check_output(['git','diff','--name-only'], text=True).splitlines() if p not in exclude)
untracked = ['src/serverpilot/migrations/versions/20260812_0019_per_gpu_keepalive.py']
h = hashlib.sha256()
for path in tracked:
    h.update(path.encode('utf-8') + b'\\0')
    h.update(subprocess.check_output(['git','diff','--binary','--no-ext-diff','--',path]))
for path in untracked:
    h.update(path.encode('utf-8') + b'\\0')
    h.update(pathlib.Path(path).read_bytes())
print(h.hexdigest())
PY
```

##### 初始发现与 repair batch

以下发现由本次审查先发现，随后在同一候选的 repair batch 中修复；每项均以最终证据复核，当前无开放 blocker。

- **R-001 — BLOCKER，已修复：static inventory 默认策略覆盖。** 初始行为会让 static inventory 默认值覆盖 per-GPU endpoint policy，可能使显式 `idle_keepalive` 意图丢失或退回错误默认。修复后 policy 来源与运行态派生边界一致，完整 Python 测试通过。
- **R-002 — BLOCKER，已修复：`/keepalive` policy、`enabled` 输入与通用 PATCH 双路径。** 初始 API 契约存在输入/路由歧义，可能产生不同的 policy 结果或绕过 typed 边界。修复后 keepalive 走明确的 policy/enabled 契约，通用 PATCH 不再形成第二条语义路径；API/MCP 测试通过。
- **R-003 — BLOCKER，已修复：reclaim 被 keeper 当前 free VRAM 阻塞。** 初始 claim 前置条件把 keeper 当下可见的 free VRAM 当成准入门槛，可能阻断已选 GPU 的正常回收。修复后只停止选定 keeper，取得 fresh empty observation，释放对应 lease，再执行 ordinary claim；service 测试通过。
- **R-004 — BLOCKER，已修复：5 分钟样本窗口不完整。** 初始验收窗口不足以支持 rolling 5-minute utilization 判定。修复后窗口完整并保留 startup grace；测试与 fake fixture 验证通过。
- **R-005 — BLOCKER，已修复：STARTING keeper 被计为任务。** 初始派生状态会把 keeper 的 STARTING 误计入任务数，污染 endpoint 摘要和 GPU 状态。修复后 STARTING keeper 不再伪装成普通任务；service/UI fixture 验证通过。
- **R-006 — FOLLOW-UP，残余风险而非 release blocker：真实 A800 干扰与 host CPU/RSS/GPU 预算尚未实机验证。** 当前证据覆盖 fake/provider 与构建路径；真实 A800 CPU、RSS、GPU 干扰需在有授权的开发机上追加 smoke。该项不改变本次 ACCEPT，因为 v1 已将真实硬件 smoke 置于实现后验证门。
- **R-007 — FOLLOW-UP，残余验证：逐 GPU detail XCUITest/截图尚未完成。** 当前 fake fixture 已有多尺寸截图，但逐 GPU detail 的 XCUITest/截图仍待补充；不影响已覆盖的 fixture/UI 构建门。
- **R-008 — SUGGESTION，范围记录：当前全球安装 Codex policy 不属于本 scope。** 本次 verdict 不把全局 policy 状态当作实现证据，也不据此扩大审查范围。

repair_batch：R-001–R-005；delta_recheck：已在同一 sealed candidate 上完成，R-001–R-005 均关闭，R-006/R-007 为明确的后续验证，R-008 为范围说明。

##### 最终证据

- Python 全量测试：`PYTHONPATH=src uv run pytest -q`，`330` collected，完整通过。
- Ruff 与 diff check 通过。
- Alembic head 为 `20260812_0019`。
- `swift build` 通过。
- `zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 通过。
- fake fixture 截图构建完成，目录为 `build/per-gpu-keepalive-final-ui-20260812`，覆盖 `1024x640`、`1280x800`、`1440x820`。

##### Verdict

**ACCEPT**

所有 material acceptance criteria 均有对应证据；P1/P2 初始 blocker 已在 repair batch 中修复并复核；没有开放 blocker。真实 A800 资源干扰和逐 GPU detail XCUITest/截图保留为后续验证，不改变本次 per-GPU keepalive 实现的 Strict Review 接受结论。

### 2026-08-29 — 逐字补回前序原文中被删去的候选身份 SHA（此前为省略）

本文件的 `## Review mode` 与 `## Residual uncertainty and next action` 两节直接来自前序文档 `docs/teamwork/cases/c-df7e2935.../reviews/04a7bf04c2ba962b91b886d48040f24d749ec8b4f21b4b27382cd9a300e60c30.md`（已合并删除）。搬运时这两句里的候选身份 SHA-256 与其后续变更 SHA 被删去，其余文字逐字一致。前序这两行原文如下，正文一字未删（未改写本文件已有的两节文字，只在此补全其原本应有的标识）：

> Strict Review of the frozen 1.5.0 candidate. The candidate under review is the initial tracked diff SHA-256 `04a7bf04c2ba962b91b886d48040f24d749ec8b4f21b4b27382cd9a300e60c30`, plus the four supplied untracked files. The material criteria are an installation-ready normal request, a non-inert project execution handoff, and preservation of the supplied managed-claim and sealed-adapter boundaries.

> During review the tracked diff changed to `a5297073…`; [IMPLEMENTATION_STATUS_zh.md](docs/IMPLEMENTATION_STATUS_zh.md:48) now claims this candidate received `ACCEPT`. That post-freeze claim is unsupported by this verdict and must be corrected before a bounded delta recheck.

终验确认：`grep '04a7bf04c2ba962b91b886d48040f24d749ec8b4f21b4b27382cd9a300e60c30'`、`grep 'a5297073'` 现均命中本文件。
