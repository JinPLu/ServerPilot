---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-26
---

# Record: per-GPU 占卡范围

## Identity and final goal

- Subject identity: ServerPilot 空闲占卡从整机切换为逐 GPU
- Final goal: per-GPU 执行 + endpoint 级开关；不提供逐 GPU 启动按钮
- Authoritative owner: user

## Decision map

- 2026-08-12：用户接受 per-GPU + endpoint toggle
- 2026-08-21：空闲租约两阶段回收是后继相关决定，不是本设计的复述
- 2026-08-26：从 legacy case `c-df7e2935…/decision.md` 提炼到扁平文档；原件保留

## Current stage

已决占卡形状。资源预算数字已过时。

## Current question batch

无未决问题。

## Options and recommendation

当时选定：per-GPU execution + endpoint-level toggle + automatic start/reclaim/reconcile + explicit opt-in。不提供逐 GPU 启动按钮。

## Decisions and open points

- 采用 per-GPU 执行 + endpoint 级开关 + 自动启动/回收/对账 + 显式开启。
- 不提供逐 GPU 启动按钮。
- v1 自动让渡只保证 ServerPilot-managed claim；ad-hoc 直接 SSH 任务必须先关闭该 endpoint 的 idle-keepalive policy。
- 策略只有 `disabled` | `idle_keepalive`；运行态按 GPU 推导。
- 不确定、观测陈旧、foreign process、身份冲突或无法证明 keeper 已空闲时一律 fail closed。
- 不引入队列、通用抢占、后台 controller，或对 ad-hoc SSH 任务的事后强制抢占。
- 整机 worker/lease 在本范围内已被取代。

预算数字已过时：本案里的约 31% VRAM / 0.25 core / 100ms 是当时设计目标。现行实现按目标 GPU 显存约 80% 占用，以 `ADAPTERS_zh.md` 与后续计划为准。

## Experiment slot

unused.

## User quotes

「采用“per-GPU execution + endpoint-level toggle + automatic start/reclaim/reconcile + explicit opt-in”的占卡设计。」
「不提供逐 GPU 启动按钮。」

## Working understanding

这是占卡范围，不是空闲租约回收策略。后者见 `docs/teamwork/records/idle-lease-reclaim.md`。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-12 — 用户接受 per-GPU 占卡

整机 keepalive 被本范围取代。

### 2026-08-26 — 提炼到扁平文档

从 gitignored 的 case-v2 原件提炼；原件不动。标注预算数字已被后来的固定约 80% 显存取代。

### 2026-08-11 — legacy case-decision 原件（18:52）并入

##### 已接受决定

采用“per-GPU execution + endpoint-level toggle + automatic start/reclaim/reconcile + explicit opt-in”的占卡设计。占卡对象是单个 GPU，控制入口是服务器（endpoint）级的一个开关；不提供逐 GPU 启动按钮。用户已确认：v1 的自动让渡只保证 ServerPilot-managed claim，ad-hoc 直接 SSH GPU 任务必须先关闭该 endpoint 的 idle-keepalive policy；未来安全 wrapper 不在 v1 范围内。

##### Policy 与运行态

- endpoint desired policy 只有 `disabled` 与 `idle_keepalive`；运行态按 GPU 推导。
- 一次开启即立即 reconcile 该 endpoint 上所有 eligible idle GPU 并启动对应 keeper。busy GPU 保持原状，之后被 release 或新的观测判定为空闲且符合条件时自动加入。
- 每张 GPU 使用一个独立的内部 keepalive lease 与 worker，UI 按 endpoint 聚合。worker 具备独立 identity、attestation 和 binding；endpoint 必须显式 opt-in。
- sealed adapter 只接收 broker 推导的 GPU UUID targets，不接收调用方 shell、可执行路径、环境、PID 或任意自定义命令/参数。

##### Claim、回收与恢复

普通 claim 只把已验证的 keepalive GPU 当作可回收资源。流程是：选定候选 GPU → 只停止这些卡的 keeper worker → 取得 fresh empty observation → 释放对应 keepalive lease → 完成普通 immediate claim。其他 GPU 的 keeper 继续运行。普通任务 release 或新的 idle 观测触发 reconcile，在符合条件的 GPU 上自动重新启动 keeper。

不确定、观测陈旧、foreign process、身份冲突或无法证明 keeper 已空闲时一律 fail closed。该设计不引入队列、通用抢占、后台 controller 或对 ad-hoc SSH 任务的事后强制抢占。

##### 低主机影响资源契约

- 每张被覆盖 GPU 固定约 31% VRAM。
- 以 rolling 5 分钟窗口内 GPU utilization ≥30% 为验收门槛，允许 startup grace。
- 稳态 worker 磁盘与网络 I/O 为 0：不加载数据、不写 checkpoint、不刷日志；collector 维持既有 cadence。
- host CPU 平均上限为每张 kept GPU ≤0.25 logical core、单 endpoint ≤1 logical core；worker 启动串行化，PyTorch thread pools 设为 1。
- host RSS ≤512 MiB/GPU 是预算提案；如实现证据表明需要调整，必须以验证结果修订，而不是默认放宽。
- 使用短 duty period 的 resident GPU-only compute buffer；约 100ms（相对当前 1s）是待验证的低 alias 提案。

##### UI 与可观测性

显示一个“开启/关闭空闲占卡”动作。endpoint 摘要可呈现“已开启 · 3/4 占卡，1 卡任务中”；资源列表逐 GPU 显示派生的 KEEPALIVE 或其他真实状态，并解释未覆盖原因。展示每 GPU 的派生健康、last verified time、显存百分比、rolling utilization 和 degraded/error reason；操作 banner 可识别并打开受影响的 endpoint。

##### 历史与范围

旧的 whole-endpoint worker/lease 以及依赖手动编排的 whole-endpoint 设计对混合 GPU 状态不成立，标记为本 scope 的 superseded；旧 case 和历史 artifact 保持不可变。当前只固化设计与验收边界，不包含运行时代码变更。

### 2026-08-11 — case-collaborate 稿（18:51，协作已收敛）并入

##### 已确认结论（协作已收敛）

占卡的资源粒度是单个 GPU；控制面保留一个服务器（endpoint）级开关。打开一次后，系统立即为该 endpoint 上所有符合条件的空闲 GPU 对账并启动占卡，不提供逐卡启动按钮。正在执行任务的 GPU 不被触碰，之后变为空闲且符合条件时自动加入。

用户已确认 v1 边界：自动让渡只保证 ServerPilot-managed claim。ad-hoc 直接 SSH GPU 任务必须先关闭该 endpoint 的 idle-keepalive policy；未来可另行设计安全 wrapper，但不属于 v1。当前方向已可进入实现设计；没有运行时代码改动。

##### 观测证据（2026-08-12，Asia/Shanghai 约 02:38）

- 服务器 181 在用户点击后进入 ACTIVE；两张 GPU 各占 25,660 MiB（约 31.3%），瞬时利用率为 80% / 73%，说明 helper 确实在运行。
- 服务器 203 未配置 keepalive；GPU0 有任务/冲突，GPU1–3 为 AVAILABLE。旧的整 endpoint 语义叠加未配置状态，解释了为何空闲卡没有被占用。

##### 已选方案与被取代方案

采用按 GPU 执行、endpoint 级意图：一个服务器级开关驱动逐卡 worker 与 lease，UI 聚合。它能独立回收和重启，忙卡不受影响，归属、观测和失败原因都能逐卡表达。

旧的整服务器 worker/lease 以及依赖手动编排的 whole-endpoint 设计对混合状态不成立，现对本 scope 标记为 superseded；旧 case 保持不可变，不覆盖历史。

##### 执行、对账与 admission

- endpoint desired policy 仅为 `disabled` 或 `idle_keepalive`，运行态按 GPU 推导。启用一次即 reconcile 所有 eligible idle GPU；busy GPU 保持原状，release 或新的 idle 观测触发再次 reconcile。
- 每 GPU 一个独立的内部 keepalive lease 和 worker，UI 按 endpoint 分组。sealed adapter 只接收 broker 推导的 GPU UUID targets，不接受调用方 shell、路径、环境或 PID；每个 worker 有独立 identity、attestation 与 binding。
- claim 只把已验证的 keepalive GPU 当作可回收资源：先选定候选 GPU，只停止这些卡的 keeper worker，取得 fresh empty observation，释放对应 keepalive lease，再完成普通 immediate claim。其他 keeper GPU 继续运行。
- 释放或新近空闲观测会自动重新启动符合条件的 keeper。任意不确定、观测陈旧、foreign process 或身份冲突都 fail closed；不引入队列、通用抢占或后台 controller。

##### 低扰动资源预算与验收

- 每张被覆盖 GPU 固定约 31% VRAM；rolling 5 分钟窗口内 GPU utilization ≥30% 为验收门槛，允许 startup grace。
- 稳态 worker 磁盘/网络 I/O 为 0：不加载数据、不写 checkpoint、不刷日志；collector 维持既有 cadence。
- host CPU 平均上限：每张 kept GPU ≤0.25 logical core，单 endpoint ≤1 logical core。worker 启动串行化。host RSS ≤512 MiB/GPU 是预算提案；若实现证据显示需要调整，应以验证结果修订。
- 使用短 duty period 的 resident GPU-only compute buffer；约 100ms（相对当前 1s）是待验证提案。PyTorch thread pools 设为 1，以降低 CPU 和采样 alias。
- UI 展示每 GPU 的派生健康/KEEPALIVE 状态、last verified time、显存百分比、rolling utilization 和 degraded/error reason，并提供 endpoint 摘要，例如“已开启 · 3/4 占卡，1 卡任务中”。banner 可识别并打开受影响 endpoint。

##### 决策状态

方向已被用户接受：per-GPU execution + endpoint-level toggle + automatic start/reclaim/reconcile + explicit opt-in + 低扰动预算；managed-claim guarantee 是 v1 的明确边界。下一步是按该决定进入实现和验证，不改变旧 whole-endpoint 历史。

### 2026-08-11 — case-collaborate 旧快照（18:38，尚未接受为最终决定）并入

##### 当前结论（仍在协作，尚未接受为最终决定）

占卡的资源粒度是单个 GPU；控制面保留一个服务器（endpoint）级开关。用户打开一次后，系统应立即为该服务器上所有符合条件的空闲 GPU 对账并启动占卡，不提供逐卡启动按钮。正在执行任务的 GPU 不被触碰，之后变为空闲且符合条件时自动加入。

当前推荐：endpoint desired policy 只有 `disabled` 与 `idle_keepalive`，运行态按 GPU 推导；每张 GPU 使用一个独立的内部 keepalive lease 和 worker，UI 按服务器聚合。显式 opt-in、自动 reconcile、按卡回收，且严格受资源预算约束。当前没有运行时代码改动。

##### 观测证据（2026-08-12，Asia/Shanghai 约 02:38）

- 服务器 181 在用户点击后进入 ACTIVE；两张 GPU 各占 25,660 MiB（约 31.3%），瞬时利用率为 80% / 73%，说明 helper 确实在运行。
- 服务器 203 未配置 keepalive；GPU0 有任务/冲突，GPU1–3 为 AVAILABLE。旧的整 endpoint 语义叠加未配置状态，解释了为何空闲卡没有被占用。

##### 方案比较

| 方案 | 形状 | 结果与取舍 |
| --- | --- | --- |
| 旧式整服务器 worker/lease | 一次覆盖 endpoint 的单个 keeper | 实现简单，但一张卡忙或冲突就会阻断同服务器其他空闲卡；无法表达 203 这类混合状态，因此不再推荐。 |
| 按 GPU 执行、endpoint 级意图 | 一个开关驱动逐卡 worker 与 lease，UI 聚合 | 可独立回收和重启，忙卡不受影响；归属、观测和失败原因都能逐卡表达。复杂度主要在 reconcile 与身份绑定，但边界和可运维性最好。当前推荐。 |

##### 执行、对账与 admission

- 一个服务器级开关设置 policy 并立即 reconcile 所有 eligible idle GPU；不新增逐 GPU 启停按钮。busy GPU 保持原状，release 或新的 idle 观测触发再次 reconcile。
- 推荐每 GPU 一个内部 keepalive lease，以获得独立 reclaim；UI 仍按 endpoint 分组。sealed adapter 只接收 broker 推导的 GPU UUID targets，不接受调用方 shell、路径、环境或 PID；每个 worker 有独立 identity、attestation 与 binding。
- claim 只把已验证的 keepalive GPU 当作可回收资源：先选定候选 GPU，只停止这些卡的 keeper worker，取得 fresh empty observation，释放对应 keepalive lease，再完成普通 immediate claim。其他 keeper GPU 继续运行。
- 任意不确定、观测陈旧、foreign process 或身份冲突都 fail closed。释放或新近空闲观测只触发 eligible GPU 的 reconcile；不引入队列、通用抢占或后台 controller。

##### 低扰动资源预算与验收建议

- 每张被覆盖 GPU 固定约 31% VRAM；以 rolling GPU utilization ≥30% 作为接受门槛。
- 稳态 worker 磁盘/网络 I/O 为 0：不加载数据、不写 checkpoint、不刷日志；collector 维持既有 cadence。
- host CPU 平均上限：每张 kept GPU ≤0.25 logical core，单 endpoint ≤1 logical core。host RSS ≤512 MiB/GPU 是预算提案，仍需验证。
- worker 启动串行化，PyTorch thread pools 设为 1。把 duty period 从当前 1s 缩短至约 100ms 是提案；配合 resident GPU-only compute buffer，降低采样 alias。
- 用 rolling 5 分钟窗口验收，允许 startup grace；UI 展示 last verified time、显存百分比、rolling utilization，以及 degraded/error reason。

##### UI 形状

动作统一为“开启/关闭空闲占卡”；摘要可显示“已开启 · 3/4 占卡，1 卡任务中”。资源列表逐 GPU 显示 KEEPALIVE 或其他真实状态，并解释未覆盖原因。操作结果 banner 必须能识别并打开受影响的 endpoint。

##### 未决边界

v1 的自动让渡保证只覆盖 ServerPilot-managed claim。ad-hoc SSH GPU 任务必须先暂停 endpoint policy，或改用 managed claim；否则启动后的 foreign-process 检测存在竞态和 OOM 风险。用户尚未明确选择这一保证边界；当前建议以 managed-claim guarantee 为 v1 目标。RSS 上限与 100ms duty period 也是需要验证的提案，不应在验证前宣称已达标。

##### 当前推荐与下一步

采用“per-GPU execution + endpoint-level intent + automatic reclaim/reconcile + explicit opt-in + 上述预算”的方向；先由用户确认 managed-claim guarantee 及预算提案，再进入实现设计。旧 whole-endpoint case 保留为不可变历史，不能覆盖。
