# ServerPilot 4.0 大版本计划：Agent 契约与 macOS App 重设计

## Context

MCP 和 UI 的形状定得较早，后来的问题都靠补丁解决。本机数据显示：约 1,200 条工作租约里，1,166 条来自共用身份 `agent`，人工发起只有 5 条。所以 Agent 是主要使用者，人主要负责监控和纠错，但现有设计没有按这个现实来。

核心问题：

- **身份**：所有 MCP 客户端都写成 `actor=agent`、`project=agent`，代码上可以互相心跳和释放。
- **成本**：`gpu_status` 约 70k 字符（约 20k token），心跳也要付这个代价。
- **恢复**：直连租约丢失上下文后拿不回 CUDA 选择器。
- **语义**：直连时 `duration_seconds` 必填但不生效；8 张卡只用 2 张时，其余卡会被静默回收，手里的选择器随之过期。
- **UI**：失败显示成成功、同一张卡有五种叫法、Slurm 节点行是死路、没有主动提醒。

目标：Agent 用很少的 token 就能完成申请、进入、监控、改卡、恢复和归还；人打开 App 或看菜单栏，就知道现在要处理什么。

## 已确认的决定

| 主题 | 决定 |
|---|---|
| 用户 | 单人加多个 Agent；继续只监听本机，不做账号 |
| MCP | 按任务会话重做；不含 Agent 间消息 |
| 等卡 | 不排队；给拒绝原因、替代方案、重试时间和低成本轮询 |
| App | 监控纠错台加主动提醒，保持单窗口，不追求覆盖全部 CLI |
| 边界 | 软隔离：只操作自己的租约，可按项目加任务显式接管，并留审计 |
| 改卡 | 加卡、减卡都在原租约上，可扩到其他服务器 |
| 空卡 | Agent 可用 `keep_idle` 推迟回收，最多 3 个回收窗口，留审计 |
| 提醒 | 任务受影响、服务器异常、资源闲置、有卡可用四类全做 |
| 有卡通知 | 任何组从无卡变有卡都提醒，带稳定时间和频率限制 |
| App 运行 | 关窗留菜单栏，不登录自启 |
| 切换 | 4.0 一次切换，数据和活跃租约全部保留，不留旧 MCP 兼容层 |
| 分支 | 在独立 `v4` 分支开发，完成后整体合入 `master` |

## 默认取舍（批准即生效）

- **Slurm 分配不支持改卡**：整份 provider 分配无法安全伸缩，4.0 返回 `resize_unsupported`，`next` 指向用新任务申请。这是“在原租约上改卡”的一个例外。
- **一份租约的形态**：要么全是直连卡，要么恰好一个 provider 分配，不混用。
- **跨服务器加卡**：默认只在已有服务器上加，扩到其他服务器或服务器组必须显式指定，每次落一台。
- **运行中进程看不到新卡**：加卡只改账本，返回 `restart_required=true`，由 Agent 决定怎么重启。
- **任务唯一**：`(project, task)` 活动期间唯一，重复申请返回原租约；卡数不同返回 `task_already_held`。
- **时长**：`duration_seconds` 保持必填，含义改为预计时长，用于 ETA 和超时提醒，永不据此回收。
- **通知失败时**：ad hoc 签名下如果系统通知不可用，就以菜单栏徽章和提醒页为主通道，不购买 Developer ID。

## 设计

### 1. Agent 身份

- **client**：归一 MCP `clientInfo.name`，作为审计标签，不是凭证。
- **session**：每个 MCP 进程一个随机 ID，首次工具调用时懒注册到新表 `agent_sessions`。后台 ping 只表示在线，不进入 `_lease_liveness_age_seconds`。
- **project**：依次取显式参数、`roots[0]`、`SERVERPILOT_PROJECT`、非 HOME 的 cwd，再上溯到 git 顶层取 slug。都没有则返回 `project_required`。历史项目名已漂移（`Storyboard`、`storyboard`、`Storyboard / MSPC`），所以不让 Agent 自由命名。
- **接管**：更新 `leases.owner_session_id`、`actor_id` 和 `resource_allocations.actor_id`，写审计。旧 session 再操作得 `lease_taken_over`。
- **老租约**：历史不改写。活跃老租约显示为可认领，按 `lease_id` 认领；迁移时重置 `last_heartbeat_at` 和 `idle_since`，避免升级窗口内被回收。

### 2. MCP 工具（7 个）

| 工具 | 作用 | 响应上限 |
|---|---|---|
| `gpu_status` | 只读；`view=summary/capacity/servers/nodes`，`if_changed` | summary 1,500 字符，capacity 300，servers 6k，nodes 12k（必须带 `server_id`） |
| `gpu_apply` | 申请；返回 ssh、cwd、env、卡位和 `heartbeat_before` | 8 卡 1,200 |
| `gpu_lease` | 心跳加查询；支持 `keep_idle`、`include=[entry,telemetry]` | 600，含遥测 2,500 |
| `gpu_resize` | `add_count`、`remove_gpu_ids`、`expected_epoch` | 1,200 |
| `gpu_release` | 带 `outcome=completed/failed/abandoned` | 200 |
| `gpu_take_over` | 按任务或租约接管 | 同 `gpu_apply` |
| `gpu_server` | 合并 add 和 update 两个工具 | schema 2,200 |

- **返回信封**：成功 `{ok:true,...}`；拒绝或失败 `{ok:false,error:{code,message,retryable,retry_after_seconds?,next?,details?}}`。`no_capacity` 等预期答复不算工具错误。`client.py:119` 须保留 `details`。
- **输出模型**：在 `agent_contract.py` 定义强类型输出，替代 `additionalProperties: true`；文本用最小化 JSON。
- **`no_capacity`**：`details` 含 `reason`、`needed`、`largest_block`、`excluded`、`alternatives[]`、`eta`，另给带抖动的 `retry_after_seconds`（60–1800 秒）。
- **ETA**：用 `issued_at + 比值 × 声明时长` 累计到卡够，比值取近 30 天完成时长分位。样本少于 50 时只给声明值；必须依赖已超期持有者时 `eta=null`，不编造。
- **Slurm 进入**：`AllocationEntry` 增加 `exec_prefix`（如 `srun --jobid --overlap`）给非交互 Agent。`verified=false` 时先探测，失败返回 `entry_unusable`，不换分配。
- **版本**：每次 MCP 调用带版本头，不匹配返回 `restart_client`；已安装的规则块带版本标记，`policy --check` 能发现旧规则。

### 3. 改卡与回收

- **数据模型**：
  - `lease_resources` 增加 `added_at`、`release_reason`、`released_by`，卡加回时复用旧行。
  - `leases` 增加 `resource_epoch` 和 `agent_seen_audit_id`；卡的事实只看 `lease_resources`，`constraints_json` 定为原始申请。
  - commitment 同步收进 `_sync_direct_allocations`。
- **事务**：add 全有或全无；add 和 remove 同一事务。占卡停止是唯一的事务外步骤，失败不改租约，沿用 claim/reassign 的端点锁和回收（`api.py:908`、`:1395-1430`）。
- **减卡**：有进程的卡整体拒绝 `card_in_use`；人工修正继续走 `_manual_release_verdict`。
- **空闲回收**：
  - `working` 改为按服务器判定（`service.py:8046`），避免 A 机在跑时，B 机准备阶段的卡失去保护。
  - 每张卡给出 `reclaim_at`，并进入 `at_risk`。
  - `keep_idle` 最多推迟 3 个回收窗口。
  - 回收后写原因并递增 epoch，Agent 下次心跳经 `changes[]` 得知。
- **必改点**：
  - `_lease_dict`（`:2135`）：一张卡缺失会清空所有服务器入口。
  - `:1143`、`:7594`：按端点操作却结算整份租约。
  - `:6661-6681`：pin 分组主机被拒，resize 须绕过。
  - `_project_can_allocate`（`:6110`）：并发上限会拦加卡，改为只查 quota。
  - `:1840-1846`：去重按 actor，改按 project + task。
  - `_can_manage_lease`（`:989`）：改按 session。
  - `mcp_server.py:1068`：删除单服务器平铺。

### 4. REST 与词表

- 新增 `/api/v2`；App 和 CLI 迁完后删除 v1，用测试断言只剩 `/health/*`。
- 词表写在 `agent_contract.py`，生成文档和 `Vocabulary.generated.swift`，由 `policy --check` 校验。
- 服务端计算唯一 `card_status`，GUI 和 MCP 共用：

| token | GUI 用词 | 来源 |
|---|---|---|
| `free` | 空闲（占卡中也可分配） | AVAILABLE、KEEPALIVE |
| `in_use` | 运行中 | RUNNING_MANAGED |
| `held_idle` / `at_risk` | 已分配未运行 / 即将回收 | 持有无进程 |
| `foreign` | 外部进程 | BUSY_UNMANAGED |
| `conflict` | 归属冲突 | CONFLICT、ORPHANED_BUSY |
| `unknown` | 数据过期 | UNKNOWN_* |
| `fault` | 异常 | UNHEALTHY、ERROR |
| `provider` | 调度器管理 | SCHEDULER_MANAGED |

- 动作和对象词固定为：申请、归还、服务器、任务。
- CLI 裁剪：删除 `lease activate/renew/bind/bind-observed`；`import-servers`、`history` 无使用证据，保留待确认；`collect once` 是约定的维护入口，保留。

### 5. 提醒

规则在 daemon 里运行，紧跟 `_reconcile_leases`，这样 App 没开时也不丢事件。纯规则放新文件 `alerts.py`，写入继续用 `_upsert_alert`。现有 `alerts` 表增加 `category`、`dedupe_key`、`state`、`pending_since`、`notified_at`、`lease_id`、`project_id`、`task_ref`、`payload_json`。

| 类别 | 触发 | 防抖 |
|---|---|---|
| 任务受影响 | 卡进入回收窗口、已回收、分配到期前 30 分钟、unconfirmed ≥60 秒 | 按租约去重 |
| 服务器异常 | 离线 ≥5 分钟；认证失败或需要 TOTP 立即提醒 | 连续 2 次成功才解除 |
| 资源闲置 | 持有无进程；占卡异常 ≥5 分钟；超过 2 倍声明时长 | 超时只进提醒页 |
| 有卡可用 | 任何组从 0 变为有卡；陈旧或未知数据不算 | 稳定 120 秒，同组 30 分钟一次，60 秒内多组合并 |

- 投递用轮询，不做 SSE：`/pulse` 约 200 字节，窗口可见时 3 秒、后台 15 秒，revision 变化才拉 `/state`。
- App 投递后写 `notified_at`。关闭期间积压的提醒合成一条摘要，超过 24 小时的只进提醒页。
- App 入口是 AppKit，用 `NSStatusItem` + `NSPopover`，不用 `MenuBarExtra`；`applicationShouldTerminateAfterLastWindowClosed` 改为 false；构建脚本加 `-framework UserNotifications`。
- 通知放在 `AlertNotifier` 协议之后，不进 Core 测试。

### 6. macOS 信息架构

| 页面 | 回答的问题 |
|---|---|
| 总览 | 现在有什么要我处理；告警和全部卡的点阵 |
| 服务器 | 每张卡的状态和使用者；服务器行、节点行都能进详情 |
| 任务 | 每个 Agent 在做什么、哪里有风险；可归还、改派、认领 |
| 提醒 | 历史和确认 |
| 设置 | 通知、采集、MCP 接入状态 |

- 身份显示为“Claude · 项目 · 任务”，修改 `docs/DESIGN_SYSTEM.md:9`、`:196`，仍不做消息功能。
- 人工申请降为任务页的次级入口，申请结果必须显示 `CUDA_VISIBLE_DEVICES`。
- 新增 `FeedbackCenter`，统一成功、被拒（附建议）、失败、待确认四种结果；一个错误只在一处显示，不被 reload 清除。
- 连续 2 次失败才转为数据过期，只禁用依赖新鲜数据的操作。“连接已中断”改为说明本机服务状态。
- 修复已知缺陷：`GPUDetailSheet` 不可达、过滤与状态词错位、`notice` 不清除、分配命令无复制按钮、占卡操作错误被吞。
- 拆分 `ServersScreen`、`ServerDetail`、`ResourceUsageDashboard`、`AppShell`（独立 StatusItem）；`BrokerStore` 拆为 Snapshot、Mutation、Alert。

## 实施阶段

全部在 `v4` 分支进行，每阶段独立提交并通过测试。

| 阶段 | 内容 | 依赖 | 规模 |
|---|---|---|---|
| P0 | 三个客户端的 clientInfo/roots/cwd 探针、ad hoc 通知实验、词表定稿 | 无 | S |
| P1 | 迁移 0041：sessions、owner、epoch、alerts 扩展 | P0 | M |
| P2 | 按服务器判定回收、按服务器结算、卡级原因、`keep_idle`、提醒引擎 | P1 | M |
| P3 | `/api/v2`：resize、take-over、结构化 `no_capacity`、ETA、`/pulse` | P2 | L |
| P4 | MCP 切换、重新生成契约和规则、版本标记 | P3 | L |
| P5a | App 反馈模型、Store 拆分、词表 | P1，可与 P2–P4 并行 | L |
| P5b | 新页面和逐卡视图 | P3 | L |
| P5c | 菜单栏、系统通知、关窗常驻 | P0、P2 | M |
| P6 | 删除 v1 和废弃 CLI；更新 README、设计文档、`UPGRADE_CHECKLIST_zh.md`、`AGENT_MCP_zh.md`、`IMPLEMENTATION_STATUS_zh.md`、两份 CHANGELOG | P4、P5 | M |
| P7 | 实机验证、合入 `master`、正式发布 | P6 | S |

P7 的实机验证、推送、合入和发布都单独向用户确认。`v4` 期间本机继续运行 3.1.0，不升级、不连接真实 GPU。

## 关键文件

- `src/serverpilot/agent_contract.py`：契约、输出模型、词表的唯一来源
- `src/serverpilot/mcp_server.py`、`client.py`：身份、工具、信封
- `src/serverpilot/service.py`、`api.py`、`models.py`、`schemas.py`：租约、回收、REST
- `src/serverpilot/migrations/versions/`：新增 0041
- `src/serverpilot/alerts.py`（新增）
- `src/serverpilot/bundled_plugins/slurm-immediate`：`exec_prefix`
- `src/serverpilot/cli.py`：迁移到 v2 并裁剪
- `desktop/`：`AppShell`、`ServersScreen`、`ServerDetail`、`ResourceUsageDashboard`、`ServerPilotCore/.../BrokerStore.swift`、`build-macos-app.sh`
- `docs/DESIGN_SYSTEM.md`、`README.md`（改写“一份租约只在一台服务器”）

## 验证

- **迁移**：用真实库的副本在临时目录跑迁移，确认活跃租约、历史和审计完整；不碰实时 `state/`。
- **Agent 生命周期 e2e（fake provider）**：申请、进入、心跳、加减卡、跨服务器、重启、接管、回收通知、归还。
- **响应预算测试**：用 6 服务器、28 卡、22 节点的夹具，断言每个工具不超过上文上限。
- **并发与边界**：两个 session 互相操作被拒、重复任务、resize 部分失败、卡被回收后 epoch 变化、`keep_idle` 第 4 个窗口被回收、陈旧数据不触发回收和有卡提醒。
- **提醒**：网络抖动不误报，App 关闭后再打开得到摘要，有卡提醒的稳定和限频符合表格。
- **契约**：`serverpilot mcp policy --check`、Ruff、全量 pytest、三平台 CI。
- **桌面**：每次桌面改动后运行 `zsh desktop/build-macos-app.sh` 和 `zsh desktop/verify-macos-app.sh`；键盘、VoiceOver、缩放列入验收。
- **实机（需批准）**：Slurm 申请、进入、归还全流程；验证 `exec_prefix`；三个客户端真实接入并重连。

## 风险

1. **跨服务器租约**会碰到所有默认“一租约一机”的路径，靠上面的不变量、必改点清单和 e2e 控制。
2. **ad hoc 签名下系统通知可能不可用**，P0 先验证；菜单栏徽章和提醒页不依赖它。
3. **升级窗口内老租约无人心跳**，靠迁移重置计时并允许按 `lease_id` 认领。
4. **客户端能力不一致**（尤其 roots），P0 探针结果可能改变 project 的取值顺序，但不改变工具形状。
5. **`v4` 分支周期较长**，期间 `master` 的 3.x 修复要定期合入，避免最后大冲突。

## P0 结果（2026-10-30）

用一次性配置实测 MCP 握手，未写入任何客户端的全局配置：

| 客户端 | clientInfo.name | roots | cwd |
|---|---|---|---|
| Claude Code 2.1.306 | `claude-code` | 有 | 启动目录 |
| Codex 0.165.0 | `codex-mcp-client` | 无 | 启动目录；无线程类环境变量 |
| Cursor | 未验证 | 未验证 | `cursor-agent` 未登录 |

Codex 没有 roots，cwd 不是项目时必须显式传 `project`；回退顺序不变。

系统通知：ad hoc 签名的探针在临时目录被直接拒绝（UNErrorDomain Code=1），放到 `~/Applications` 后进入等待用户授权。签名不是障碍，但授权和投递未走通，P5c 用真实 App 验证。本机无代码签名身份。
