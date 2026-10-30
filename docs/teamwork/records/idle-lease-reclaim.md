---
status: active
superseded-by:
created: 2026-08-21
updated: 2026-08-21
---

# Record: 空闲租约两阶段回收与占卡可见性

## Intended result and selected direction

- Subject identity: ServerPilot 空闲 GPU 回收（Agent 释放不及时 / 空闲清理 / 占卡保护）
- Authoritative owner: Root agent
- Intended result: 让被租约锁住但没有任何计算进程的 GPU 能自动还回可申请池，并让"空转被占"在界面上可见
- Selected direction: 用户于 2026-08-21 选择 **两阶段：先告警后回收**；占卡方向选择"空闲租约期间没有占卡保护"
- User-settled constraints（未被推翻）:
  - 不新增 MCP 工具，默认发现结果保持 3 个
  - 不连真实 GPU 执行写操作
  - 不建分支、不开 PR、不自动提交

## 诊断（实机证据，非推测）

采样时刻 `2026-08-21T13:27:21Z`，实机租约 `efe991e96a922380c3028bc33d4165af`：

| 事实 | 值 |
| --- | --- |
| 签发时间 | `2026-08-21T11:52:06Z`（1.59 小时前） |
| `expires_at` | `null` |
| 状态 | `ACTIVE`，`runtime_state=ASSIGNED`（从未观测到运行） |
| 持有 | 4 张卡，占全场 12 张的 33% |
| 这 4 张卡 | `util=0%`、`mem_used=0MiB`，**近 10 分钟均值同样全 0** |
| 最后心跳 | `12:10:04`，之后 1.29 小时无变化 |

**根因一**：`_reconcile_leases`（`service.py`）已有完整回收逻辑——过期且无进程 → `EXPIRED_EMPTY` 并释放资源。但入口条件是 `expires_at is not None`，而 `/api/v1/routine/claims` 传 `persistent_lease=True`，在 `create_request` 中把 `expires_at` 显式置 `None`。因此**每个 `gpu_apply` 租约都永远进不了回收器**。这是 `AGENTS.md` 记录的政策，不是 bug；现有 `EXPIRED_EMPTY` 的语义（`expired without observed process`）正是所需的清理，只是够不着。

**根因二**：`_gpu_public_projection` 把 `LEASED_IDLE` / `HELD` / `RUNNING_MANAGED` / `ORPHANED_BUSY` / `CONFLICT` / `BUSY_UNMANAGED` 折叠成同一句 `任务使用中`，违反 `DESIGN_SYSTEM.md` §5 状态用语表。人要做空闲清理，第一步得能看见谁在空转。（第一轮设计契约审计遗漏，编号 D7。）

**组合效应**：空闲的工作负载租约同时关掉了占卡保护——租约持有的卡上占卡不会运行，于是既不干活、也不挡外人、还不能被重新分配。

## Delivered

### 两阶段回收（`_reconcile_idle_lease`）

挂在 `_reconcile_leases` 内，由每次采集摄入触发。规则：

1. 只处理 `expires_at is None` 的工作负载租约。**声明了 duration 的租约不受影响**——那个时间窗是用户的明示意图，任务可能合法地在长 CPU 阶段保持零 GPU 进程。
2. 有进程，或 `_resources_have_fresh_telemetry` 为假 → 清零 `leases.idle_since` 并消解告警。
3. 首次观测到"新鲜且零进程" → 记 `idle_since`。
4. 超过 `idle_lease_alert_seconds`（默认 600s）→ `idle_lease` warning 告警，**仍持有 GPU**。
5. 超过 `idle_lease_reclaim_seconds`（默认 3600s）→ `EXPIRED_EMPTY`，释放资源，审计 `lease.idle_reclaimed`。

**关键安全属性**：采集变 stale 会**清零**计时而不是冻结，所以 `idle_since` 始终测量一段完整观测到的空闲窗口，采集中断永远不会累积成回收。这与 GPU 准入的 fail-closed 一致。

新增 `leases.idle_since` 列（migration `20260821_0030`，升降级往返已验证）。新增 `idle_lease_alert_seconds` / `idle_lease_reclaim_seconds` 配置，附顺序校验（reclaim 不得小于 alert）。`idle_lease` 用独立消解器，不并入 `LEASE_SCOPED_BLOCKING_ALERT_TYPES`——它不阻断准入，不应被终态租约修复路径扫掉。

### 状态可见性（D7）

按 §5 拆分：`任务占用`（真在跑） / `占卡`（已申领未观测到任务） / `未归属占用` / `归属冲突`。桌面端与 Web 都优先消费服务端 `public_status`，自动生效；两处本地兜底文案与全部夹具同步。

## 未交付且需要单独决策

**占卡在空闲租约期间的保护**。用户选了这个方向，但直接实现有 OOM 风险：占卡固定占 80% 显存（`TARGET_MEMORY_FRACTION`），若租约的 Agent 稍后真的启动工作负载会直接失败——大任务的准备（同步代码、装环境、检查数据集）超过任何合理宽限期都很常见。

安全形式需要"缩小占位足迹 + 观测到真实进程即让位"，而足迹常量烧在**远端 sealed helper** 里（`server_keepalive.py` 随 helper 部署），改动意味着 helper 协议 v3 → 新版本 + 远端重新部署，且本机无法验证。

本轮的替代效果：回收把无保护窗口从**无上限**压到宽限期（默认 1 小时），窗口结束后现有占卡自动重新覆盖该卡——`keepalive_owned_gpus` 本就只在未分配的卡上运行。

## Verification

- `uv run --reinstall-package serverpilot pytest` → **481 passed**（本轮前 476，新增 5 个测试）
- 新增测试覆盖：两阶段告警→回收全流程、进程出现清零计时、**stale 采集清零计时而非回收**、带 duration 的租约不受影响、§5 状态用语逐条
- `uv run ruff check .` → All checks passed
- migration `upgrade → downgrade → upgrade` 往返干净
- `zsh desktop/build-macos-app.sh` + `zsh desktop/verify-macos-app.sh` → PASS
- `git diff --check` 干净

## Evidence limits

- 默认阈值（600s 告警 / 3600s 回收）是我按"连续零进程"的强度选的，未经现场调优；可按 endpoint 负载模式在 inventory 中调整。
- 回收依赖 collector 运行——`_reconcile_leases` 由采集摄入触发。采集停摆时不会回收（这正是 fail-closed 的意图）。
- 本机无 XCTest，Swift 侧改动（两处兜底文案、夹具）未经单测验证，仅经 build + verify 脚本。
- 未连真实 GPU 执行任何写操作。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-21 — 交付与验收落盘

空闲租约两阶段回收与占卡可见性已交付；验收含 `uv run --reinstall-package serverpilot pytest`（481 passed）、Ruff、migration 升降级往返与 macOS build/verify PASS。
