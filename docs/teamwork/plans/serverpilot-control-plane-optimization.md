---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-09-03
---

# Plan: ServerPilot Control Plane Optimization 2026 08 12

## Current synthesis

- Subject identity: 把 GUI/CLI/MCP 的正常资源路径收口为单一 loopback 控制面（立即成功或立即 `no_capacity`）这一选定结果
- 本计划只描述执行设计，不执行源代码、README、设计系统、GUI 修改。

## Intended result

在不重做 GUI 或用户叙述的前提下，把 ServerPilot 的正常资源路径收口为一个简单、可验证的 loopback 控制面：GUI、CLI、MCP 都经 REST 进入同一个领域服务；申请是立即成功或立即返回 `no_capacity`；项目自己的执行路径负责启动任务，随后绑定已观察到的 workload，最后释放。ServerPilot 不新增总调度器、后台等待器或任务启动/停止器。

本计划只描述执行设计，不执行源代码、README、设计系统、GUI 或 Storyboard 修改。当前 per-GPU keepalive dirty change 是独立前置依赖；本计划不回退、覆盖或把它改造成通用抢占。

## Selected direction and protected boundaries

- 保留 [README.md](README.md) 的生命周期承诺、[docs/DESIGN_SYSTEM.md](docs/DESIGN_SYSTEM.md) 的信息架构和现有 macOS App 的三个一级页面「服务器 / 使用情况 / 设置」。现有布局、颜色、表格、可访问性与用户已确认的叙述均是保护边界。
- ServerPilot 只拥有 loopback REST/domain 的准入、lease、bind、release、审计和状态投影；GUI、CLI、MCP 不直连 SQLite、SSH 或远端运行时。租约只协调归属，不授权启动、停止或抢占项目任务。
- 正常裸机申请只有一个即时语义：成功返回服务端选定的 `lease.resources[]`（含 `cuda_visible_devices`），无容量原子返回 `no_capacity`，不创建等待 request、不轮询、不推断容量。
- Collector 仍只执行固定只读探针；stale、unknown、unmanaged、冲突、维护和无法证明的资源继续 fail closed。CPU/内存与外部 SchedulerTarget 保持各自合同，不并入裸机 GPU 生命周期。
- accepted per-GPU keepalive 设计（endpoint-level opt-in、逐卡 worker/lease、只回收被选中的 keepalive GPU、managed-claim guarantee）保持不变；其当前 dirty 迁移和实现先单独冻结并验证。
- 任何可见 queue wording、表单字段、状态文案或历史兼容行为都不是本计划的隐含授权；必须经过用户确认。历史数据库、AllocationRequest 数据和审计证据不可删除或重写。根目录只保留一个 `ServerPilot.app`。

## Ordered work

### 0. Freeze the current per-GPU keepalive baseline

- **Owner:** 当前 keepalive 实现 owner；Root 负责集成门。
- **Target:** 当前 dirty 的 `src/serverpilot/` keepalive、service/API/MCP/models/schemas 模块，`20260812_0019_per_gpu_keepalive.py`，桌面 Core/Swift 改动及其 fixtures/tests。
- **Outcome:** 记录 dirty worktree、迁移单头和现有用户改动；将 per-GPU keepalive 作为一个独立可验证变更冻结。不得在此阶段夹带即时申请重构、GUI copy 改动或 Storyboard 变更。
- **Dependencies:** 无；后续所有步骤依赖本基线。
- **Verification:** 临时数据库 migration/旧 fixture/keepalive focused tests、Python/Ruff、桌面核心测试；如基线包含桌面改动，使用根目录 build/verify 命令确认唯一 App。只使用 fake provider/临时 DB，不连接真实 GPU。
- **Stop:** dirty 文件归属不清、迁移非单头、基线测试失败，或 keepalive 语义需要扩大为通用抢占时停止并回到 Root。

### 1. Obtain the user approval gate

- **Owner:** 用户 + Root。
- **Target:** README 与设计系统保护条款、`desktop/ServerPilot.swift`、`desktop/ResourceUsageDashboard.swift`、`BrokerStore`/fixtures，以及历史 HTTP/Python request/read helpers。
- **Outcome:** 在任何可见变更前取得两项明确决定：
  1. 是否允许把仍写着等待/排队的 GUI 文案或展示改为与 README 一致的即时 `no_capacity` 事实；
  2. 是否继续支持历史 request/read 调用面（建议保留只读历史兼容，不删除表或审计）。
  未获批准时只允许进行不可见的内部收口，并且不得宣称 GUI 已完成无队列叙述统一。
- **Dependencies:** Step 0 基线证据。
- **Verification:** 将每项决定记录为明确的 accepted/拒绝边界；冻结现有 GUI 截图、可访问性和文案基线，避免执行阶段凭猜测改写用户投入的叙述。
- **Stop:** 用户未批准可见文案、字段、页面或兼容性变化；发现实际外部调用者依赖 request/poll 语义；或需要重新设计三页 GUI。

### 2. Unify the immediate-claim core

- **Owner:** 后端领域/API/MCP/CLI owner；`service.py` 保持唯一调度与租约规则 owner。
- **Target:** `src/serverpilot/service.py`、`api.py`、`client.py`（若存在）、`mcp_server.py`、`cli.py`，以及新增即时申请契约测试。
- **Outcome:** 建立一个内部 immediate-claim 路径供正常 claim 与 profile claim 共用：服务端按现状投影选择资源；容量不足在同一事务边界内返回 `no_capacity`，不留下 request、lease 或“等待申请”审计；成功只返回 lease 与服务端资源选择。幂等 mutation 使用 caller-stable key。bind 只接受已观察 workload，release 不停止任务。
- **Protected behavior:** keepalive 只允许在已验证的选中 GPU 上受控回收，其他 keeper 不受影响；不引入通用抢占、后台 controller、容量减用量推算、自动 SSH 或 workload 启停。
- **Dependencies:** Steps 0–1；无需新增持久化表或破坏性 migration。
- **Verification:** 临时 DB/fake collector/provider 覆盖成功 claim、`no_capacity` 零持久化、副本 idempotency、profile claim、bind/release、释放后不自动排队、keepalive targeted reclaim 与 fail-closed 分支。
- **Stop:** 需要改写历史数据、增加等待/轮询状态、让 ServerPilot 启停任务，或无法证明 no-capacity 不产生残留状态。

### 3. Put desktop transport behind one typed client

- **Owner:** 桌面 Core owner；Root 负责 UI 保护边界。
- **Target:** `desktop/ServerPilotCore/Sources/ServerPilotCore/BrokerStore.swift`、`CoreModels.swift`；必要时新增单一 `ServerPilotCommandClient.swift`；对应 fake-client tests。
- **Outcome:** 把 raw JSON、路径拼接、错误解析、revision 和 mutation idempotency 收口到 typed client。每次用户命令生成稳定 key；安全重试复用同一 key；`no_capacity` 是最终结果，不自动等待、轮询或重复提交。旧快照只能展示，不能发起 mutation；成功后按服务端 revision 刷新。
- **Protected behavior:** 不改变三页结构、布局、颜色、字段、可访问性、用户叙述或 README/DESIGN_SYSTEM；未获 Step 1 批准时不改任何可见 queue copy。
- **Dependencies:** Step 2 的响应/错误合同；与 Step 4 不并行修改同一 read model。
- **Verification:** fake client 覆盖成功、409/`no_capacity`、超时后同 key 重试、旧快照 mutation 拒绝、revision 刷新和具体失败原因；桌面核心测试通过。
- **Stop:** typed client 需要绕过 REST、直接读 SQLite/SSH，或为了减少阻塞而放宽 fail-closed 准入。

### 4. Isolate legacy queue compatibility

- **Owner:** 后端与桌面 owner；Root 执行兼容性 gate。
- **Target:** `service.py`、`api.py`、`mcp_server.py`、`cli.py`、桌面 read models/fixtures，以及 request/poll 相关 tests。
- **Outcome:** 按 Step 1 决定处理旧路径：若获准移除，删除正常流程中不可达的 request 创建、等待和轮询；若需兼容，保留只读历史投影与明确 legacy dispatch，正常 claim 不再产生 pending 行。内部名称和帮助文本不应继续暗示新工作流会排队，但可见 copy 仍受用户批准 gate 约束。
- **Protected behavior:** 不删除 `AllocationRequest` 表、历史 request/lease/audit；不制造“兼容”假数据；旧调用无法证明即时语义时明确失败或走只读路径。
- **Dependencies:** Steps 1–3；不得与 typed read model 同时改同一字段。
- **Verification:** 覆盖 legacy read、不可达 create/poll、历史 pending 行、即时 claim 与旧客户端并存；确认 no-capacity 不创建兼容 request。
- **Stop:** 发现真实外部使用者依赖写入或轮询、兼容层会混入新 score/status，或需要破坏性 migration；停下并回到用户决定。

### 5. Verify and build the single control plane

- **Owner:** 集成/验证 owner；Root 负责发布判定。
- **Target:** 全部相关 tests、Ruff、迁移检查、根目录 `ServerPilot.app`、必要的 fixture evidence。
- **Outcome:** 只有在行为和保护边界均有直接证据后，才更新事实状态或交付本地 App。验证 GUI 仍为 README 所述三页、App 不聊天/不执行任务、刷新只读控制面、no-capacity 无排队、keepalive/collector/外部调度失败仍 fail closed。
- **Verification:** `uv run pytest`、`uv run ruff check src tests`、Alembic 单头；桌面核心测试；若桌面有任何改动，运行 `zsh desktop/build-macos-app.sh`、`zsh desktop/verify-macos-app.sh`，并确认仓库内只有根目录这一份 `ServerPilot.app`。测试只用临时 DB/fake provider，不连接真实 GPU、裸 SSH 或生产 `state/`。
- **Rollback:** 保留 Step 0 的 dirty preimage；失败时回到最近一个已验证的服务/App 组合，不删除历史数据，不混入或回退当前独立 keepalive migration。
- **Stop:** 测试、build/verify、迁移、App 唯一性或文案/兼容性批准证据缺失；任何结果只能作为未完成交付返回。

## Dependency and verification relationship

```text
per-GPU keepalive 基线冻结
            ↓
用户批准可见 queue/legacy 边界
            ↓
统一 immediate claim core
            ↓
桌面 typed command client
            ↓
legacy queue 隔离或只读兼容
            ↓
全量测试 + 根目录 App build/verify
```

Step 0 是所有后续工作的硬前置；Step 1 同时守住 GUI 叙述和历史兼容的用户决定。Step 2 先确立唯一后端语义，Step 3 才能稳定 typed client，Step 4 依赖两者并隔离旧路径，Step 5 是集成与发布门。整个计划不新增调度器、队列、后台等待器或新的控制面。

## Stop and replan conditions

- 用户改变 README、设计系统、三页 GUI、可见文案或 Agent/协调边界；暂停并重新取得决定。
- 当前 per-GPU keepalive dirty change 无法形成可验证基线，或被要求与本计划混合重写。
- 需要 ServerPilot 启动/停止/抢占用户任务、绕过 loopback REST、推断容量、使用裸 SSH/SQLite，或引入新的 allocator/controller。
- `no_capacity` 不能成为一次申请的终点，或旧 request/poll 语义会继续进入正常流程且用户未批准兼容方案。
- 需要删除或重写历史 AllocationRequest、lease、audit，或触及 Storyboard。
- 无法在临时 DB/fake provider 和本机桌面测试中取得直接证据，或根目录唯一 App/build/verify 不能通过。

## Handoff

这是 ServerPilot-only 的执行计划，不是实现完成声明。当前 GUI、README 与设计系统保持不变；可见队列叙述和 legacy compatibility 仍由用户批准 gate 决定。实现阶段完成后必须分别报告：per-GPU keepalive 基线、immediate-claim contract、typed desktop client、legacy compatibility 选择及 build/verify 证据。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 计划落盘

记录 loopback 控制面收口的选定方向、保护边界与执行设计。

### 2026-08-29 — 补回迁移时丢失的 Handoff 非声明段

迁移进本文件时，原案卷计划的整个 `## Handoff` 段落丢失，其中那条 non-claim（「这是 ServerPilot-only 的执行计划，不是实现完成声明」以及实现阶段完成后必须分别报告的四项：per-GPU keepalive 基线、immediate-claim contract、typed desktop client、legacy compatibility 选择及 build/verify 证据）随之消失，计划读起来像是已交付声明。已按案卷原文逐字补回该段，位置照原文放在 `## Stop and replan conditions` 之后、`## History` 之前。已有 History 条目与其余正文未改。

### 2026-09-03 — 用户解除「不删除 AllocationRequest 表」这条保护边界

本计划的 Protected behavior 写着「不删除 `AllocationRequest` 表、历史 request/lease/audit」，
Stop-and-replan 也把「需要删除或重写历史 AllocationRequest」列为停止条件。用户在
2026-09-03 的连接失败排查会话中显式解除了这一条：线上 1,776 行 allocation_requests 里
从未出现过 `QUEUED` 状态，一次申请却仍写一条 request 行再由 `_allocate_queued` 发租约，
于是一份申请长期有两个身份。选定做法是把 `task_ref` / `purpose` / `constraints_json` /
`duration_seconds` 折进 `leases` 并回填，然后 drop `leases.request_id` 与
`allocation_requests` 表；480 条 `request.created` 审计事件保留历史。

解除的只有这一条。历史 lease 与 audit 不删、不制造兼容假数据、根目录唯一
`ServerPilot.app` 这三条保护边界不变。执行细节见
[观测层重做与项目收口](observation-layer-and-consolidation.md)，本计划所描述的
「立即成功或立即 `no_capacity`」方向未变，仍未执行。
