---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-29
---

# Discussion: keepalive 对外原语形状与启停确认语义

## Current synthesis

- Subject identity: 最终目标是可选、极轻量的 GPU keepalive 附加能力；主题是对外原语形状与启停确认语义
- 当前文档仍是协作中的推荐设计，尚未成为用户接受的决定；两个未决点见文末。

## 当前方向

用户重新收敛了范围：ServerPilot 不需要自动观察队列、暂停占卡、交接正式任务、再自动恢复，也不需要另一套控制器。Agent 已经可以编排自己的申请和释放流程；人类端只需要一个开关按钮和一个可信的占卡状态。

因此，keepalive 应是一个可选的薄能力：ServerPilot 提供状态和一个很小的启停原语，Agent 决定何时调用；正式 claim 的队列、优先级、恢复策略和重试策略仍由 Agent 负责。

## 候选形状

| 方案 | 形状 | 取舍 |
| --- | --- | --- |
| A：只存意图 | GUI 写入“希望保活”的标记，Agent 轮询后自行启动或停止 keeper；ServerPilot 只显示 collector 看到的结果。 | 改动最少，但按钮不能唤醒未运行的 Agent，意图与 DRAINING 等现有运维状态容易混淆；keeper 进程归因也完全依赖 Agent。 |
| B：薄型显式原语（当前推荐） | 增加一个幂等 `set_keepalive(endpoint, enabled)`，REST、MCP、GUI 和 Agent 共用；ServerPilot 只执行受控 keeper 的启停并返回结果，不根据 claim 自动做任何事。 | 多一个明确的受控入口，但按钮立即可用，Agent 不必自建 SSH/脚本路径；调度工作流仍留在 Agent，边界最清楚。 |

当前建议采用 B，但仍处于协作阶段，未形成用户接受的决定。

## 公共接口与显示

只新增一个动作和一个派生状态：

```text
set_keepalive(endpoint_id, enabled: bool) -> {enabled, state, reason?}
```

- `enabled=true`：请求在该 endpoint 上启动 Broker-owned keeper。
- `enabled=false`：请求停止该 endpoint 上已登记的 keeper。
- 调用必须幂等；重复启用或停用返回当前结果，不创建第二个 keeper。
- GUI 与 Agent 走同一 REST/MCP 路径，不增加另一套桌面专用逻辑。
- GUI 只显示 `KEEPALIVE` 或现有的非保活状态；动作进行中使用本地 spinner，失败沿用现有 error/audit 展示。
- `KEEPALIVE` 只由 fresh collector 观测到已登记 keeper 后产生；没有 keeper 时 GUI/摘要可显示 `OFF`（表示未启用 keepalive；这不是新增 GPU domain state），不增加 `STARTING`、`STOPPING`、`PAUSED` 等公共状态。

## Agent 使用方式

Agent 自己决定时机，ServerPilot 不监听 claim 并不自动改变 keeper：

```text
申请前：若状态为 KEEPALIVE，调用 set_keepalive(false)，确认 GUI 摘要回到 OFF（仅表示未启用 keepalive），再执行普通 gpu_claim
任务结束：按 Agent 自身策略释放 claim；需要保活时调用 set_keepalive(true)
```

这不是 Broker 的强制工作流。Agent 可以不使用 keepalive，也可以在多个正式任务之间自行选择是否恢复；Broker 不替它排队、抢占、续做或重试跨任务动作。

## 最小安全边界

- endpoint 必须一次性 opt-in 一个 registry-sealed keepalive adapter；未配置即完全关闭。
- adapter 只管理 Broker-owned keeper，不接受用户提供的 shell、脚本路径、环境、PID 或任意命令，也不停止正式任务或 foreign process。
- `enabled=true` 只有在整台 endpoint 的 GPU 均可用、没有正式 Lease、没有 unmanaged/conflicting process 时才允许成功；v1 采用 whole-endpoint，不做 per-GPU 启停。
- `enabled=false` 只能作用于 keeper 归属；正式 Lease 或 foreign process 不受影响。
- 内部仍可复用一条覆盖整台 endpoint 的 exclusive keepalive Lease，作为并发互斥和归属记录；它不是用户可见状态机，也不触发 allocator 的自动 handoff。
- collector 继续是实际进程、显存和利用率的事实源；adapter 的动作结果不能替代观测。启停内部可以用一次 fresh observation 避免明显竞态，但不固化为 claim→pause→handoff→resume 流程。
- adapter 不可用、观测过期、状态冲突或存在 foreign process 时 fail closed，并通过现有 audit/error 返回原因。

## 明确不做

- 不增加自动 controller、outbox、fence、通用 priority/preemption、自动恢复或 per-GPU 状态机。
- 不把启停副作用塞进只读 collector。
- 不引入动态 Python 插件或任意远程命令；“可插拔”只表示 endpoint 选择一个代码内注册的 sealed adapter/profile。
- 不用 DRAINING 伪装 keepalive 意图。DRAINING 仍表示运维排空；KEEPALIVE 是独立的观测派生状态。

## 未决点

需要用户在进入实现前确认：是否接受方案 B 的一个显式 `set_keepalive` 原语，以及启停成功是否要求接口等待 keeper 进程被 fresh collector 观测到。除此之外，Agent 的 claim 前后编排不纳入 ServerPilot 的固定流程。

当前文档仍是协作中的推荐设计，尚未成为用户接受的决定。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 当前方向稿落盘

收敛为方案 B（薄型显式 `set_keepalive` 原语）的推荐设计，并留下两个待用户确认的未决点。

### 2026-08-11 — 协作稿快照（09:30）并入

#### GPU Broker Keepalive Extension

##### 当前方向

用户重新收敛了范围：ServerPilot 不需要自动观察队列、暂停占卡、交接正式任务、再自动恢复，也不需要另一套控制器。Agent 已经可以编排自己的申请和释放流程；人类端只需要一个开关按钮和一个可信的占卡状态。

因此，keepalive 应是一个可选的薄能力：ServerPilot 提供状态和一个很小的启停原语，Agent 决定何时调用；正式 claim 的队列、优先级、恢复策略和重试策略仍由 Agent 负责。

##### 候选形状

| 方案 | 形状 | 取舍 |
| --- | --- | --- |
| A：只存意图 | GUI 写入“希望保活”的标记，Agent 轮询后自行启动或停止 keeper；ServerPilot 只显示 collector 看到的结果。 | 改动最少，但按钮不能唤醒未运行的 Agent，意图与 DRAINING 等现有运维状态容易混淆；keeper 进程归因也完全依赖 Agent。 |
| B：薄型显式原语（当前推荐） | 增加一个幂等 `set_keepalive(endpoint, enabled)`，REST、MCP、GUI 和 Agent 共用；ServerPilot 只执行受控 keeper 的启停并返回结果，不根据 claim 自动做任何事。 | 多一个明确的受控入口，但按钮立即可用，Agent 不必自建 SSH/脚本路径；调度工作流仍留在 Agent，边界最清楚。 |

当前建议采用 B，但仍处于协作阶段，未形成用户接受的决定。

##### 公共接口与显示

只新增一个动作和一个派生状态：

```text
set_keepalive(endpoint_id, enabled: bool) -> {enabled, state, reason?}
```

- `enabled=true`：请求在该 endpoint 上启动 Broker-owned keeper。
- `enabled=false`：请求停止该 endpoint 上已登记的 keeper。
- 调用必须幂等；重复启用或停用返回当前结果，不创建第二个 keeper。
- GUI 与 Agent 走同一 REST/MCP 路径，不增加另一套桌面专用逻辑。
- GUI 只显示 `KEEPALIVE` 或现有的非保活状态；动作进行中使用本地 spinner，失败沿用现有 error/audit 展示。
- `KEEPALIVE` 只由 fresh collector 观测到已登记 keeper 后产生；没有 keeper 就是普通状态 `OFF`，不增加 `STARTING`、`STOPPING`、`PAUSED` 等公共状态。

##### Agent 使用方式

Agent 自己决定时机，ServerPilot 不监听 claim 并不自动改变 keeper：

```text
申请前：若状态为 KEEPALIVE，调用 set_keepalive(false)，确认状态为 OFF，再执行普通 gpu_claim
任务结束：按 Agent 自身策略释放 claim；需要保活时调用 set_keepalive(true)
```

这不是 Broker 的强制工作流。Agent 可以不使用 keepalive，也可以在多个正式任务之间自行选择是否恢复；Broker 不替它排队、抢占、续做或重试跨任务动作。

##### 最小安全边界

- endpoint 必须一次性 opt-in 一个 registry-sealed keepalive adapter；未配置即完全关闭。
- adapter 只管理 Broker-owned keeper，不接受用户提供的 shell、脚本路径、环境、PID 或任意命令，也不停止正式任务或 foreign process。
- `enabled=true` 只有在整台 endpoint 的 GPU 均可用、没有正式 Lease、没有 unmanaged/conflicting process 时才允许成功；v1 采用 whole-endpoint，不做 per-GPU 启停。
- `enabled=false` 只能作用于 keeper 归属；正式 Lease 或 foreign process 不受影响。
- 内部仍可复用一条覆盖整台 endpoint 的 exclusive keepalive Lease，作为并发互斥和归属记录；它不是用户可见状态机，也不触发 allocator 的自动 handoff。
- collector 继续是实际进程、显存和利用率的事实源；adapter 的动作结果不能替代观测。启停内部可以用一次 fresh observation 避免明显竞态，但不固化为 claim→pause→handoff→resume 流程。
- adapter 不可用、观测过期、状态冲突或存在 foreign process 时 fail closed，并通过现有 audit/error 返回原因。

##### 明确不做

- 不增加自动 controller、outbox、fence、通用 priority/preemption、自动恢复或 per-GPU 状态机。
- 不把启停副作用塞进只读 collector。
- 不引入动态 Python 插件或任意远程命令；“可插拔”只表示 endpoint 选择一个代码内注册的 sealed adapter/profile。
- 不用 DRAINING 伪装 keepalive 意图。DRAINING 仍表示运维排空；KEEPALIVE 是独立的观测派生状态。

##### 未决点

需要用户在进入实现前确认：是否接受方案 B 的一个显式 `set_keepalive` 原语，以及启停成功是否要求接口等待 keeper 进程被 fresh collector 观测到。除此之外，Agent 的 claim 前后编排不纳入 ServerPilot 的固定流程。

当前文档仍是协作中的推荐设计，尚未成为用户接受的决定。

### 2026-08-11 — 协作稿快照（08:55）并入

#### Gpu Broker Keepalive Extension

#### Gpu Broker Keepalive Extension

##### 当前问题与边界

目标是给 ServerPilot 增加一个可选的、极轻量的 GPU keepalive 附加能力：开发机空闲时运行 Broker 自己的保活程序；正式 claim 到达时自动让出 GPU；正式任务结束且端点重新空闲后再恢复。功能必须默认关闭、按 endpoint opt-in，并且不能影响正式任务或 foreign process。

已知边界：ServerPilot 的 lease 只协调归属，collector 只做固定只读观测；GUI、CLI、MCP 仍走 REST；不开放任意 SSH、shell、路径、环境或 PID。这个功能因此只能是一个受控的 Broker-owned keepalive adapter，不是通用远程执行或通用抢占器。

##### 候选方案与取舍

| 方案 | 结果 | 原因 |
| --- | --- | --- |
| 推荐：一个公共派生状态 + 真实 exclusive Lease + sealed keepalive adapter | 保留 | 最少新增面；现有 lease 唯一约束、collector 证据和 allocator 都可复用。 |
| 把 start/stop 塞进 collector | 拒绝 | collector 契约是只读；观测与副作用耦合会破坏 fail-closed 和审计边界。 |
| 新建 fence/keepalive 专用表 | 拒绝 | 与现有 lease/resource 所有权重复，增加 crash/reconcile 路径。 |
| 通用 priority/preemption 框架 | 拒绝 | 需求只有 keepalive 这一种可让出资源，不应把它扩展成任意抢占。 |
| 动态 Python 插件或用户命令 | 拒绝 | 供应链和命令注入面过大；可插拔应是 code-owned、registry-sealed 的 adapter capability。 |
| per-GPU keepalive | 暂缓 | 会引入部分端点恢复、碎片化和更多竞态；v1 采用 whole-endpoint。 |

##### 推荐的最小形状（当前建议，尚未成为用户接受的决定）

1. **唯一新增公共 GPU 派生状态：KEEPALIVE。** 不增加新的 Lease/GPU 状态列，也不增加 fence/outbox 或公开 MCP 开关。KEEPALIVE 只在 fresh observation 证明端点全卡由已登记的 keepalive 进程占用时出现；未知、过期、非托管进程或冲突均保持 fail closed。

2. **复用真实所有权。** 每个启用的 endpoint 最多一条覆盖该端点全部 GPU 的 keepalive exclusive Lease；其 AllocationRequest.priority_class 为 keepalive，仅用于识别 Broker 的保活请求，不形成通用优先级规则。Lease、LeaseResource、唯一 active-GPU 约束、审计和释放语义全部复用现有模型。

3. **adapter 是独立 sealed capability。** endpoint 只保存 nullable keepalive_adapter（空值即关闭）；registry 只暴露固定、版本化的一个幂等方法 reconcile(endpoint, desired=running|stopped)。远端固定 entrypoint 只接收 desired literal（running 或 stopped）；不接收 GPU UUID、shell/path/env/PID，也不停止普通任务。collector 仍是实际进程、显存和利用率的唯一事实源，adapter 返回值不能替代 fresh observation。

4. **whole-endpoint v1。** 端点上任一正式 Lease 存在时，整机 keepalive 必须暂停；keepalive 恢复时也覆盖全部 GPU。这样只新增一个状态，不处理 per-GPU 分片。

##### 正式 claim 的原子 handoff

1. allocator 先按现有规则 dry-select AVAILABLE；可用资源优先，不因 keepalive 主动抖动。
2. 只有当 dry selection 证明约束完整满足、KEEPALIVE 是唯一阻塞且不存在其他可行 endpoint 时，正式请求保持 QUEUED，keepalive Lease 继续 active；不提前释放，避免 ownership gap。
3. collector tick 之后的小型 controller 在事务外调用 sealed adapter reconcile(endpoint, desired=stopped)，以同一 keepalive lease/request 生成幂等 key；然后立即触发该 endpoint 的 fresh collect。
4. 只有当观测时间严格晚于本次 desired=stopped 调用、keeper process 已消失，并且现有 _eligible_gpus 的全部安全条件通过（包括 VRAM、health、staleness 和 foreign process），BrokerService 才在同一 DB 事务中终止 keepalive Lease/资源并运行现有 allocator，产生正式的 HELD Lease。若证据不满足，请求继续排队，下一 tick 重试。
5. fresh observation 仍显示 foreign process、冲突、未知或 stale 时，不能 handoff、不能 kill；记录可诊断的 fail-closed 结果。controller 崩溃只需要用同一幂等 key 重试相同 desired=stopped 的 reconcile，不需要 outbox 或额外状态机。
6. 请求在 desired=stopped 期间取消时，不得发放正式 Lease；后续 reconcile 重新判断是否需要恢复 keepalive。

##### 空闲后的恢复

1. 释放正式 Lease 后，allocator 先处理现有正式队列；只有端点 fresh、全卡 AVAILABLE，且没有可在该端点满足的正式需求/预约时，才恢复 keepalive。
2. controller 在事务内先 reserve/创建覆盖全卡的 keepalive Lease（priority_class=keepalive），事务外调用幂等 reconcile(endpoint, desired=running)，下一次 fresh collect 观察并 bind keeper process。desired=running 未被观测确认时释放该保活 reservation 并 fail closed，不阻塞正式任务的正确性。
3. 任何正式 Lease、维护、排空、telemetry stale 或冲突都会阻止恢复；恢复失败不会触碰正式任务。

##### 组件职责与观测

- BrokerService：唯一决定 KEEPALIVE 派生、dry selection、handoff 条件、Lease 生命周期和队列公平性。
- keepalive adapter：仅执行固定的 Broker-owned reconcile(endpoint, desired=running|stopped)；不拥有调度状态。
- collector：固定只读探针；提供 desired=stopped 后的时间屏障、进程归属，以及现有 eligibility 所需的 VRAM、health、staleness、foreign-process 证据。
- controller：复用 collector tick 的一个幂等 reconcile 步骤，不是第二控制面；不添加用户可见开关。
- audit：记录计划、幂等动作、fresh-observation barrier、成功/失败原因和重试；adapter 的远程响应只作为动作结果，最终以 collector 证据为准。

##### 实施边界与验证顺序

先扩展 endpoint opt-in、priority_class=keepalive 的严格 schema 和 sealed adapter registry；再实现 KEEPALIVE 派生与 whole-endpoint dry selection；随后接入 collector tick 的 desired=stopped→fresh collect→同事务 handoff、以及空闲后的 reserve→desired=running→observe/bind。测试必须覆盖：普通可用资源优先、保持 QUEUED 且无 ownership gap、观测时间屏障、foreign process/stale fail closed、重复 reconcile、崩溃后重试、取消竞态、恢复不抢占正式任务。默认未配置 adapter 时，现有调度路径和性能完全不变。

目前只是协作阶段的推荐设计；用户尚未接受，因此不写 accepted decision。

### 2026-08-11 — 协作稿快照（08:50）并入

#### Gpu Broker Keepalive Extension

##### 当前问题与边界

目标是给 ServerPilot 增加一个可选的、极轻量的 GPU keepalive 附加能力：开发机空闲时运行 Broker 自己的保活程序；正式 claim 到达时自动让出 GPU；正式任务结束且端点重新空闲后再恢复。功能必须默认关闭、按 endpoint opt-in，并且不能影响正式任务或 foreign process。

已知边界：ServerPilot 的 lease 只协调归属，collector 只做固定只读观测；GUI、CLI、MCP 仍走 REST；不开放任意 SSH、shell、路径、环境或 PID。这个功能因此只能是一个受控的 Broker-owned keepalive adapter，不是通用远程执行或通用抢占器。

##### 候选方案与取舍

| 方案 | 结果 | 原因 |
| --- | --- | --- |
| 推荐：一个公共派生状态 + 真实 exclusive Lease + sealed keepalive adapter | 保留 | 最少新增面；现有 lease 唯一约束、collector 证据和 allocator 都可复用。 |
| 把 start/stop 塞进 collector | 拒绝 | collector 契约是只读；观测与副作用耦合会破坏 fail-closed 和审计边界。 |
| 新建 fence/keepalive 专用表 | 拒绝 | 与现有 lease/resource 所有权重复，增加 crash/reconcile 路径。 |
| 通用 priority/preemption 框架 | 拒绝 | 需求只有 keepalive 这一种可让出资源，不应把它扩展成任意抢占。 |
| 动态 Python 插件或用户命令 | 拒绝 | 供应链和命令注入面过大；可插拔应是 code-owned、registry-sealed 的 adapter capability。 |
| per-GPU keepalive | 暂缓 | 会引入部分端点恢复、碎片化和更多竞态；v1 采用 whole-endpoint。 |

##### 推荐的最小形状（当前建议，尚未成为用户接受的决定）

1. **唯一新增公共 GPU 派生状态：KEEPALIVE。** 不增加新的 Lease/GPU 状态列，也不增加 fence/outbox 或公开 MCP 开关。KEEPALIVE 只在 fresh observation 证明端点全卡由已登记的 keepalive 进程占用时出现；未知、过期、非托管进程或冲突均保持 fail closed。

2. **复用真实所有权。** 每个启用的 endpoint 最多一条覆盖该端点全部 GPU 的 keepalive exclusive Lease；其 AllocationRequest.priority_class 为 keepalive，仅用于识别 Broker 的保活请求，不形成通用优先级规则。Lease、LeaseResource、唯一 active-GPU 约束、审计和释放语义全部复用现有模型。

3. **adapter 是独立 sealed capability。** endpoint 只保存 nullable keepalive_adapter（空值即关闭）；registry 只暴露固定、版本化的一个幂等方法 reconcile(endpoint, desired=running|stopped)。远端固定 entrypoint 只接收 desired literal（running 或 stopped）；不接收 GPU UUID、shell/path/env/PID，也不停止普通任务。collector 仍是实际进程、显存和利用率的唯一事实源，adapter 返回值不能替代 fresh observation。

4. **whole-endpoint v1。** 端点上任一正式 Lease 存在时，整机 keepalive 必须暂停；keepalive 恢复时也覆盖全部 GPU。这样只新增一个状态，不处理 per-GPU 分片。

##### 正式 claim 的原子 handoff

1. allocator 先按现有规则 dry-select AVAILABLE；可用资源优先，不因 keepalive 主动抖动。
2. 只有当 dry selection 证明约束完整满足、KEEPALIVE 是唯一阻塞且不存在其他可行 endpoint 时，正式请求保持 QUEUED，keepalive Lease 继续 active；不提前释放，避免 ownership gap。
3. collector tick 之后的小型 controller 在事务外调用 sealed adapter reconcile(endpoint, desired=stopped)，以同一 keepalive lease/request 生成幂等 key；然后立即触发该 endpoint 的 fresh collect。
4. 只有当观测时间严格晚于本次 desired=stopped 调用、keeper process 已消失，并且现有 _eligible_gpus 的全部安全条件通过（包括 VRAM、health、staleness 和 foreign process），BrokerService 才在同一 DB 事务中终止 keepalive Lease/资源并运行现有 allocator，产生正式的 HELD Lease。若证据不满足，请求继续排队，下一 tick 重试。
5. fresh observation 仍显示 foreign process、冲突、未知或 stale 时，不能 handoff、不能 kill；记录可诊断的 fail-closed 结果。controller 崩溃只需要用同一幂等 key 重试相同 desired=stopped 的 reconcile，不需要 outbox 或额外状态机。
6. 请求在 stop 期间取消时，不得发放正式 Lease；后续 reconcile 重新判断是否需要恢复 keepalive。

##### 空闲后的恢复

1. 释放正式 Lease 后，allocator 先处理现有正式队列；只有端点 fresh、全卡 AVAILABLE，且没有可在该端点满足的正式需求/预约时，才恢复 keepalive。
2. controller 在事务内先 reserve/创建覆盖全卡的 keepalive Lease（priority_class=keepalive），事务外调用幂等 reconcile(endpoint, desired=running)，下一次 fresh collect 观察并 bind keeper process。start 未被观测确认时释放该保活 reservation 并 fail closed，不阻塞正式任务的正确性。
3. 任何正式 Lease、维护、排空、telemetry stale 或冲突都会阻止恢复；恢复失败不会触碰正式任务。

##### 组件职责与观测

- BrokerService：唯一决定 KEEPALIVE 派生、dry selection、handoff 条件、Lease 生命周期和队列公平性。
- keepalive adapter：仅执行固定的 Broker-owned reconcile(endpoint, desired=running|stopped)；不拥有调度状态。
- collector：固定只读探针；提供 desired=stopped 后的时间屏障、进程归属，以及现有 eligibility 所需的 VRAM、health、staleness、foreign-process 证据。
- controller：复用 collector tick 的一个幂等 reconcile 步骤，不是第二控制面；不添加用户可见开关。
- audit：记录计划、幂等动作、fresh-observation barrier、成功/失败原因和重试；adapter 的远程响应只作为动作结果，最终以 collector 证据为准。

##### 实施边界与验证顺序

先扩展 endpoint opt-in、priority_class=keepalive 的严格 schema 和 sealed adapter registry；再实现 KEEPALIVE 派生与 whole-endpoint dry selection；随后接入 collector tick 的 desired=stopped→fresh collect→同事务 handoff、以及空闲后的 reserve→desired=running→observe/bind。测试必须覆盖：普通可用资源优先、保持 QUEUED 且无 ownership gap、观测时间屏障、foreign process/stale fail closed、重复 stop/start、崩溃后重试、取消竞态、恢复不抢占正式任务。默认未配置 adapter 时，现有调度路径和性能完全不变。

目前只是协作阶段的推荐设计；用户尚未接受，因此不写 accepted decision。

### 2026-08-11 — 链内最早协作稿快照（08:35）并入

##### 当前问题与边界

目标是给 ServerPilot 增加一个可选的、极轻量的 GPU keepalive 附加能力：开发机空闲时运行 Broker 自己的保活程序；正式 claim 到达时自动让出 GPU；正式任务结束且端点重新空闲后再恢复。功能必须默认关闭、按 endpoint opt-in，并且不能影响正式任务或 foreign process。

已知边界：ServerPilot 的 lease 只协调归属，collector 只做固定只读观测；GUI、CLI、MCP 仍走 REST；不开放任意 SSH、shell、路径、环境或 PID。这个功能因此只能是一个受控的 Broker-owned keepalive adapter，不是通用远程执行或通用抢占器。

##### 候选方案与取舍

| 方案 | 结果 | 原因 |
| --- | --- | --- |
| 推荐：一个公共派生状态 + 真实 exclusive Lease + sealed keepalive adapter | 保留 | 最少新增面；现有 lease 唯一约束、collector 证据和 allocator 都可复用。 |
| 把 start/stop 塞进 collector | 拒绝 | collector 契约是只读；观测与副作用耦合会破坏 fail-closed 和审计边界。 |
| 新建 fence/keepalive 专用表 | 拒绝 | 与现有 lease/resource 所有权重复，增加 crash/reconcile 路径。 |
| 通用 priority/preemption 框架 | 拒绝 | 需求只有 keepalive 这一种可让出资源，不应把它扩展成任意抢占。 |
| 动态 Python 插件或用户命令 | 拒绝 | 供应链和命令注入面过大；可插拔应是 code-owned、registry-sealed 的 adapter capability。 |
| per-GPU keepalive | 暂缓 | 会引入部分端点恢复、碎片化和更多竞态；v1 采用 whole-endpoint。 |

##### 推荐的最小形状（当前建议，尚未成为用户接受的决定）

1. **唯一新增公共 GPU 派生状态：KEEPALIVE。** 不增加新的 Lease/GPU 状态列，也不增加 fence/outbox 或公开 MCP 开关。KEEPALIVE 只在 fresh observation 证明端点全卡由已登记的 keepalive 进程占用时出现；未知、过期、非托管进程或冲突均保持 fail closed。

2. **复用真实所有权。** 每个启用的 endpoint 最多一条覆盖该端点全部 GPU 的 keepalive exclusive Lease；其 AllocationRequest.priority_class 为 keepalive，仅用于识别 Broker 的保活请求，不形成通用优先级规则。Lease、LeaseResource、唯一 active-GPU 约束、审计和释放语义全部复用现有模型。

3. **adapter 是独立 sealed capability。** endpoint 只保存 nullable keepalive_adapter（空值即关闭）；registry 只暴露固定、版本化的 start、stop、reconcile 三个幂等动作。动作参数为 Broker 生成的受限结构和已验证 GPU UUID 集合；adapter 不接收任意 shell/path/env/PID，也不停止普通任务。collector 仍是实际进程、显存和利用率的唯一事实源，adapter 返回值不能替代 fresh observation。

4. **whole-endpoint v1。** 端点上任一正式 Lease 存在时，整机 keepalive 必须暂停；keepalive 恢复时也覆盖全部 GPU。这样只新增一个状态，不处理 per-GPU 分片。

##### 正式 claim 的原子 handoff

1. allocator 先按现有规则 dry-select AVAILABLE；可用资源优先，不因 keepalive 主动抖动。
2. 只有当 dry selection 证明约束完整满足、KEEPALIVE 是唯一阻塞且不存在其他可行 endpoint 时，正式请求保持 QUEUED，keepalive Lease 继续 active；不提前释放，避免 ownership gap。
3. collector tick 之后的小型 controller 在事务外调用 sealed adapter stop，以同一 keepalive lease/request 生成幂等 key；然后立即触发该 endpoint 的 fresh collect。
4. 只有当观测时间严格晚于本次 stop 调用，并且所有 keeper process、显存占用和计算利用率都清空，BrokerService 才在同一 DB 事务中终止 keepalive Lease/资源并运行现有 allocator，产生正式的 HELD Lease。若证据不满足，请求继续排队，下一 tick 重试。
5. fresh observation 仍显示 foreign process、冲突、未知或 stale 时，不能 handoff、不能 kill；记录可诊断的 fail-closed 结果。controller 崩溃只需要用同一幂等 key 重试 stop，不需要 outbox 或额外状态机。
6. 请求在 stop 期间取消时，不得发放正式 Lease；后续 reconcile 重新判断是否需要恢复 keepalive。

##### 空闲后的恢复

1. 释放正式 Lease 后，allocator 先处理现有正式队列；只有端点 fresh、全卡 AVAILABLE，且没有可在该端点满足的正式需求/预约时，才恢复 keepalive。
2. controller 在事务内先 reserve/创建覆盖全卡的 keepalive Lease（priority_class=keepalive），事务外调用幂等 start，下一次 fresh collect 观察并 bind keeper process。start 未被观测确认时释放该保活 reservation 并 fail closed，不阻塞正式任务的正确性。
3. 任何正式 Lease、维护、排空、telemetry stale 或冲突都会阻止恢复；恢复失败不会触碰正式任务。

##### 组件职责与观测

- BrokerService：唯一决定 KEEPALIVE 派生、dry selection、handoff 条件、Lease 生命周期和队列公平性。
- keepalive adapter：仅执行固定的 Broker-owned start/stop/reconcile；不拥有调度状态。
- collector：固定只读探针；提供 stop 后的时间屏障、进程归属、VRAM/utilization 证据。
- controller：复用 collector tick 的一个幂等 reconcile 步骤，不是第二控制面；不添加用户可见开关。
- audit：记录计划、幂等动作、fresh-observation barrier、成功/失败原因和重试；adapter 的远程响应只作为动作结果，最终以 collector 证据为准。

##### 实施边界与验证顺序

先扩展 endpoint opt-in、priority_class=keepalive 的严格 schema 和 sealed adapter registry；再实现 KEEPALIVE 派生与 whole-endpoint dry selection；随后接入 collector tick 的 stop→fresh collect→同事务 handoff、以及空闲后的 reserve→start→observe/bind。测试必须覆盖：普通可用资源优先、保持 QUEUED 且无 ownership gap、观测时间屏障、foreign process/stale fail closed、重复 stop/start、崩溃后重试、取消竞态、恢复不抢占正式任务。默认未配置 adapter 时，现有调度路径和性能完全不变。

目前只是协作阶段的推荐设计；用户尚未接受，因此不写 accepted decision。

### 2026-08-29 — slug 从旧 case 标题改为按 identity 派生（撞车修复）

本文件此前落在 `docs/teamwork/discussions/gpu-broker-keepalive-extension.md`，与 `docs/teamwork/plans/gpu-broker-keepalive-extension.md` 同 slug：两个 slug 都是从旧 case 标题机械 title-case 来的，不是从各自 identity 派生的。两侧 identity 不同——本文件是仍未决的选项空间（方案 A / 方案 B 未定，文末两个未决点仍开放），plans 侧是已选方向的有序实施步骤——因此不合并，改为按 identity 派生 slug：本文件迁至 `discussions/keepalive-primitive-shape-and-toggle-semantics.md`。正文与未决点未改。
