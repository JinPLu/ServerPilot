---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-11
---

# Plan: Gpu Broker Keepalive Extension

## Current synthesis

- Subject identity: 把 keepalive 做成可选 endpoint capability + 显式 `gpu_set_keepalive(enabled)` 原语这一选定结果
- 本文件只定义实现顺序、拥有者、验证与停止门；runtime code 尚未实施。

## 目标与已选方向

把 keepalive 做成可选的 endpoint capability：人类端只看到一个启停按钮和 `KEEPALIVE` 状态，Agent 通过一个显式 `gpu_set_keepalive(enabled)` 自己编排“关闭 → claim → release → 按需开启”。ServerPilot 不监听 claim、不自动抢占、不自动恢复、不引入后台 controller；collector 仍只读，keepalive adapter 只管理自己登记的 guard。首版只支持 whole endpoint，并复用现有 Lease/LeaseResource/审计/幂等约束。

### 交付边界

- 默认关闭；一次性 admin opt-in 后，endpoint 才暴露 `endpoint_keepalive` capability。
- 只有 collector fresh observation 绑定 guard 后才投影 `KEEPALIVE`；adapter 返回值不是事实来源。
- adapter 调用永远在数据库事务外；正常 workload、foreign process、维护/冲突/陈旧遥测都不能被停止或绕过。
- 不实施 runtime code；本计划只定义实现顺序、拥有者、验证和停止门。

## 工作单元

### W0 — rename/UI baseline green（硬前置）

- 范围：完成当前 package rename 和已有桌面 UI 优化，冻结路径地图；后续只使用 `src/serverpilot/`、`desktop/ServerPilotCore/`、`desktop/ServerPilot.swift`。
- 保护：不改变 Lease、collector、API、MCP 语义，不提前放入 keepalive runtime。
- 验证：由 W0 从 `pyproject.toml` 与 `desktop/ServerPilotCore/Package.swift` 读取并确认实际包命令、Swift 构建命令和 UI 测试命令；未知命令不得猜测。所有后续单元复用该已确认命令集。
- 退出：rename、导入路径、桌面基线和现有测试均 green；否则停止全部 keepalive 工作。

### W1 — 最小持久化与内部归属

- Owner：后端领域/持久化。
- 文件：`src/serverpilot/models.py`、`src/serverpilot/database.py`、`src/serverpilot/schemas.py`、`src/serverpilot/migrations/versions/<next_revision>_keepalive.py`、keepalive persistence tests。
- 变更：`Lease.kind` 仅允许 `workload`（默认）和 `keepalive`；endpoint 增加可空 sealed `keepalive_adapter_id`；固定 system actor/project 可幂等取得；内部 request/lease 对普通 Agent/API 隐藏，普通配额、列表和历史保持兼容。
- 约束：默认关闭；历史 Lease 一律 `workload`；keepalive 租约覆盖完整 endpoint，复用既有 active `LeaseResource` 唯一约束，不增加锁表/fence/reservation 表。
- 退出：临时数据库 migration、旧 fixture、配额/列表兼容和正常 Lease 与 keepalive Lease 的整端点互斥测试通过。

### W2 — sealed adapter 与服务器协议

- Owner：adapter/server-script。
- 文件：`src/serverpilot/adapters.py`、`src/serverpilot/keepalive_protocol.py`、固定的 `serverpilot-keepalive` server entrypoint、adapter tests。
- 变更：registry 只登记 `endpoint_keepalive` capability；对外只有一个 typed `set_enabled` 语义（实现为固定 start/stop）；远端入口接收 code-owned typed JSON，拒绝 shell、路径、环境变量、PID、GPU selector 和 Agent 自定义参数；whole endpoint。
- 安全：stop 只能处理入口自身登记的 guard；未知 adapter/响应/受控标识或 foreign process 一律 fail closed；不得把 `raw-ssh` 观察 adapter 变成任意命令执行器；collector 仍纯只读。
- 退出：fake adapter 覆盖 start/stop 幂等、超时/未知响应、命令注入不可达和 foreign PID 拒绝；打包产物包含固定 entrypoint。

### W3 — BrokerService 显式启停与投影

- Owner：调度领域。
- 文件：`src/serverpilot/service.py`、必要的 keepalive domain helper、service tests。
- 变更：只增加 `begin/confirm/finalize` 领域边界和唯一启停入口；启用时 fresh、健康、整端点无普通 Lease/预约冲突/foreign process 后创建 system `Lease(kind=keepalive)`，事务外执行 adapter，再由 fresh collector 确认并绑定 guard；确认后才续该 keepalive Lease TTL。停用同理：只停止自己的 keepalive Lease，fresh collector 确认 guard 消失后释放。
- 投影：`_gpu_state` 仅在“keepalive Lease 已绑定且 collector 观察到 guard”时显示 `KEEPALIVE`；未确认时保持真实现有状态。`gpu_claim`、`gpu_release`、队列分配和 reconcile 不自动启停，Agent 必须显式关闭。
- 退出：fake collector 覆盖并发启停、幂等 key、未确认、遥测过期、foreign process、adapter 超时、服务重启及只续自身 TTL；数据库事务不包含 adapter 调用。

### W4 — REST/MCP 对 Agent 暴露

- Owner：API/MCP。
- 文件：`src/serverpilot/api.py`、`src/serverpilot/schemas.py`、实际 MCP 入口 `src/serverpilot/mcp_server.py`、API/MCP tests。
- 变更：新增 `POST /api/v1/endpoints/{id}/keepalive`，body 严格为 `{"enabled": true|false}`；沿用 mutation idempotency；endpoint read model 输出 capability 与真实 `KEEPALIVE`。MCP 仅新增 `gpu_set_keepalive`；无 CLI v1、无 pause/resume/force/kill、无 Agent 提供的 adapter 配置。
- 权限：一次性 admin opt-in；运行时使用现有 project-visible Agent auth；system actor/project 不可由调用方伪造或直接选择。
- 编排：API 在事务外调用 adapter，随后 fresh collect，再调用 Service finalize；不让 API/MCP 复制领域规则。
- 退出：无 capability、存在正常/foreign workload、重复 key、adapter 失败时均有明确拒绝；CLI help/命令表无新增 keepalive 命令。

### W5 — 人类端单按钮与状态

- Owner：桌面 UI；以 W0 冻结后的真实路径为准。
- 文件：`CoreModels.swift`、`BrokerStore.swift`、`desktop/ServerPilot.swift` 及对应 fixtures/UITests（具体嵌套路径由 W0 确认）。
- 变更：endpoint 详情 action 区仅在 capability 存在时显示一个“开始占卡/停止占卡”按钮；操作中只禁用按钮并显示进行中，完成后以服务端刷新结果为准；资源表把 `KEEPALIVE` 渲染为正常资源状态而非 error；system actor/project 对用户隐藏，最多显示 ServerPilot 保活。
- 保护：不新增设置页、策略页、定时器、dashboard；保留现有全宽表格和 UI 优化；普通 `BUSY`/`CONFLICT` 不伪装为 `KEEPALIVE`。
- 退出：fixture 覆盖 capability 有/无、启用/停止中、成功/失败、KEEPALIVE 表格和 system 归属隐藏；桌面构建及 W0 确认的 UI tests green。

### W6 — 文档、验证、上线与回滚

- Owner：集成/文档。
- 文件：`README.md`、`docs/ADAPTERS_zh.md`、`docs/AGENT_MCP_zh.md`、`docs/IMPLEMENTATION_STATUS_zh.md`、end-to-end tests。
- 文档必须说明：这是可选 endpoint capability 和显式 Agent 编排，不是自动调度/抢占；whole endpoint；adapter sealed；collector 只读；Agent 必须先关闭再 claim，release 后按需开启；默认关闭。
- 验证顺序：W0 的实际命令 → W1 migration/兼容 → W2 sealed adapter → W3 service/collector 证据 → W4 REST/MCP/idempotency → W5 fixture UI/构建 → W6 全量测试、lint、打包。真实开发机 smoke 另行取得明确授权，不以 fake-provider 结果代替。
- 上线：先只给一台明确授权的开发机配置 adapter，观察启用→KEEPALIVE→停用→AVAILABLE→正常 claim。
- 回滚：显式停用 → fresh collect 确认 guard 消失 → system Lease 释放 → 移除 endpoint adapter opt-in → 再回退应用版本；不直接改真实 `state/`、SQLite 或 SSH。

## 依赖与并行

依赖图：`W0 → (W1 ∥ W2) → W3 → W4 → W5 → W6`。W1/W2 只有在 W0 的路径和基线冻结后才可开始；W3 需要两者的类型/协议契约；W4、W5 不并行修改同一公共 read model；W6 是集成门。

## Stop / replan 条件

- W0 rename/UI 基线未 green，或无法从 `pyproject.toml`/`Package.swift` 确认实际命令和真实路径。
- 需要任意 shell、脚本路径、密钥、PID、GPU 参数，或需要停止普通/foreign process；这超出 sealed adapter 边界。
- collector 无法可靠区分受控 guard 与普通进程，或不能以 fresh observation 证明启停结果；不得显示 `KEEPALIVE`、续 TTL 或释放资源。
- 无法用既有 active `LeaseResource` 原子覆盖 whole endpoint，或 migration 不能增量兼容历史 Lease/列表/配额。
- 需求重新变为 claim 自动暂停、自动恢复、per-GPU、抢占、后台 controller 或新的通用优先级框架；停止本计划并另开设计。
- 测试需要接触真实 `state/`、生产 SQLite、裸 SSH 或真实 GPU，或真实 smoke 未取得另行授权。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 计划落盘

记录 keepalive 可选 endpoint capability 的工作单元 W0–W6、依赖与并行图，以及 Stop / replan 条件。
