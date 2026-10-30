---
status: active
superseded-by:
created: 2026-09-03
updated: 2026-09-03
---

# Plan: 观测层重做与项目收口

- Subject identity: macOS App 里服务器反复显示「连接失败」（常常六台同时）的根因修复，
  以及借这次修复把 ServerPilot 收口为一个可长期维护的 MCP + App 的选定方向。

## Current synthesis

### 根因：一个数字当两个预算用，六个探测又共享同一条期限

`ssh_connect_timeout_seconds = 8` 同时是 OpenSSH 的 `ConnectTimeout` 和整个观测
（连接 + 认证 + `nvidia-smi` + `ps` + 读流）的 wall-clock 上限（`adapters.py` 的
`RawSSHObservationAdapter.run_probe`）。实测成功探测均值 2.3–2.9 秒、最大 7.7–12.8 秒，
预算与正常耗时只差两三倍。过去 48 小时里 **100% 的采集失败**都是同一条
`TimeoutError: SSH observation timed out after 8 seconds`。

六台机器每 5 秒各新开一次完整 SSH 握手（全树没有 ControlMaster），经 VPN
（utun7，MTU 1400）到 `10.40.x.x`；`collect_once` 用一个 `asyncio.gather` 且
`stagger_seconds=0.0` 同时发出，于是它们共享同一个起点和同一条 8 秒线。任何整体抖动
——VPN 抖动、Mac DarkWake、事件循环被插件 fork 卡住——都让**所有**机器在同一轮越线。
数据库里「同一 10 秒窗口内 5–6 台一起失败」的事件每小时十几次。

失败三轮后 `collector_endpoints_due` 把端点冻结 60 秒不再探测
（`DEGRADED_ENDPOINT_PROBE_SECONDS`），这段时间 `monitor.status` 钉在 ERROR，
**机器早就恢复了 App 仍显示「连接失败」**。

三条放大器：一个坏 profile 能让整轮采集静默失败（`EndpointConfig` 的 profile 校验器会
fork 插件 `info`，`collector_endpoints()` 一个列表推导构造全部端点，任一行抛错被
collector loop 的 `except Exception: pass` 整轮吞掉，且插件探测缓存把失败也永久记住）；
App 把「我没轮询到 daemon」和「远端不可达」压成一个布尔，于是截图里整组显示
`0/34 空闲`、`未确认`；日志没有时间戳，且冻结后端与 uv 安装版争抢同一 label、端口和日志文件。

### 已验证的传输层方案（实测，非推断）

用生产环境的同一条 `RAW_SSH_COMBINED_QUERY`，加 `ControlMaster=auto` +
`ControlPersist` 复用连接：

| 主机 | 冷启动 | 热复用 |
| --- | --- | --- |
| p8908（12 卡） | 0.69s | 0.31–0.34s |
| p4580（16 卡） | 0.73s | 0.26–0.33s |
| p6103（4 卡） | 0.59s | 0.20–0.21s |

热复用 0.2–0.34 秒，对比今天守护进程测到的 2.3–2.9 秒均值与 7.8–12.8 秒尾部。
`| cat` 立即收到 EOF，证明 ControlPersist 的 master 不持有探测的 stdout 管道
（这是整个设计唯一的承重假设）；`IdentitiesOnly=yes` +
`PreferredAuthentications=publickey` 下 rc=0，不影响现有密钥。

### 选定方向

**传输**：daemon 持有的 SSH 复用连接，master 由第一次探测隐式打开——没有监督任务、
没有重试包装、没有「打开 master」这条代码路径。睡眠唤醒后
`ServerAliveInterval=5 × CountMax=3` 让死掉的 master 在 15 秒内自灭并删除 socket，
下一次探测冷启动一次。daemon 启动时清空自己的 control 目录（SIGKILL 后残留的 socket
会让 `ControlMaster=auto` 静默永久退化成非复用）。与 hanhai22 插件使用的人工 master
（`~/.ssh/cm/`）完全分离——那台机器需要交互式两步登录。

**预算**：一张 `SSHBudgets` 表，connect 10s 与 command 20s 分开，
`probe_deadline = 30s`；`stale_after_seconds(interval) = interval + probe_deadline`
成为唯一定义。不变式 `interval(5) < master_dead(15) < probe_deadline(30) < stale_after(35)`
由测试锁住：一次探测在通道被判死前已发出三轮，通道在探测超时前已重开，探测在主机被判
过期前已超时。

**节奏**：删除整轮 `gather` 和 60 秒冻结，改为每端点一个 asyncio task，
带确定性起始相位与 ±10% 抖动，失败只影响它自己。冻结存在的唯一理由是「让一轮不被最慢的
主机拖住」——删掉「一轮」这个概念，理由就消失了。

**观测面**：收口成「一个密封远端探测 + 插件」。`linux-host` 与 `linux-nvidia` 只差一段
shell，而合并探测本来就自带「没有 nvidia-smi → cpu_only」的判定；`server-script-v1`
需要在远端装 `serverpilot-collect`，线上零个端点在用。插件的 `observe` 成为唯一扩展点。

**错误**：13 个闭合错误码在采集侧分类一次，存进 `provider_states.last_error_code`。
Swift 端不再用子串匹配 Python 的自由文本英文 SSH 报错。rc=255 且 stdout 含
`__SERVERPILOT_GPU__` 判为 `remote_error`，解决「255 既是 ssh 的失败码又是合法远端退出码」
这一二义。

**删除**：预约、维护窗口、`api_tokens`、`endpoint_projects`（四张空表）；排队申请
（线上从无 QUEUED）与 `allocation_requests`（折进 leases）；端点 DISABLED/DRAINING 人为轴
（只有三个无 REST 路由、无 App 入口的方法会写）；只被测试调用的约 1,270 行；Windows GUI
（第三套客户端，没有 daemon 生命周期，也没有改派与 release-empty）。

**收口**：agent 契约 prose 从六份靠 59 个子串断言同步，改为单一来源生成；保留策略一张表；
配置 7 个来源收敛为一个 `Settings` 解析器；`service.py` 一万行一个类拆成有单一所有者的包。

## Protected behavior

- ServerPilot 仍然只协调 GPU，不启动、停止、迁移或抢占用户的 workload。
- 采集异常、非托管进程、冲突一律 fail closed；过期遥测不能被当成可用 GPU。
- 公开的 `monitor.status` 与 GPU 状态枚举值不改，除非删除的是整个概念
  （DISABLED/DRAINING 与 RESERVED/MAINTENANCE 属于后者，它们已无生产者）。
- 历史 lease 与 audit 不删；不制造兼容假数据；根目录只保留一个 `ServerPilot.app`。
- 不加 `force` 开关、兼容分支、`*_v2` 路径或重试包装来绕过上面任何一条。

## Stop and replan conditions

- 承重假设被推翻：ControlPersist 的 master 持有探测的 stdout 管道（已验证为假，
  若在别的 OpenSSH 版本上为真，`run_probe` 改为 `wait()` + 非 EOF 读，其余设计不变）。
- 需要在远端安装或常驻任何新进程才能让观测工作。
- 删除动作触及历史 lease/audit，或需要用户未批准的可见行为变更。
- 无法用只读数据库证据证明失败率、相关性、延迟三项都改善。

## Handoff

这是执行计划，不是完成声明。实现阶段结束后必须分别报告：复用连接的实测前后数字、
六台机器不再同窗口一起失败的证据、睡眠唤醒后的恢复时间、日志中四类噪声归零、
以及桌面 build/verify 证据。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-09-03 — 根因定位、传输层实测验证、方向落盘

用户报告「怎么总是连接失败（有时候是大规模的）」。初始假设是网络或阈值问题，读码与读
线上数据库后否定：真正的缺陷是一个超时数字承担了两种语义，以及六个探测共享一条期限。
`ControlMaster` 方案在三台真实主机上用生产查询实测通过（热复用 0.2–0.34 秒），
承重假设（master 不持有 stdout 管道）已用 `| cat` 立即返回证明。
用户同时决定删除 Windows GUI、收口观测 profile、删除四类死路径与人为轴，
并解除了[控制面计划](serverpilot-control-plane-optimization.md)对 AllocationRequest 表的保护。

### 2026-09-03 — 修法上线后被一次真实抖动当场验证

新构建运行 69 分钟，0 次采集失败、0 次同窗口多机失败，探测耗时均值 0.39 秒。
期间在 `2026-09-02 20:10:15` 出现了一次真实网络抖动：同一秒内三台主机的探测分别
耗时 9.45s、9.29s、8.19s。**这三次在旧的 8 秒预算下会同时超时，三台机器会同时显示
「连接失败」——正是用户报告的那个症状。** 新构建把它们照常收下，没有产生任何失败。

全窗口 394 次探测中有 3 次超过 8 秒，都落在这一秒里。这也解释了为什么用户看到的失败
「有时候是大规模的」：抖动本身是相关的，而旧设计让相关的抖动直接变成相关的失败。

同期完成的收口（已提交）：删除预约、维护窗口、`endpoint_projects`、`api_tokens`
四张无写入路径的表与 `RESERVED`/`MAINTENANCE` 两个无生产者的状态，删除只被测试调用的
十一个方法，删除 Windows 桌面 GUI（Windows 保留库、CLI 与 MCP）。删除过程中有两个
`@staticmethod` 装饰器被留在了幸存的方法上，静默解绑了 `self`，打断了 GPU 状态机与
分配器；已修复，并加了一条覆盖全部模块的形状检查。

### 2026-09-03 — 收口全部执行完毕并在真实库上落地

四个提交:`00a9a6e` 观测层重做、`eae50c4` 删四张无写入路径的表与 Windows GUI、
`a1d34af` 契约单源 + 保留策略 + Swift 拆分 + CI、`6ff4d89` 删停用轴/排队/七源配置/
远端采集脚本。全仓净 −4,100 行(9,990 增 / 14,090 删),`service.py` 从 10,007 行降到
约 8,500 行。

**迁移链已在真实库上执行**:head 20260903_0035 → 0038,1,796 条租约的 `task_ref`
全部回填,6 个活跃租约完好,`allocation_requests` 表与 `endpoints.enabled` /
`lifecycle_state` / `gpu_devices.enabled` 三列已消失,重跑幂等。重启后六台 SSH 端点
全部 ok(年龄 0.6–2.8 秒),hanhai22 仍是它本来就有的 `plugin_error`。

**被评审抓到的两个已提交缺陷**:Swift 拆分把一个 `@MainActor` 漏带到
`Confirmations.swift`,绑定到了原本没有该标注的 `ServiceProbeResult`(Swift 6 下是
编译错误;删掉后 typecheck 与 67 个 Swift 测试照常通过,证明它从来不是必需的);
`mcp policy --check` 用包自身位置推仓库根,安装版会把 tool venv 里两个从不该存在的
文件报成 missing,现已改为识别"这里没有检出"。

**`serverpilot-collect` 已删除**:观测 profile 收口后没有任何配置能调用它,而 README、
SECURITY.md 和升级清单仍在教用户往每台 GPU 服务器上装它并对齐版本。这三处文档已改为
描述真实要求——什么都不用装。`SERVER_SCRIPT_SCHEMA_VERSION` 保留,插件 observe 的
输出仍按这个 JSON 形状校验。
