---
status: active
superseded-by:
created: 2026-08-21
updated: 2026-09-09
---

# Record: MCP 投影瘦身与 macOS GUI 优化 — 阶段二方向修正与交付结果

supersedes: `docs/teamwork/plans/2026-08-21-mcp-projection-and-macos-gui-optimization.md`（已并入本文件的 History）

前一版计划的阶段一与阶段三判断成立并已按其执行；**阶段二的方法被实测否定**，用户于 2026-08-21 批准替代方向。本文件记录修正后的方向与已交付结果。

## Current synthesis

- Subject identity: MCP 响应投影瘦身 + macOS 桌面端设计契约/可访问性优化这一持续目标
- 阶段一与阶段三按原计划交付；阶段二的 ETag/304 方法被实测否定，改为 `include_advanced=false` 收窄状态载荷。
- 实测：决策全流程 −69.2%、`gpu_status` −32.2%、`gpu_apply` −39.1%、桌面端状态刷新 −22.4%。
- 2026-08-30：agent 分不清「占卡」与「任务占用」的根因定位为契约文本缺失（`ROUTINE_GPU_STATUS` 早有四值，`MCP_INSTRUCTIONS` 之前从未定义它们），已在 `MCP_INSTRUCTIONS` 补上四值语义；`service.py` 里 `KEEPALIVE` 在 agent 侧被 `publicly_available` 抹平仍未解决，留作后续。
- 2026-09-09：`KEEPALIVE` 这一条已按「补词表」而非「开新字段」收口——`resource_nodes` 投影原样透传裸 `state`，而契约全文从未出现 KEEPALIVE 这个词，agent 因此把控制面自己的占卡读成别人的任务（见下节实测）。已在 `agent_contract.py` 的 resource_nodes 规则里给这个 token 下定义；`publicly_available` 仍是一个布尔，未开新字段。

## 为什么阶段二改向（两条实测证据）

前一版写"以 `snapshot_revision` 为强 ETag 返回 304"，未验证两个前提，两个都不成立：

1. **正确性**：`src/serverpilot/service.py:3562` 的 GPU 状态按请求时刻的 `now` 计算，`age > stale_after_seconds` 会在采集停止后**仅凭时间流逝**把卡翻成 `UNKNOWN_STALE`，而 revision 不自增。维护窗口与预约窗口同为时间驱动。以 revision 为 ETag 会在采集器故障后持续返回 304，让 GUI 长期显示"新鲜"数据——直接违反项目的 telemetry fail-closed 规则与 `DESIGN_SYSTEM.md` §5 的`无响应 / 无最新数据`状态。
2. **收益**：实测 1 分钟窗口，revision 每 5 秒推进约 4 次（3 台服务器各自 ingest 观测都会 `_bump_revision`）。12 次 5 秒轮询中仅 3 次 revision 未变，命中率约 25%。

同时实测到成本确实存在且有更好的着力点：桌面端每 5 秒拉取 `/api/v1/state`（**不是** `/api/v1/snapshot`，前一版记错了端点）约 74 KB / 30ms，其中约 22% 是界面从不渲染的通用资源与外部 scheduler 投影。`compact=true` 不可用——它会丢掉 `recent_average`、`memory_utilization_pct`、`keepalive` 与 lease 详情，而设计契约的四项近 10 分钟均值和占卡状态正需要这些。

## 修正后的阶段二（已交付）

- 服务端 `GET /api/v1/state` 新增 `include_advanced: bool = True`；为 `false` 时省略 `resource_providers`、`allocatable_units`、`scheduler_targets`、`scheduler_jobs`、`scheduler_transfers`、`workload_profiles`、`resource_plan_evaluations`（`Service.ADVANCED_STATE_KEYS`）。默认值保持既有行为，旧服务忽略未知查询项，向后兼容已实测。
- `BrokerStore.swift` 的 `URLSessionBrokerSnapshotClient.snapshot` 带上 `include_advanced=false`。Swift 模型不动——所有解码路径均为 `as? … ?? []`，缺键退化为空数组。
- **客户端无变化短路无需新建**：`BrokerSnapshot.isSemanticallyEquivalentForRefresh(to:)` 已存在，并在 `BrokerStore.swift:1034` gate 住 `self.snapshot` 赋值。
- 实测：状态载荷 76,969 → 59,727 字符（**−22.4%**），界面渲染的段逐字节不变（由 `tests/test_control_plane_state.py` 断言）。

## 交付结果与实测

| 场景 | 改前 | 改后 | 降幅 |
| --- | --- | --- | --- |
| Agent 判断"有没有空卡、被谁占着"（原需两次调用） | 21,752 字符 / 5,438 est tokens | 6,706 / 1,676 | −69.2% |
| `gpu_status` 单次 | 8,954 | 6,071 | −32.2% |
| `gpu_apply`（4 卡） | 2,444 | 1,489 | −39.1% |
| 桌面端每次状态刷新 | 76,969 | 59,727 | −22.4% |

单次调用降幅低于前一版估的 40% / 47%：当时把端点字段总量当成可全部消除，实际仍需保留一份，另加 `servers[]` 的结构开销。头条数字（决策全流程 −69.2%）与原估 −73% 基本吻合。

## 按证据偏离原计划的三处

1. **MCP instructions 上限 512 → 560**（`tests/test_agent_policy.py`）。必需的禁令措辞加上新的 `servers[]` 结构装不进为旧契约设定的上限。选择抬高闸门而非弱化禁令用词；代价约每会话 10 est tokens。
2. **`gpu_release` 不返回 `active_leases`**。所有 routine MCP 调用共用 actor `agent`（`mcp_server.py:48`），该列表会把其他会话的任务名泄露给当前调用者，并可能诱导它释放不属于自己的租约。改为回显被结算的 `lease_id` 与 `state`。
3. **不删除桌面端未使用的 Swift 模型**。`resourceProviders` / `allocatableUnits` / `scheduler*` / `resourcePlanEvaluations` 确认 UI 零引用（`monitoringProviders` 也只被测试引用），但它们由 `BrokerStoreTests.swift`（XCTest）覆盖，本机无 XCTest，删除将无法验证。改为只在请求侧收窄。

## 阶段三状态

- **3.1 审计已完成**：`docs/teamwork/records/macos-design-contract-audit.md`。9 项符合、5 项偏差（D1–D5）、5 项可访问性发现（A1–A5）。
- **已落地**：D1（表头图标 `terminal` → `server.rack`）、D3（`数据更新间隔` → `数据采集间隔`，含 UI 测试与夹具证据脚本共 6 处同步）、D4（使用情况页空状态文案）。
- **已落地（用户 2026-08-21 批准按推荐执行）**：
  - **A1**：`success` `#3EB866` → `#339653`、`warning` `#F2B100` → `#AA7C00`（只改明度、保持色相），`danger` 达标不变。实测 vs 内容表面 3.48 / 3.50 / 3.55，vs 页面底色 3.01 / 3.03 / 3.07，全部过 WCAG 1.4.11 的 3.0 且三者视觉权重一致。`DESIGN_SYSTEM.md` §3 颜色表与对比度下限同步更新。未取 4.5 档（`#287642` / `#866200`）——琥珀会变成橄榄褐，丢掉调色板 identity，而契约本就只要求这些色用于图形对象。
  - **D2**：`DESIGN_SYSTEM.md` §5 补充紧凑布局图标例外。
- **执行 A1 时新发现 D6**（第一轮审计遗漏）：§3 已明文"压力颜色只放在状态点和进度柱上"，但有 6 处用状态色渲染 8.5–11 pt 文字标签，属既有契约违规。A1 把它们从 1.77 提到 3.50，但正文尺寸需 4.5:1 仍不达标。未处理——这些多为 `Label(text, systemImage:)`，收回颜色需拆 Label 重构视图，且是否保留彩色警告文字属独立设计取舍。
- **未执行**：A2（140 处硬编码 `.font(.system(size:))`、0 处语义字体，界面完全不随系统文字大小缩放）建议单独立项；A3/A4/A5 需实机走查。

## Verification（本轮实际执行）

- `uv run --reinstall-package serverpilot pytest` → **476 passed**（改动前基线 460，新增 3 个测试）
- `uv run ruff check .` → All checks passed
- `zsh desktop/build-macos-app.sh` + `zsh desktop/verify-macos-app.sh` → PASS（根目录唯一 `ServerPilot.app`）
- stdio 端到端：默认工具严格 3 个；实机 3 服务器 12 卡验证 `servers[]` / `busy_gpus` / `recent_average` 结构
- `git diff --check` 干净

## 仍然成立的证据边界

- 本机无 XCTest，Swift 单测与 XCUITest 未运行；可访问性结论只来自静态检查与数值计算。
- 未连真实 GPU 执行任何写操作。
- 未建分支、未开 PR、未提交。
- 发布须知：阶段一改动后必须重跑 `python3 scripts/install_agent_policy.py all --install`。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-30 — 占卡 vs 任务占用：根因是契约文本缺失，不是模型能力（MCP 措辞已补，service.py 未动）

用户报告 agent 分不清「占卡」（有人占着但没有计算进程）与「任务占用」（真的在跑）。

**已观测**：GUI 侧 `service.py` 的 `_gpu_public_projection` 早已有完整六态中文词表（可用·空闲占卡 / 可用·未开启占卡 / 占卡 / 任务占用 / 未归属占用 / 归属冲突）；MCP 侧 `mcp_server.py` 的 `ROUTINE_GPU_STATUS` 也早有对应四值（`held_idle` / `running` / `busy_unmanaged` / `ownership_conflict`），但改动前的 `MCP_INSTRUCTIONS` 从未解释这四个值分别是什么意思，只提了 `busy_gpus(task)`，只给了「谁占着」这一个轴，没给「是否在算」那个轴。

**推断的根因**：agent 判断不出占卡 vs 任务占用，不是模型读不懂这四个英文词，而是契约文本从未定义它们——`status` 字段的值域本身就是未文档化的死信息，agent 唯一能确定的是 `task` 非空即"有人占着"，无从区分空占与在跑。

**本轮已交付（`mcp_server.py`）**：`MCP_INSTRUCTIONS` 新增一行显式给出四值语义（`running`=该租约下有计算进程，`busy_unmanaged`=ServerPilot 不拥有的进程，`held_idle`=占着但没在算、应找持有者或换一张卡，`ownership_conflict`=持有者无法核实），并点出只有 `running`/`busy_unmanaged` 两值代表卡上真的有工作；`gpu_status` docstring 与内部 `_routine_gpu_status` docstring 同步补上"要看 `status` 这个轴"的指引，值的语义只在 instructions 里定义一次，不重复。为控制契约总长，同时删掉两处与别处逐字重复的旧文字（`gpu_add_server` 参数里的 observation_profile 枚举、`busy_gpus(task) with no telemetry` 的 `(task)`），详细前后对照与逐条理由见改动本身的提交说明（本文档不重复贴代码）。改动使 `tests/test_agent_policy.py` 的契约长度上限需要抬高，该文件不在本文档作者的改动范围内，由另一位 agent 完成。

**本轮未动、仍是未决问题**：`service.py` 的 `_gpu_payload_is_publicly_available` 把 `KEEPALIVE` 状态也算作 `publicly_available`，MCP 投影里一张挂着 ServerPilot 自己占卡程序的卡因此和普通 `available` 卡长得一样——没有字段告诉 agent「这张看起来空的卡其实带着我们自己的占卡」。这一条在本轮的 changeSpec 里所有涉及可用性语义的选项都被判 `rejected`，`service.py` 本次未改一行，`M` 状态来自另一位 agent 在做别的事。留给后续：如果要让 agent 也能看见"这张卡挂着占卡程序、马上能抢"和"这张卡真的什么都没有"的区别，需要在 `service.py` 的可用性投影上开一个新字段或新枚举值，而不是继续用 `publicly_available` 一个布尔盖住两种情况。

### 2026-09-09 更新：这一条已闭合，走的是词表而不是新字段

**实测证据（用户会话 `Threads讨论续接与子代理设计 (fork)`，逐字）**：一个 agent 报告
「百舸两张卡 broker 报 available，但遥测显示都是 KEEPALIVE、各占 65 GB / 65% 利用率的外部进程——不抢」，
被用户当场纠正「KEEPALIVE 就是可用，你申请就好了」。65 GB 约为 80 GB 卡的 80%，正是占卡程序
的既定占用比例，所以它看到的确实是控制面自己的占位进程。

**根因比上面写的更具体**：不止是 `publicly_available` 抹平了一个布尔。同一份响应里另有一条
相反的叙述——`mcp_server.py` 的 `resource_nodes[].gpus[]` 把每张卡的 `state`、`telemetry`、
`processes` 原样透传，其中 `state` 就是裸字符串 `"KEEPALIVE"`；而唯一能说明归属的 `keepalive`
字典没有被投影进去，`processes` 行也没有任何「这是控制面自己起的」标记。与此同时
`agent_contract.py` 全文 0 处出现 KEEPALIVE，`ROUTINE_GPU_STATUS` 也没有这个条目——
所以 agent 拿到的是一个词表里查不到的 token 加一个高显存读数，只能自己编。

**已执行的修法（选项二：补契约文本）**：在 `agent_contract.py` 的 resource_nodes 规则末尾
加一句，给这个 token 下定义——`state=KEEPALIVE` 是 ServerPilot 自己在无人占用的卡上的
hold，算可用容量，分配前会被停掉，它的进程／显存／利用率都不是别人的活。同时把原先落在
lease-telemetry 规则里的那句「free card 上的负载是 ServerPilot 自己的 hold」移到这里，
避免同一事实在两条规则里各说一遍。已重新渲染 `docs/AGENT_MCP_policy.en.md` 与
`.cursor/rules/serverpilot.mdc`，`mcp policy --check` 通过；契约长度 3613 / 4200 字符。

**明确没做**：没有在 `service.py` 的可用性投影上开新字段或新枚举值，也没有给 `resource_nodes`
补 `keepalive` 归属摘要。上面那条「开新字段」的后续建议因此仍然是**未选用的备选**，不是待办：
当前判断是 agent 需要的是能查到这个 token 的意思，而不是多一个字段。若将来出现「必须区分
『挂着占卡、马上能抢』和『真的什么都没有』」的真实消费者，再回到那条建议。

**同一会话里顺带收掉的两处冗余判据（`service.py`，与本条 MCP 主题不同但同批次交付）**：`_validate_keepalive_observation`（2318–2330 行）与 `release_empty_conflicted_lease`（7456–7468 行）各删掉一行 `provider_state.last_error is not None`。论证：观测事实由 `EndpointTelemetryCurrent.collected_at` 与 `ProviderState.last_success_at` 两者都 `>= threshold/barrier` 承载,即"操作之后确实成功观测过一次";`last_error` 只描述最近一次尝试，一次成功观测落库后再发生的无关探测抖动不会回溯改写那次已成立的观测。保留它的实际效果是：在线主机上一次超时探测就能让已取得的有效证据作废，使占卡激活或人工恢复入口在控制面本就不健康时更难用，与这两个入口的用途相反。已观测：新增测试 `test_probe_failure_after_a_complete_observation_still_activates_keepalive` 覆盖该路径。

### 2026-08-21 — 阶段二方向修正与交付结果落盘

记录阶段二改向的两条实测证据、修正后的交付、按证据偏离原计划的三处，以及 476 passed 的验收。

### 2026-08-21 — 前一版计划（阶段二方法已被实测否定）并入

##### Intended result and selected direction

- Subject identity: ServerPilot MCP 响应投影优化 + macOS 桌面端设计契约/可访问性优化
- Authoritative owner: Root agent
- Intended result: 在不动三工具契约与 loopback 边界的前提下，把 Agent 单次 GPU 决策的响应体积降约 70%，把 GUI 无变化轮询降到 304 零 body，并把桌面端设计契约一致性与可访问性从"未验证"变为"有证据"
- Selected direction: 用户于 2026-08-21 批准的三阶段方案，宿主计划文件 `~/.claude/plans/mcp-gui-wondrous-quilt.md`
- User-settled constraints（本次对话明确确认，未被推翻）:
  - 响应结构**一次性切换**，不做双写过渡期
  - **分三阶段**交付，每阶段可单独发布与回滚
  - 不新增第四个 MCP 工具；默认发现结果严格保持 3 个
  - 不动 loopback 边界、访问控制、远端运行时、自动 allocator
  - 不连真实 GPU 执行写操作
  - 不建分支、不开 PR、不自动提交

##### Measured baseline（证据，非估算）

来源：`本地 Agent 会话记录`，2026-08-21T11:50–11:52Z 的真实 `mcp__serverpilot__*` tool_use / tool_result 配对。环境为 1 台服务器 8 张 GPU。

| 场景 | 实测当前 | 目标 | 依据 |
| --- | --- | --- | --- |
| `gpu_status()` | 8,348 字符（≈2,087 est tokens） | ≈5,000（−40%） | 端点级字段逐卡重复 2,040；遥测窗口描述符重复 910；`last_observed_at` 与 `observed_at` 重复 368 |
| 决策全流程（含第二次 `include_busy=true`，11,918 字符） | 20,266 字符（≈5,066 est tokens） | ≈5,430（−73%） | 默认响应内嵌紧凑忙卡摘要后不再需要第二次调用 |
| `gpu_apply(gpu_count=4)` | 2,290 字符 | ≈1,210（−47%） | 4 行 × 270 字符逐卡重复，且顶层已有同样字段 |
| GUI 轮询（无变化） | 完整快照 | 304 零 body | 复用已存在的 `snapshot_revision` |

关键结构事实（已逐字段核验）：

- 8 张卡的 `server_id` / `workspace_path` / `workspace` / `ssh` 完全相同。
- `recent_average` 的 `window_seconds` / `sample_count` / `first_observed_at` / `last_observed_at` 在 8 张卡上 distinct 均为 1。
- `telemetry.observed_at` 与 `telemetry.recent_average.last_observed_at` 逐字符相同。
- `_routine_gpu_allocation` 构建了 `resources` 列表但仅在单端点时使用，多端点时整个丢弃。

##### Current execution plan

1. **阶段一 — MCP 投影与契约**（纯 Python）：端点级字段提升到顶层 `servers[]`；遥测窗口描述符按端点去重并对逐卡不一致 fail-closed；默认响应内嵌 `busy_gpus` 紧凑摘要；`gpu_apply` 的 `resources` 无条件返回且 GPU 行只留设备字段；`gpu_release` 返回 `active_leases` 以支撑既有的"逐个释放并确认"契约；`gpu_status` 透传已有的 REST `endpoint_id` 为 `server_id` 参数。同步 `MCP_INSTRUCTIONS`、`docs/AGENT_MCP_policy.en.md`（三客户端唯一源）、`docs/AGENT_MCP_zh.md`、`docs/IMPLEMENTATION_STATUS_zh.md`、测试与 CHANGELOG。
2. **阶段二 — 共享快照层 ETag/304**：`/api/v1/snapshot` 以 `snapshot_revision` 为强 ETag，ETag key 必须纳入 `compact`/`endpoint_id`/`state`/`only_available` 四个查询参数；消费侧为 `BrokerStore.swift`、`web/static/app.js`（需去掉 `cache: "no-store"`）、`client.py`。收益集中在 GUI 轮询，对 Agent 成本无实质影响，须如实表述。
3. **阶段三 — macOS GUI**：先做设计契约逐条一致性审计（`DESIGN_SYSTEM.md` §2/§3/§5–§10 → Swift 实现），产出偏差清单；设计改进依赖审计结果且必须先改契约再改实现；性能项消费阶段二的 304；可访问性以 92 处硬编码 `.font(.system(size:))`、仅 1 处 `accessibilityReduceMotion`、三个状态色未测对比度为具体抓手。

##### Dependencies and parallel tracks

- 阶段三 3.2/3.3/3.4 依赖 3.1 审计结论；3.1 尚未执行，当前**没有**偏差清单，不得当作已知结论引用。
- 阶段三 3.3 依赖阶段二交付的 304。
- 阶段一与阶段二之间无强依赖，但阶段一先行可让体积回归门更早生效。

##### Verification contract

- 阶段一：`uv run pytest tests/test_api_gui_mcp.py tests/test_mcp_harness_neutral.py tests/test_agent_policy.py -q`；`uv run ruff check .`；新增体积回归门断言 GPU 行不含端点字段且序列化长度低于阈值；stdio 端到端确认默认工具仍严格为 3 个。
- 阶段二：pytest + `curl -i` 验 304，并对四个查询参数组合各验一次 ETag 不串台。
- 阶段三：`zsh desktop/build-macos-app.sh` 后 `zsh desktop/verify-macos-app.sh`，只评审根目录唯一 `ServerPilot.app`。
- 全阶段：`git diff --check`；用户可见行为改动同步 `CHANGELOG.md` 与 `CHANGELOG.en.md` 的 `Unreleased`。

##### Known evidence limits

- 本机未安装 XCTest（`docs/IMPLEMENTATION_STATUS_zh.md` 已记录），Swift 单测与 XCUITest 无法运行；可访问性结论只能来自实机走查与静态检查，不能宣称自动化验证通过。
- 基线取自单服务器 8 卡的一次真实会话；多服务器场景的节省比例应在实际多端点环境复测后再声明。
- case-v2 工具（`case-inspect` / `case-schema` / `case-apply`）本机不可用，本 checkpoint 按 `docs/teamwork/plans/` 既有先例落盘。

##### Upgrade note

外部 Agent 的全局规则是磁盘上的静态副本。阶段一发布后必须重跑 `python3 scripts/install_agent_policy.py all --install`（Cursor 用 `--print` 手动粘贴），否则规则描述与实际响应不符。

### 2026-08-29 — 恢复被 relink 抹掉的前序文档名

`:10` 的 `supersedes` 指向了本文件自己的路径，即声称本文件取代本文件。按备份原文恢复为被取代的前一版计划 `docs/teamwork/plans/2026-08-21-mcp-projection-and-macos-gui-optimization.md`，并加注「已并入本文件的 History」。

### 2026-08-29 — 记录迁移改名

本文件由 `docs/teamwork/plans/2026-08-21-mcp-projection-and-macos-gui-optimization-r2.md` 迁移改名为 `docs/teamwork/records/mcp-projection-and-macos-gui-optimization.md`：日期前缀与 `-r2` 版本号都不表达 identity（本文件记录的是阶段二方向修正后的交付结果，kind 应为 records 而非 plans；`-r2` 只是当时的版本序号），按落盘契约的 slug 规则一并去掉。改名前本文件 History 未记录旧路径，`grep -rn 'r2' docs/teamwork/records/mcp-projection-and-macos-gui-optimization.md` 因此无输出、外部引用无法回查；本条补记旧路径 → 新路径，不改写任何已有条目的判断或正文。
