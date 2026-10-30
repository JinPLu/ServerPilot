---
status: active
superseded-by:
created: 2026-08-30
updated: 2026-08-30
---

# Record: 端点 monitor 状态的两条正交轴拆分

- Subject identity: `endpoint_snapshot` 对外的 `monitor` 状态（ONLINE/PENDING/STALE/ERROR + DISABLED/DRAINING）该由什么判据推导，以及消费者该读哪个字段

## Current synthesis（已观测的根因 + 已落地的修法）

**根因（已观测）**：端点的 monitor 枚举把两条正交的轴压进一个值。

- 轴 A「我们现在知不知道这台机器的情况」= ONLINE / PENDING / STALE / ERROR。
- 轴 B「人有没有让它接活」= DISABLED / DRAINING，来自 `endpoint.enabled` 与 `lifecycle_state`。

这带来两个独立问题：

1. 轴 A 的判据本身用错了时钟。改动前 ERROR 由单次 SSH 探测失败写入 `provider_state.last_error` 直接触发——即"最近一次尝试是否失败"；而 GPU 卡那一侧的可分配性判据用的是"telemetry 距今是否超过 `stale_after_seconds`（30s）"，即"最近一次成功有多旧"。两个时钟不一致，实测会出现"主机显示无响应、同时 4 张卡空闲可分配，`gpu_apply` 也确实分得出去"这种界面与事实矛盾的场景。
2. 轴 A、轴 B 挤在一个枚举里，导致消费者只要写 `!= ONLINE` 就会同时命中两条轴——把"运营侧主动停用/暂停接活"的机器，误显示成"连接失败"。

**选定修法（净删除，未新增机制，已落地于 `src/serverpilot/service.py`）**：

- 轴 A 只保留一个判据：`last_success_at` 的年龄是否超过 `stale_after_seconds`——与 GPU 卡侧、与既有的 `_endpoint_is_unreachable`（service.py:1135）判据形状同构，改动前后 ERROR 的真值条件现在与 `_endpoint_is_unreachable` 严格等价。
- `last_error` 降级为纯原因文本，不再直接决定枚举值；只在"已经 stale”的前提下用它来区分 STALE（无 error）与 ERROR（有 error）。
- 轴 B 不再经 monitor 枚举转译——消费者（macOS 桌面端 `statusWord`/`statusColor`/连接失败过滤器）改为直接读 `enabled` / `lifecycle_state`，或者在轴 A 判断之外单独判断 DISABLED/DRAINING。
- 告警（`collector_unreachable`，service.py:5768-5798）同步换成同一判据：`last_success_at` 未过期时，单次探测失败只记 `last_error`、不产生告警。
- 枚举值本身（ONLINE/PENDING/STALE/ERROR/DISABLED/DRAINING）没有改——它是 REST/MCP/桌面端/fixtures 的公开契约，改值的迁移成本（同步四端 + 所有 fixture）大于把判据修对的收益，本轮只改判据、不改契约形状。

**顺带删除的死代码（已观测，非推断）**：`_endpoint_monitor_status` 与 `_host_capacity_cards` 全仓 grep 零调用，语义已被 snapshot 内联的 `host_capacity_payloads`（service.py:4472-4548）取代，本轮一并删除。

**审查结论（已执行的对抗性复核）**：ACCEPT。GPU 分配路径（`_gpu_state`/`_eligible_gpus`）与 keepalive 校验（`_validate_keepalive_observation`）均不引用 `ProviderState`/`last_error`/`monitor`，改动没有触及；`_endpoint_is_unreachable` 及其两个调用点（删服务器、结算）不在 diff 内。量化实跑（interval=10s / stale=30s）显示 monitor 变 ERROR 与 GPU 变 `UNKNOWN_STALE` 在同一时刻发生，方向安全：不会出现"界面显示在线且可分配、实际不可用"。

**残留 unknown（已标注，未验证）**：
- 告警（而非 monitor 状态本身）比 ERROR 状态最多滞后约 60s 才产生（受 `DEGRADED_ENDPOINT_PROBE_SECONDS` 退避影响）；恢复清除路径未变，不存在挂上清不掉的场景，但用户可见的"告警栏"与"状态色"在这 60s 窗口内可能不同步——推断，未经用户确认是否可接受。
- 同一份改动附带引入了 CPU/内存展示口径（cgroup 配额）与 GPU 准入口径（整机读数）的分叉，以及"可用 CPU"在 capacity 展示（利用率推算）与 host_capacity 卡片（负载推算）之间的两套算法并存——这是审查中发现的独立问题，不属于 monitor 轴拆分本身，本文件只记录、不在此展开。
- 容器场景下这套判据的真实 cgroup 读数未做真实服务器验证（规则不允许连接真实 GPU 服务器）。

## History

### 2026-08-30 — 首次创建

记录本轮"端点 monitor 状态两条正交轴拆分"的根因定位、选定修法、审查结论与残留 unknown。
