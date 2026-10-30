---
status: active
superseded-by:
created: 2026-08-30
updated: 2026-08-30
---

# Record: 端点连通性状态的两条轴与单一判据

- Subject identity: ServerPilot 端点 `monitor_status` 的语义边界——「我们知不知道这台机器的情况」与「人有没有让它接活」如何分开表达，以及连通性用哪一个判据。

## 当前结论

用户报告「网络波动导致可以正常连接的服务器显示无响应」。根因不是阈值太敏感，而是两件事：

1. **判据用错了对象。** 采集器单次 SSH 探测失败即写入 `provider_state.last_error`，而 monitor 推导只要它非空就判 `ERROR`。`last_error` 描述的是「最近一次尝试」这个事件，被当成了「这台主机」的状态。同一份代码里 `_endpoint_is_unreachable`（service.py）早就写对了形状——有错 **且** `last_success_at` 已超过 `stale_after_seconds`——但状态推导没有复用它。
2. **一个枚举混了两条正交的轴。** `ONLINE / PENDING / STALE / ERROR` 是轴 A（连通性），`DISABLED / DRAINING` 是轴 B（人为设定，来自 `enabled` 与 `lifecycle_state`）。压成一个枚举后，任何消费者写 `!= ONLINE` 都会同时命中两条轴，于是「已停用」被显示成「连接失败」。这不是 UI 粗心，是枚举形状逼出来的。

附带发现：`_endpoint_monitor_status` 与 `_host_capacity_cards` 是活路径（`snapshot()` 内联块）的**零调用重复副本**，且语义已漂移（缺 host-only 探针那条特例分支）。已删除。

**选定修法（净删除，未新增任何机制）**

- 轴 A 只保留一个判据：`last_success_at` 的年龄 > `stale_after_seconds`。新鲜即 `ONLINE`，即使 `last_error` 非空；不新鲜时有错为 `ERROR`、无错为 `STALE`。`last_error` 降级为纯原因文本。
- `collector_unreachable` 告警改用同一判据，避免「状态 ONLINE 但告警栏挂 warning」。
- 轴 B 由消费者直接读 `enabled` / `lifecycle_state`，不从 monitor 枚举反推。
- **枚举值本身不改**：它是 REST 快照、MCP `cpu_only_servers` 投影、两个桌面端与 fixtures 的公开契约，改值的收益只是省一个字符串，不值。语义收敛靠「推导只有一个判据」实现。
- 未新增配置项、计数器、新状态值、重试包装或兼容分支。

**已观测的结果**（非推断）

- 抖动场景端到端自洽：单次探测失败后 monitor 仍 `ONLINE`、无告警、该主机的 GPU 仍可分配——这与 `_eligible_gpus` 的既有行为一致，改动只是让 monitor 不再对此撒谎。
- 时间窗对齐：`interval=10s / stale=30s` 下，monitor 转非 `ONLINE` 与 GPU 转 `UNKNOWN_STALE` 发生在同一时刻（`age=20s → ONLINE/AVAILABLE`；`age=30s → ERROR/UNKNOWN_STALE`）。方向安全：准入先关，显示后红。
- fail-closed 未被放松：`_gpu_state`、`_eligible_gpus` 从不读 `last_error`；`_validate_keepalive_observation`、`_endpoint_is_unreachable` 未改动。
- Python 304 passed；Swift 66 tests passed；`build-macos-app.sh` + `verify-macos-app.sh` PASS，根目录仅一份 `ServerPilot.app`。

**残留的 unknown / 未处理**

- `_validate_keepalive_observation`（service.py:2327）与 `release_empty_conflicted_lease`（service.py:7439）仍把 `last_error is not None` 当判据。它们是操作后要求「一次干净的完整观测」的 fail-closed 校验，比 monitor 严格是**有意**的；但一次抖动会让这两条人工恢复路径需要重试。是否也收敛到年龄判据，**未决**。
- `endpoint_snapshot` 里 host-only 探针那条特例仍绕开年龄判据（既有代码，本次未加剧）。
- `derive_state` 与 `_gpu_state` 是为避开 N+1 查询而存在的一对重复副本，本次未合并。
- Windows UI (`app.js:122`) 仍把 `PENDING/STALE/ERROR` 塌缩为一个「连接失败」样式。它已正确摘出轴 B，故不属于本次修的 bug，但与 macOS 现在的六态文案不再等价。
- 告警滞后：真实宕机后 `collector_unreachable` 最晚可能比 monitor 转 `ERROR` 晚约 60s（退避探测周期）才出现。有滞后，无空洞。

## History

### 2026-08-30 — 根因从「阈值太敏感」修正为「两条轴 + 判据用错对象」，修法执行并通过评审

初始假设是采集阈值过于敏感、可加连续失败容忍。读码后否定：真正的缺陷是 `last_error`（事件）被当作状态判据，以及枚举把两条正交的轴压成一个值。修法因此是删除一条判据、复用已有的年龄判据，而不是新增容忍计数。

三条并行评审：fail-closed 对抗审查 `ACCEPT`（0 blocker）；单一路径审查与跨层一致性审查均 `REVISE`，指出的残留（`ServerPilot.swift` 详情页页脚、`CoreModels.swift` 的 `monitorDetail` 把 `last_error` 非空当连接失败证据、`docs/DESIGN_SYSTEM.md` 仍收录已不再产出的「无响应」）已在同一轮修完并重新构建验证；其余残留列在上面的 unknown 段。

评审同时报出一条与本主题无关的 major：同一工作区里未提交的容器配额改动使**展示口径**（cgroup 配额）与 `_eligible_gpus` 的**准入口径**（整机 `cpu_count` / `memory_total_mib`）分叉。属于另一条工作线，未处理。
