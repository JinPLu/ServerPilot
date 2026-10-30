---
status: active
superseded-by:
created: 2026-08-31
updated: 2026-08-31
---

# Record: 占卡程序与 agent 工作负载在同一张卡上相撞（p8908 事件）

- Subject identity: 「空闲占卡（keepalive）把 80% 显存占到 agent 正在跑的卡上」这一类事故的根因与判据。

## 当前结论

事故不是「占卡程序有 bug」，也不是「agent 没释放」。根因是**控制面把"此刻没观察到进程"
直接当成了"这个租约的活干完了"**，而这个瞬时判据在两处被当作充分证据使用，且两处都没有
第二次确认：

1. **人工「清空占用」按钮**（`release_empty_conflicted_lease`，`src/serverpilot/service.py:7366`）
   只要求一次新鲜、完整、无进程的采集，就释放一个 `HELD`/`ACTIVE` 的 workload 租约。
   一个分阶段的任务在两批 shard 之间会有几十秒到几分钟没有 CUDA 进程，这个空窗完全满足该判据。
   相比之下自动空闲回收（`_reconcile_idle_lease`，`service.py:7879`）是两阶段的：先告警、
   到 `idle_lease_reclaim_seconds` 才回收。人工按钮绕开了这层保护。
2. **一次不含某进程的完整采集，立刻把该进程行置为 `active=False`**
   （`service.py:5536-5551`），没有连续缺席次数要求。对百舸这类容器，`nvidia-smi` 报的是
   宿主 PID，容器 `ps` 看不到，采集器因此拿不到 username/start-time
   （库里 `username` 为空、`executable` 为 `[Not Found]`）；这种进程的身份**只在行仍 active 时可复用**
   （`service.py:5475-5508` 的注释即写明这一点）。于是采集抖一次，一张正在跑活的卡就变成"空卡"，
   而且原进程身份再也接不回来。

这两条叠加，再加上第三条放大器：

3. **keepalive 在 workload 租约被释放后没有任何冷却期就重新占卡**，且每个 worker 占
   `TARGET_MEMORY_FRACTION = 0.80`（`src/serverpilot/server_keepalive.py:51`）＝ 80 GB 卡上约 65456 MiB。
   本次是释放后 **8 秒** 就起了 8 个 worker。

## 观察到的时间线（UTC，2026-08-30，endpoint `server-10-40-1-76-p8908`）

来源：控制面 `audit_events` / `leases` / `lease_resources` / `process_observations`
（`~/Library/Application Support/ServerPilot/state/serverpilot.sqlite3`，只读查询）。

| 时刻 | 事件 |
| --- | --- |
| 15:30:38 | agent 取得 workload 租约 `e810104b…`，8 卡，task `205k latent fill 34705 rows` |
| 15:41:57–17:06:58 | 该任务第一批进程（约 8 GB/卡）被持续观察到，租约 ACTIVE 且 workload_bound |
| 17:07:11 / 17:08:12 | p8908 `telemetry.failed` / `telemetry.recovered` |
| 17:14:02–17:15:52 | 第二批进程出现又消失在观察里 |
| 17:16:05 / 17:17:07 | 再次 `telemetry.failed` / `telemetry.recovered`；**`lease.idle_since` 被打在 17:17:07.068437，与 recovered 事件同一微秒** |
| 17:19:52 | **human** 触发 `lease.empty_cleared`，release_reason = `empty fresh observation cleared endpoint ownership`，8 卡 workload 租约被释放 |
| 17:20:00 | `serverpilot-system` 在这 8 张卡上启动 keepalive，每卡 65456 MiB |
| 17:20:23 | agent 的计算进程（宿主 PID 804753–804763，约 8 GB/卡）出现在其中 7 张卡上 —— 与 65 GB 的占卡 worker 同卡共存 |
| 17:20–17:44 | 这 7 张卡在投影里是 `CONFLICT` → MCP `ownership_conflict`、`task=null`（持有者是 keepalive 租约，卡上却有外来进程）；index 0 无进程，显示 available |
| 17:44:14 | human 关闭 keepalive 策略；8 个 worker 在 17:44:11–17:49:10 之间陆续停止 |
| 17:48:31 | agent 进程行被置为 inactive（17:48:41 又一次 `telemetry.failed`），但进程实际仍在运行 |
| 17:48:55–17:49:10 | 7 次 `keepalive.gpu_stopped` 以 "target GPU observed empty" 收尾 —— **此时卡上其实还有 agent 的进程** |
| 17:49:53 / 17:50:03 | human 重新打开 keepalive；8 个 worker 重新占满 65456 MiB，压在仍在运行的任务上 → 用户看到的「全崩溃」 |
| 17:51:13 | agent 重新申请 8 卡，reclaim 停掉 8 个 worker，取得租约 `4ad5906…`（至今 `HELD`，未绑定 workload） |

## 判定

- 已观察：上表所有时刻、租约状态、释放原因、进程行的 active 翻转，均直接来自控制面数据库。
- 已观察：远端 keepalive 的 `stop` 只按 pidfd 定向信号自己的 worker（`server_keepalive.py:242`），
  **没有**误杀 agent 进程。崩溃来自显存被占满，不是被 kill。
- 推断：17:48:31 之后 agent 进程再未被观察到，是因为一次不完整/失败采集把行置为 inactive，
  而宿主 PID 进程的身份只能在 active 时复用。未直接抓到那一次采集的原始载荷。

## 流程层面的判断：冗杂在哪、设计漏了什么

### 三处冗杂

1. **「把卡还回来」有三个入口，判据互不相同，最松的那个恰好是人手动触发的。**
   `gpu_release`（所有者主动）、`_reconcile_idle_lease`（两阶段：告警窗口 + 回收窗口，
   且对声明了 duration 的租约豁免）、`release_empty_conflicted_lease`（一次采集、无窗口、无豁免）。
   同一件事三套判据。
2. **App 主动邀请人去点最松的那条。** `desktop/ServerPilot.swift:3654` 的 `reclaimableLeases`
   对任何 workload 租约、只要其 GPU 当前都在 `HELD`/`LEASED_IDLE`/`CONFLICT` 就弹出
   「仍被租约占用，但当前采集没有观察到进程；可确认后释放」。一个正常任务在两批 shard 之间
   就满足这个条件。一个恢复工具被做成了日常按钮。
3. **「结束占卡」是 endpoint 级开关，占卡机制却是 per-GPU 的，而且 agent 申请时本来就会自动让位**
   （`reclaim_keepalive_for_claim`，本次事件 17:51:13 的审计可见）。为腾一张卡而关掉整台机器的
   占卡是不必要的操作；它真正的语义是"我不想让 ServerPilot 在这台机器占卡"这个配置意图。
   粒度错配是本次「关掉再打开导致 8 个 worker 全灭再全起」的直接原因。

### 设计漏掉的那一件事

**控制面没有任何"持有者还活着"的表示。** 唯一的存活证据是"此刻卡上有没有 CUDA 进程"。

`_reconcile_idle_lease` 本来设计了一个豁免：声明了 duration 的租约不按观测空闲回收
（"that window is the user's stated intent"）。但 MCP `gpu_apply` 走
`POST /api/v1/routine/claims`，该路由固定 `persistent_lease=True`（`api.py:1373`），
于是 `lease.expires_at` 被显式清空（`service.py:6740-6741`）——**agent 租约按构造永远拿不到这个豁免**。
请求里的 `duration_seconds: 28800` 成为死数据。同时 MCP 面没有 renew/heartbeat
（只有 CLI 有 `lease renew`），agent 无法表达"还在用"。

结果：对本项目的主要使用者（MCP agent），全部下游判据
（`idle_since`、App callout、release-empty、keepalive 起卡资格）都建立在一个
该路径结构性缺失的信号上。

### 收敛方向（已落地）

- **一个判据**：进程是否还在，由 `process_observations.absent_since` 一处回答——完整采集里
  缺席则起表，被观测到则清零，采集不完整或失败则清零。断连不计入缺席，而是让缺席从头开始。
  读侧一律只问 `active`；`_process_seen_cutoff` 那类第二个时钟已删除。
- **一个所有者信号**：`_lease_liveness_age_seconds` = `now - max(进程最后目击, last_heartbeat_at)`；
  `gpu_status(lease_id=…)` 经 REST 刷新心跳，agent 零改动。
- **一个还卡入口**：人工释放对持有者仍活跃的 workload 租约拒绝，与自动回收共用同一个原语；
  服务端发布 `manual_release`，App 不再自己按 GPU 状态推断。
- **占卡起卡冷却期**：一张卡的 workload 租约释放后 `keepalive_start_cooldown_seconds` 内不起 worker。

## History

### 2026-08-31 — 首次定位

用户报告 p8908「状态异常」，随后说明是占卡程序与 agent 申请卡冲突、点「结束占卡」后全崩溃。
按上表复原时间线，定位到三条根因。

### 2026-08-31 — 流程层面的复查

用户追问流程是否有冗杂操作、设计是否漏了东西。复查后把结论从"三条 bug"改写为
"三处冗杂 + 一个缺失的表示"：控制面没有"持有者还活着"的信号，而唯一为此设计的豁免
（声明 duration 的租约不按观测空闲回收）被 routine claim 的 `persistent_lease=True`
按构造关掉了。修法仍未实施。

### 2026-08-31 — 修法落地：一个存活原语、一个进程判据、一个还卡入口、一个冷却期

四条根因全部改完，代码已落地（Python 全量测试通过；桌面端另见构建脚本）。

1. **控制面补上「持有者还活着」的表示。** `BrokerService._lease_liveness_age_seconds` 是唯一定义：
   `now - max(该租约各活跃 GPU 上任意 ProcessObservation.last_seen_at, lease.last_heartbeat_at)`，
   读进程行时不看 `active`。`gpu_status(lease_id=…)` 经新路由
   `POST /api/v1/routine/leases/{lease_id}/heartbeat` 刷新心跳，Agent 侧零改动；该路由不写审计、
   不 bump revision，对已释放或非本人租约返回 `recorded: false`。原「声明 duration 即豁免」被删除
   （routine claim 按构造清空 `expires_at`，那条豁免永远轮不到最需要它的租约）。
   `_normalize_legacy_workload_conflicts` 里控制面自己写 `last_heartbeat_at = now` 的那行被删——
   它每轮 reconcile 都会伪造一次存活证据。
2. **进程观察按年龄退休。** 新增 `collector.process_absence_grace_seconds`（默认 180s，由本次事故的
   62s 断连 + 39s 空列表实测推出），`_process_seen_cutoff` 收编此前三份误用 `stale_after_seconds`
   的内联副本。`ingest_observation` 不再逐条比对本次观测键，改为按年龄退休。
   **规格之外补的一步**：验证通过的 `finalize_keepalive_stop` 直接把该 worker 的进程行置为
   `active=False`——年龄判据存在是因为「一次采集没列出它」有歧义，而命令停卡 + 停卡后完整采集不再带它
   没有歧义。不补这一步，卡在让位后会被扣住最长 180 秒，`reclaim_keepalive_for_claim` 拿不到刚让出的卡。
3. **人工还卡与自动回收共用判据。** `_manual_release_verdict` 是唯一答案，供
   `release_empty_conflicted_lease` 放行与 snapshot 的 `manual_release` 投影使用；新错误码
   `lease_holder_recently_alive`，`blocked_reason` 与该路径会抛的码同名。keepalive 租约豁免的只是
   **存活窗口**（点的人就是持有者），仍要求每张卡当前没有进程；已失联端点整体豁免（机器已经不答，
   给不出任何证据）。桌面端删掉 `["HELD","LEASED_IDLE","CONFLICT"]` 的本地推断，只读
   `manualRelease.allowed`，字段缺失即不可释放。
4. **起卡冷却期。** `inventory.keepalive_start_cooldown_seconds`（默认 600s）读
   `lease_resources.released_at` 现算，只挡 `list_keepalive_transitions` 的 start；让位路径
   （`_eligible_gpus(include_reclaimable_keepalive=True)`）不受影响。

无 schema 变更，因此没有新 migration。

**已知代价（未解决，需要用户拍板）**：卡的周转慢了最多一个宽限期（默认 180 秒）——一张卡上的进程真正退出后，
仍会在最后一次目击之后的宽限期内保持 `BUSY_UNMANAGED`，release→立即 re-apply 会拿到
`no_capacity`。这是把 wall-clock 年龄当判据的固有代价（断连期间没有完整采集，断连时长必须全额计入）。
唯一不放弃正确性的替代是给 `process_observations` 加一列 `absent_since`（与 `LeaseResource.idle_since`
同形：完整采集里缺席则起表，被观测到或采集不完整则清零），时钟只在健康观测期间走，宽限期可缩到 60 秒；
那需要一个 migration，本轮按「不引入计数器列」的指示未做。

### 2026-08-31 — 评审收口：两个 fail-open 与一个门太窄

对上一条落地做对抗性评审后逐条收口，四条 finding 里三条改了代码，一条留给用户拍板。

1. **人工清理曾能清掉 worker 仍在跑的占卡租约（fail-open，已修）。** `_manual_release_verdict`
   把 keepalive 豁免放在了逐卡「当前无进程」检查**之前**，于是一个 65 GiB worker 仍被观察到的
   占卡租约会被直接释放，卡随后卡在 `BUSY_UNMANAGED`：没有租约指向那个 worker，
   `list_keepalive_transitions` 既不发 stop（查不到活的占卡租约）也不发 start（状态不是 AVAILABLE），
   `finalize_keepalive_stop` 404。HEAD 是拒绝的。已把豁免移到进程检查之后——豁免的只有存活窗口。
   证据：探针测试在修前得到 `released: True` + `BUSY_UNMANAGED`，修后得到 `conflict_process_present`。
2. **新增的跨字段校验会让既有 inventory 加载失败（已修）。** `process_absence_grace_seconds >=
   stale_after_seconds` 是对的不变量，但用「拒绝加载」来强制它，会让一份只调过
   `stale_after_seconds`（合法取值到 86400）的既有配置在升级后直接搞垮 daemon，连带 GUI/CLI/MCP，
   报错还指向一个用户从未写过的字段；而且 `stale_after_seconds > 3600` 时改都改不好。
   改为在唯一owner `_process_seen_cutoff` 处派生：实际宽限期 = `max(两者)`。
   证据：`CollectorConfig(interval_seconds=10, stale_after_seconds=600)` 修前 ValidationError，修后加载正常。
3. **存活门只护住最后一张卡（已修）。** 原实现的门是 `len(reclaimed) == len(resources)`，
   它想表达「整份租约都空」，但各卡的空闲计时会错开——某张卡遥测抖一次，或某个阶段只用了部分卡——
   于是一个正在心跳的持有者会丢掉 8 张里的 7 张，正是 p8908 的形状（卡离开租约、冷却期一过，
   占卡压到马上要用它们的任务上）。判据换成对整份租约问一次：**这份租约的任何一张卡上都没有当前进程**
   且持有者在回收窗口内被证明活着 → 一张都不收。有卡在跑时门不生效，闲卡照旧按卡收回，
   反囤积的原意保留。评审给出的场景（8 卡纯 CPU 阶段全空 + 心跳）实测**不复现**：那时
   `len(reclaimed) == len(resources)` 成立，旧门确实生效；真正的漏洞是计时错开的那一类。
   证据：探针测试 7 成熟 + 1 新鲜 + 心跳，修前剩 1 张卡，修后 8 张全留。
4. **释放后的卡最长一个宽限期不可再申请（未解决，留给用户）。** 见上一条 History 的「已知代价」。
   收口时确认它确实落在 Agent 的主路径上（release → 立刻 re-apply 得到 `no_capacity`），
   已写进 CHANGELOG 与 `docs/IMPLEMENTATION_STATUS_zh.md`，不再只作为实现备注。
   不在本轮修的理由：能同时保住正确性的两条路都要新增一条「这个问题的第二个答案」——
   要么在 ingest 里为「已释放租约的卡」重新引入逐键比对（正是本次事故修法删掉的结构），
   要么加 `absent_since` 列让时钟只在健康观测期间走（需要 migration）。后者是更诚实的解，
   但它是设计决定，不该在收口里顺手做掉。

同时校正了文档：`docs/IMPLEMENTATION_STATUS_zh.md` 里「逐卡回收不受存活门影响」与「keepalive 整体豁免」
两句原本描述的不是实际行为；CHANGELOG 两侧补上「声明时长的租约不再豁免观测空闲回收」
与「卡的周转最多慢一个宽限期」这两条用户可感知的变化。Python 全量 `651 passed`，`ruff check` 通过。

### 2026-08-31 — 用 absent_since 把周转代价收掉，并清掉评审剩下的 minor

上一轮留下的最大代价是「释放后卡最长 180 秒不可再申请」：判据用的是 wall-clock 年龄，
断连时长必须全额计入，所以窗口不得不取大。本轮按当时记下的方案 (a) 执行：

- 新增 `process_observations.absent_since`（migration `20260831_0034`），缺席时钟只在
  证据链不断时前进，`record_provider_failure` 与不完整采集都会清零它。窗口因此可以从
  180 秒缩到 60 秒，`process_absence_grace_seconds` 与 `stale_after_seconds` 的派生耦合
  （`max(...)`）随之删除——两者回答不同问题，不再需要排序。
- 删除 `_process_seen_cutoff`：读侧不再有第二个时钟。它此前还有一个未被指出的后果——
  一台只是停止应答的服务器，其进程会被读侧悄悄退休，那是 fail-open。
- 新增 `test_a_collection_outage_restarts_the_absence_instead_of_counting_toward_it` 与
  `test_a_card_its_owner_released_comes_back_once_the_absence_closes`，后者直接钉住
  release → re-apply 会收敛。
- `gpu_status` 的 `readOnlyHint` 改为 `False`（它现在写心跳），补上
  `destructiveHint=False` / `idempotentHint=True`。`test_mcp_protocol.py` 里断言旧声明的
  那条测试同步改写——上一轮的验证面没覆盖到它，是全量跑出来的。
- App 在服务器失联时不再渲染「有 0 张 GPU 仍被租约占用」：改为按租约自己的卡数计数，
  并给失联情形单独的文案与按钮标题。
- `desktop/build-macos-app.sh` 新增根目录编号副本清扫（`ServerPilot*.app` 排除正本），
  规则从「记住」变成「被执行」；已用一个人造副本验证会被清掉。
- 回退了 4 个文件上与本次无关的整文件 `ruff format` 重排（174 行纯换行噪声），
  这些文件的 diff 现在只剩语义改动。`service.py` 等语义占比高的文件保留格式化。

验证：`uv run pytest` 652 passed；`uv run ruff check .` 全绿；
`zsh desktop/build-macos-app.sh` + `zsh desktop/verify-macos-app.sh` PASS，根目录仍只有一份
`ServerPilot.app`。`desktop/UITests/FixtureModeUITests.swift` 的断言只做了静态核对
（fixture 的 `manual_release.allowed=false`、`clearAction` 为 nil、`DetailCallout` 在
`actionTitle`/`action` 皆 nil 时不渲染按钮），**本机无 Xcode.app，XCUITest 未执行**。
