# ServerPilot 当前实现与验证状态

更新时间：2026-09-03（Asia/Shanghai）

本文只记录当前事实、直接证据和仍未验证的边界。源码与构建完成不等于本机正在运行或已部署。当前包装版本为 `2.0.0`；工作树已实现一等服务器分组与任务声明 GPU 需求，去掉外部调度器提交、通用资源规划、工作负载预设和整套浏览器 `/ui`，并把插件接入的集群并入同一个集群模型：不再有平行的 `scheduler_servers` 顶层桶，每个分组投影同形的 `allocation` / `limits` / `largest_allocatable_block`。同时取消了 PyPI 发布工作流，`tag` 与 `__version__` 的一致性校验保留在 `release.yml`；Windows 桌面 App 已整体移除，Windows 上继续通过 CLI 和 MCP 支持。本文件不宣称已重启 daemon、已重装 MCP 入口，或已重新生成现场连接器描述。`1.9.1` 及更早各版本的现场验收证据已归档到 `docs/archive/`，与本次源码变更分开记录。

## 当前工作树：观测层重做（2026-09-03，已在本机运行并验证）

用户报告服务器反复显示「连接失败」、经常六台同时。根因是 `ssh_connect_timeout_seconds = 8`
同时充当 OpenSSH 的 `ConnectTimeout` 和整次观测的 wall-clock 上限，而实测一次成功观测均值
2.3–2.9 秒、尾部 7.8–12.8 秒；六个探测又在同一个 `asyncio.gather` 里以 `stagger_seconds=0.0`
同时发出，共享同一起点与同一条线，于是任何整体抖动让所有主机在同一轮越线。失败三轮后
`collector_endpoints_due` 还会把端点冻结 60 秒不再探测，机器恢复了界面仍显红。

已执行的改动：

- **传输**：`adapters.observation_ssh_argv` 是唯一的选项列表，加 `ControlMaster=auto` /
  `ControlPersist=300` / `ServerAliveInterval=5 × CountMax=3`，master 由第一次探测隐式打开，
  没有监督任务也没有重试包装；daemon 启动清空自己的 control 目录。control 路径必须加引号
  ——真实目录含 "Application Support" 的空格，不加引号 ssh 会把整条选项判为
  "extra arguments at end of line"，全机所有探测一起失败。
- **预算**：`config.SSHBudgets` 把连接（10s）与远端命令（20s）分开，`probe_deadline = 30s`；
  `config.stale_after_seconds(interval) = interval + 30` 成为唯一定义（原来是 `interval * 3`，
  比一次合法的慢探测还短）。不变式 `interval < master_dead(15) < probe_deadline(30) <
  stale_after(35)` 由 `test_budget_ordering` 锁住。
- **节奏**：删除整轮 `gather`、`collect_once`、`collector_endpoints_due` 与
  `DEGRADED_ENDPOINT_PROBE_SECONDS`；改为每端点一个 asyncio task，确定性起始相位加 ±10% 抖动。
  keepalive reconcile 仍是独立 task 并由 shutdown 等待，不能内联——否则关机取消探测循环会取消
  正在进行的占卡启动，留下一个占着卡却没有账本记录的 worker。
- **观测面**：`linux-nvidia` / `linux-host` / `server-script-v1` 收口为 `linux`；删除
  `_OBSERVATION_QUERIES` 与 `RAW_SSH_HOST_ONLY_QUERY`，`observe_endpoint` 只剩两臂
  （内置探测 / 插件 `observe`）。
- **错误**：`collector.classify_failure` 是唯一分类点，13 个闭合错误码写进
  `provider_states.last_error_code`；rc=255 且 stdout 含 `__SERVERPILOT_GPU__` 判 `remote_error`
  （255 既是 ssh 的失败码又是合法远端退出码）。桌面端改读该码，删掉子串匹配级联。
- **可达性**：新增 `serverpilot.freshness`，`Freshness` / `freshness_of` 成为唯一所有者；
  `endpoint_reachability`、`_endpoint_is_unreachable`、snapshot 的 monitor 推导都读它。
  快照 `monitor` 新增 `error_code`、`last_success_age_seconds`、`stale_after_seconds`。
- **生命周期与日志**：`_daemon_executable` 只解析 uv tool 那一份（删除 PATH/sys.executable
  候选与 `SERVERPILOT_DAEMON_EXECUTABLE`）；install 会删除旧 `local.gpu-broker.daemon.plist`
  而不只是 bootout；`Database.migrate()` 永远用包内 migrations（不再优先 cwd 祖先的源码树）；
  新增 `logging_setup.py`，日志带 UTC 时间、PID、endpoint 名并轮转。
- 迁移 `20260903_0035`：改写 `endpoints.observation_profile` 为 `linux`，新增
  `provider_states.last_error_code`（不回填猜测的码）。

**本机实测（同一台机器，改前 / 改后）**：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 单次观测耗时（`collected_at - observed_at`） | 均值 2.27–2.90s，最大 7.8–12.8s | 均值 0.15–0.37s，最大 0.83s |
| 采集失败 | 过去 48h 内 100% 是 `TimeoutError ... after 8 seconds`，峰值 121 次/小时 | 新构建运行期间 0 次 |
| 同一 10 秒窗口 ≥3 台同时失败 | 约 15 次/小时 | 0 次 |
| 日志噪声（`No such revision` / `Errno 48` / instance-id 不匹配 / `Unknown child process`） | 462 / 462 / 373 / 12 | 0 / 0 / 0 / 0 |
| 复用连接 | 无（每 5 秒每台一次完整握手） | 6 个 master socket，6 个 `ssh [mux]` 进程 |

`hanhai22-p22` 仍是 `plugin_error`，这是既有且真实的情况：该集群不能用密钥、每次登录要一个
一次性验证码，插件按设计复用人工打开的 ControlMaster，需要人工先 `ssh -fN hanhai22`。

## 当前功能

1. **信息采集**：固定只读探针采集服务器 CPU、内存、GPU、进程和历史趋势；APP 刷新只读取这份状态。共享集群通过本机插件的 `observe` / `apply` / `release` 接入，随包参考插件为 `slurm-immediate`，没有独立的调度器提交面。GPU 与主机资源都投影最近 10 分钟均值，endpoint 快照在 `host_telemetry.recent_average` 提供标准化 CPU 负载和内存占用率，详情页的逐卡显存环图仍以当前观测绘制。
2. **人类监控与纠错**：macOS 桌面 App 展示服务器分组、任务与 GPU；Windows 上没有桌面 App，CLI 和 MCP 仍可用。没有浏览器界面，也不再提供 `/ui/*` 页面。分组携带共享工作目录、环境说明和数据/权重说明；成员可继承或覆盖工作目录。环境说明只供阅读，不会被执行或注入。App 刷新只读取本机 daemon 已提交快照。保持卡数不变并选择新 GPU，以及释放已确认空闲占用，只在 macOS App 与 REST operator 路径上提供。目标 GPU 正在占卡时，macOS / REST 改派先按卡停止并刷新确认，再更新分配，随后提示对应 Agent 按返回的 `CUDA_VISIBLE_DEVICES` 重启任务。
3. **Agent 操作**：源码日常 MCP 正好五个工具：`gpu_status`、`gpu_apply`、`gpu_release`、`gpu_add_server`、`gpu_update_server`。申请签名为 `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)`：`gpu_count` 来自启动脚本或配置中的任务并行度，安全默认 1，不从空闲容量推断；已分组裸机先选定组，再在组内 best-fit 一台主机，一份租约始终只落在一台机器上；插件接入的分组同样可以直接以 `server_group_id` 申请，由 `apply_plugin_for_claim` 在该组恰好一个声明了 `apply` 的成员上落地，`server_id` 仍用于未分组主机。`gpu_add_server` 登记主机，`gpu_update_server` 更新安全元数据；删除不是 MCP 工具。`gpu_status` 按 组 → 服务器 → SKU 投影可申请容量（`name`、`vram_mib`、`total_count`、`available_count`），不是逐张空闲卡菜单，不含遥测与 `keepalive`；连接与远端工作目录随该服务器投影一次。插件接入的集群不再有平行的 `scheduler_servers` 顶层桶：它的登录节点以同一个 `{server_id, workspace_path, workspace, ssh, gpus[]}` 形状挂在所属分组内，未知的 `total_count` 省略而不是填 `null`，且不再同时出现在 `cpu_only_servers`。没有逐卡 GPU 行的分组也会被投影，因此手工设置的组名与共享工作目录不会丢失。每个分组另投影 `allocation`（`direct` / `delegated`）、`limits`（`max_gpus_per_lease`、`max_lease_seconds`、`lease_ends`、`cpu_cores_per_gpu`、`memory_mib_per_gpu`、`apply_max_seconds`、`queues`）和 `largest_allocatable_block`。`largest_allocatable_block` 的语义固定为**一次申请能拿到的最大卡数**：委托型取插件上报的单节点最大空闲块，缺失时只在同时已知单次上限时回退为 `min(free_gpu_count, max_gpus_per_lease)`，两者都未知时为 `null`，绝不用跨节点池子总数冒充；直连型取组内单台主机的最大可申请卡数。默认响应附带紧凑 `busy_gpus`（`server_id`、`gpu_id`、`index`、`status`、人类可读 `task`），占用情况不需要第二次调用，`server_id` 可将响应收窄到一台服务器。申请成功后的 `gpus[]` 仍按卡返回，每行只保留 `server_id` 外键和该卡自身字段。遥测只随租约投影：`gpu_status(lease_id=…)` 返回 `leased_gpus` 逐卡 `recent_average` 与 `current`，以及 `lease` 汇总（`telemetry_gpu_count`、`min_memory_free_mib`、多卡时的 `gpu_utilization_spread_pct` 与 `slowest_gpu`）。近 10 分钟均值的时间窗描述由 `lease.telemetry_window` 承担，逐卡窗口与其他卡不一致时才落到该卡的 `window_override`，部分失败的采集不会被投影成一个共享窗口；`lease_id` 已释放、被回收或不在本次收窄范围内时返回 `no_leased_gpus`。`gpu_release` 回显被结算的 `lease_id` 与 `state`。连接、工作目录、代码位置和设备选择仍分开投影：`ssh` 只负责连接；`workspace {path, kind=working_directory, use_as_cwd=true, code_location=not_provided}` 明确远端 cwd 且不暗示代码路径；`workspace_path` 继续保留。GPU UUID 只作物理身份，`gpu_index` 保留采集时的 `nvidia-smi index`；collector schema v2 另按 PCI bus 顺序生成 `cuda_ordinal`，lease 返回 `cuda_device_order=PCI_BUS_ID`、顶层完整 `cuda_visible_devices` ordinal 集合和逐卡 `gpu_cuda_visible_devices` ordinal。数据库中没有当前 `cuda_ordinal` 的 GPU 不参与分配。启动前做最小 CUDA gate，失败立即释放并在当前任务内避开同一环境。租约持续到显式释放、macOS App / REST 人工改派或释放空闲占用，或被两阶段空闲回收收回；容量不足返回 `no_capacity`，未选定合适分组时返回 `group_selection_required`，两者都不排队且同一 turn 不反复轮询。空闲回收只依据观测且**按卡粒度**：每张卡各自计时，某张卡在**新鲜**采集下持续没有计算进程时单独被收回，同一租约中仍在工作的卡不受影响；全部卡都被收回时租约转 `EXPIRED_EMPTY`——但**只要这份租约在任何一张卡上都没有当前进程，收回任何一张卡都还要求持有者本身已经沉默**。`_lease_liveness_age_seconds` 是控制面唯一的存活判据：取该租约各活跃 GPU 上任意进程观察的最后 `last_seen_at`（不看该行的 `active` 标志）与 `lease.last_heartbeat_at` 的较晚者；租约在任何一张卡上都没有当前进程、且在 `idle_lease_reclaim_seconds` 内还有任一存活证据时，只告警不收回——因为「此刻每张卡都没有进程」正是分阶段任务两批之间的样子。这道门覆盖**每一张**卡，不只是最后一张：一张卡的遥测抖一次，或某个阶段只用了这份租约的一部分卡，各卡的空闲计时就会错开，只在「全部卡同时到期」时才生效的门恰好挡不住这些情形。心跳不需要 Agent 新增调用：MCP `gpu_status(lease_id=…)` 经 `POST /api/v1/routine/leases/{lease_id}/heartbeat` 刷新 `last_heartbeat_at`，该路由不写审计、不 bump revision，对已释放或不属于调用者的租约返回 `recorded: false` 而非报错。原先「声明了 duration 的租约豁免观测空闲回收」已被这条判据取代：routine claim 固定清空 `expires_at`，那条豁免按构造永远轮不到最需要它的租约。**这对 CLI / App 路径是可见的行为变化**——声明了时长的租约此前完全不参与观测空闲回收，现在与其他租约同一套判据，卡持续空闲且持有者连续沉默超过回收窗口时同样会被收回；声明的时长仍然决定租约何时到期。租约里**还有卡在跑进程**时这道门不生效：那时闲着的卡就是这份租约用不上的卡，仍按卡收回，一个仍在跑的进程护不住同租约其余空闲的卡。当租约的全部 GPU 持续空闲，超过 `idle_lease_alert_seconds`（默认 600s）记 `idle_lease` 警告告警，超过 `idle_lease_reclaim_seconds`（默认 3600s）且持有者同样沉默时以 `EXPIRED_EMPTY` 释放并写审计 `lease.idle_reclaimed`。出现进程或采集变 stale 都会清零 `lease_resources.idle_since`，因此计时始终是一段完整观测到的空闲窗口，采集中断不会累积成回收。一次 MCP 调用具有内部重放键并在本地传输失败时只重试一次；新的同参数调用仍能取得第二个 lease，多个 lease 由申请者逐个确认释放。
4. **空闲 GPU 占卡**：明确分开持久意图与当前进程状态。endpoint 的 `desired` 只有 `ON / OFF`，只随用户开关改变；逐卡 `actual` 只有 `ON / OFF / ERROR`，由 helper 操作与新鲜采集更新。内部逐卡归属不再使用 TTL，并持久保存唯一的 collector PID、boot ID 和进程启动时间；远端 helper 本地状态保存其 PID namespace 内的 PID、Linux boot ID、`/proc` 启动时钟和固定 worker marker，停止前使用 pidfd 重新校验并发信号，PID 重用或 marker 不匹配时绝不 kill。恢复时 helper 先确认该 namespace worker 仍是自己，再以固定 NVIDIA 查询证明目标 UUID 恰有一个 driver-visible PID；ServerPilot 只在该 driver PID 与 boot ID 同新鲜 collector 的唯一进程一致时重新登记 worker，且以 collector 的启动时间写入持久状态。helper namespace 的 ticks 不被伪装为 host PID 的启动身份；当前 collector schema v2 尚未提供可端到端比较的 host ticks。PID-only 或旧 marker 状态直接 fail closed，不提供旧版收养路径。额外或替代业务进程为 `ERROR/CONFLICT` 并 fail closed。helper 状态文件和数据库备份都通过同目录临时文件、fsync 与原子替换发布。

一等分组持久保存共享 `workspace_path`、环境说明和数据/权重说明。未分组 endpoint 仍必须自带绝对远端路径；已分组 endpoint 可继承组默认工作目录，或按 endpoint 覆盖。新增或更新服务器时 REST、五个 MCP 工具中的登记/更新工具和原生 APP 都要求最终能解析出绝对远端路径；`gpu_status` / `gpu_apply` 在不改变持久化模型的前提下将生效路径投影为结构化 `workspace`，明确这是远端 cwd、不是代码路径。历史记录迁移保留且未知路径保持空值，不猜测项目子目录。该字段只是元数据和操作指引，不创建/删除远端目录、不授权启动 workload；环境说明只供阅读，不会被执行或注入。密封占卡 helper 固定布局为 `${workspace_path}/serverpilot-keepalive`（此处为生效工作目录），adapter 先只读执行 `./serverpilot-keepalive --protocol-info`，确认 v3 和 `worker_attestation` 能力后再执行固定启停命令。身份恢复时只读执行固定 `--inspect --schema-version 3`，不依赖远端 `PATH`，也不允许 caller 传入 PID、路径、命令或参数；预检失败不发送 mutation，旧 v2 wire/state 直接 fail closed。

占卡 GPU 对 APP、REST 和 MCP 仍计为可用；`desired=ON, actual=OFF` 时 GPU 空闲则仍可申请，同时下一轮按策略重新启动 helper。真正分配前，Agent 申请和 macOS App / REST 人工改派都复用同一个「选中 GPU → 逐卡停止 helper → 定向采集 → 结束占卡记录 → 普通申请或改派」实现。

loopback 控制面不使用登录 token：没有 token model、登录页面、签发接口或撤销接口。服务器永久删除只在 REST `DELETE /api/v1/endpoints/{id}` 与桌面 App；它故意不是 MCP 工具。旧 daemon 缺少 `endpoint_delete` 时会被替换为内置后端。删除会写入墓碑，YAML 清单在重启或 `sync_inventory` 时不会把已删服务器复活；用户显式重新添加同一 endpoint 时清除墓碑。有进行中租约时 fail closed，且不停止远端进程。服务器没有停用/排空这一轴：`endpoints.lifecycle_state`、`endpoints.enabled` 与 `gpu_devices.enabled` 已随写它们的方法一并删除，`monitor.status` 与 `gpu.state` 不再有 `DISABLED` / `DRAINING`。一次申请只留一份身份：`allocation_requests` 已删除，`task_ref`、`purpose`、`constraints_json`、`duration_seconds` 四个字段随 `20260903_0037` 迁到 `leases` 上并回填，`leases.request_id` 一并去掉；从来没有一行请求进过 `QUEUED`，`GET/POST /api/v1/requests`、`POST /api/v1/requests/{id}/cancel`、`request` CLI 命令组和 `GET /metrics` 因此一并删除，快照与控制面状态不再有 `requests`，`no_capacity` 改为在 `details.excluded` 里按原因给出计数。`request.created` 等历史审计事件保留。当前源码另有 `20260828_0033`，去掉调度器、规划与预设表，以及 `allocation_requests.profile_id`；本文件不把它记为已在运行库上执行。升级迁移不删除已有 token 表、退役服务器、占卡租约或 lease resource。

占卡链路没有校验摘要、自动重试、退避器、第二套定时器、自动抢占或整机占卡状态机；唯一的身份证明是固定 helper 对自身 v3 state 的只读检查，并且必须与已有 collector 观测一致，不能用于收养任意进程。

项目明确要求的资源正确性边界仍保留：过期采集不能被当成可用 GPU，Agent 只能使用实际返回的 lease 资源。这两项来自当前项目合同，不新增状态机。

Agent 合同现已明确限定作用域：ServerPilot 只协调 GPU，禁止绕过的对象是 GPU 发现、选卡、申请和释放；已获得当前授权 endpoint 的 Git 同步、文件维护与只读环境检查不需要 GPU lease。`workspace_path` 仍只是元数据，不提供远端 shell 或额外授权。`Transport closed` 与 `no_capacity` 分开处理，前者最多重试一次；同一任务内的 CUDA 初始化失败不会立即重试同一 server。

## 已完成验证

### 本次工作树（实机验证，2026-08-28 夜）

针对「申请不到卡 / 流程卡顿 / 申请很慢」的定位与修复，证据取自本机运行的控制面（6 endpoint / 26 GPU / 2 分组）。

语义变化：

- **占卡状态以采集新鲜度为第一条规则。** 看不见一张卡就无法确认它上面的占卡进程还在，`actual` 因此报 `ERROR` 而不是回放上一次记录值。状态页、endpoint 汇总和分配器现在共用 `_keepalive_gpu_status` 这一处实现（此前 snapshot 内另有一份手写拷贝 `derive_keepalive`，且已经对「有进程但无占卡记录」给出不同答案）。snapshot 因此少了一次 `KeepaliveCurrent` 预取，`session.get` 命中 identity map；只读副本实测 `snapshot()` p50 从 8.3ms 降到 7.5ms。
- **过期的占卡不再是可回收容量。** `_eligible_gpus` 此前算出 GPU state 却在 keepalive 分支丢弃它，于是失联主机上的卡进入 reclaim 候选；`_select_resources` 的 best-fit 在同为 8 卡时按 endpoint id 排序，恰好优先选中那台失联的机器，申请在 SSH 超时上耗尽预算后以 503 结束，且因为是 `raise` 而非 `return None`，插件兜底根本不执行。
- **一个 GPU UUID 只归属一个 endpoint。** 容器换转发端口重新登记会为同一张物理卡留下第二份记录。`ingest_observation` 现在按「黏性在位者 + 过期移交」收敛归属，失主的占卡租约在同一轮释放；分配器另有 `uuid_leased_elsewhere` 这条 fail-closed 过滤。不合并 endpoint，只裁定物理卡归属。
- **警告不再比它描述的对象活得久。** `idle_lease` 并入 lease 作用域告警类型，孤儿 endpoint / GPU 告警在 `_reconcile_leases` 每轮清理，删除服务器时即时关闭。
- **采集节奏与退避。** 循环改为固定间隙（此前 `max(0.25, interval - elapsed)` 在慢周期下退化为背靠背连续采集）；持续失败的 endpoint 退到 `DEGRADED_ENDPOINT_PROBE_SECONDS`（60s）重试，第一次成功即回到每轮。退避只影响探测节奏，不改变准入——它已经通过 stale telemetry fail closed；省下的是每轮一次失败写入与 revision 自增，那会让每个读者都以为有变化。间隔取得接近采集周期，好让刚被人修好的主机自己回来，不需要另开一个即时重探入口。扇出常数删除：一轮的时长是最慢的一台，不是总和。
- **阻塞调用离开事件循环。** 插件 `info` 的结论按文件内容缓存（实测单次调用 37ms → 0.11ms）；`observe` / `apply` / `release` 经 `asyncio.to_thread`；协程内的领域调用经 `BrokerService.in_domain`（单 worker，即 `/health/ready` 已公布的 `single_writer`）。`tests/test_package_layout.py` 有静态护栏禁止协程里裸调 `service.` 或插件动词。
- **申请预算由服务端决定。** 插件声明的 `apply_max_seconds` 成为强制超时，直连分组投影推导出的预算，客户端不再按 `gpu_count` 估算；读超时不再重放申请（请求已送达，重放只会把等待翻倍），只有确证未送达的连接失败才重试一次。`/api/v1/routine/claims` 与 `/api/v1/claims` 一样强制 idempotency key。

实机证据（同一台机器，改前 / 改后）：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 事件循环延迟探针（不存在路由，45s 采样） | p99 458ms，max 1922ms，>1s 停顿 5 次 | p99 5.0ms，max 56ms，>1s 停顿 0 次 |
| `GET /api/v1/state` | 1.0–1.3s | 0.04s |
| `GET /api/v1/gpus` | 1.70s | 0.05s |
| `GET /api/v1/endpoints` | 1.52s | 0.05s |
| 活跃告警 | 23 条（21 条指向已不存在的资源） | 2 条，均为真实失联 endpoint |
| 失联主机 `server-10-40-1-28-p4725` 的占卡投影 | `actual=ON`，8 张卡进入 reclaim 候选 | `actual=ERROR`，`available_count=0`，在 `busy_gpus` 中标为 `unreachable` |
| `gpu_apply(server_group_id="baidu-baige", gpu_count=8)` | 挑中失联主机后 503 | 约 17 秒成功，落在仍在应答的 `server-10-40-0-199-p4580`，随后正常释放 |

Python 全量测试 `616 passed`，Ruff 通过。未改数据库 schema，无新迁移：所有判定都从既有列（`GPUDevice.present/last_seen_at`、`ProviderState.last_success_at/last_attempt_at`、`TelemetryCurrent.observed_at`）派生。

第二轮（契约与归因，同夜）：

- **两个服务器管理工具恢复可调用。** `_require_endpoint_admin_contract` 删除：它校验 `approval_ref` 非空后即丢弃，服务端从不消费。`gpu_add_server` 必填参数从 6 个降到 `[project_id, host, workspace_path]`，`gpu_update_server` 从 4 个降到 `[server_id]`，与 instructions 首次一致（`approval_ref` / `idempotency_key` 正是 `tests/test_agent_policy.py` 断言不得出现在 instructions 里的概念）。replay key 改由工具自己生成。保留必填 `project_id`：owner 为空会让 `has_unowned_endpoints` 在下次 `initialize` 触发 `_upsert_inventory`，用 YAML 回滚用户在 App 上改过的字段。重复登记由 `endpoint_exists` / `endpoint_address_exists` 两条 409 挡住——那从来不是 idempotency key 的职责。
- **两个 broker client 收敛到 `parse_broker_response`。** 此前 `error.details` 只有异步侧携带，同步侧静默丢弃，于是 `group_selection_required` 经 MCP 到达时带着候选组、经 CLI 到达时被剥空。
- **租约遥测按持有期夹紧。** snapshot 为 workload 租约的卡新增 `telemetry.lease_recent_average`，样本下界取 `lease.issued_at`；MCP 的 `leased_gpus[].recent_average` 与 `lease.telemetry_window` 改读它。未夹紧的 `recent_average` 不动——它描述这张卡本身，是 App 消费的另一个事实。样本不足为 `null`，`current` 仍可读。
- **lease 投影新增只读 `last_process_observed_at`**：这些卡上最后一次运行计算进程的时间，**下界取 `process_started_at >= lease.issued_at`**——只有在你拿到卡之后启动的进程才可能是你的。用 `last_seen_at` 做下界挡不住一个跨越分配时刻仍在被观测的旧进程。它不参与任何放行判断。

**服务端门槛：先前否决的是判据，不是门槛本身。** release-empty 最初提出的门槛建立在 `idle_since` 上，被否决的理由成立且仍然成立：`_reconcile_idle_lease` 只在 `kind != "keepalive"` 且 state ∈ {HELD, ACTIVE} 时更新它，故 keepalive 与 CONFLICT 租约的该值恒为 `None` 或冻结，任何基于它的阈值都会让这条人工恢复路径在自己的目标场景上永久 409。p8908 事故（`docs/teamwork/records/keepalive-workload-collision-p8908.md`）随后给出了相反方向的证据：没有门槛时，一个健康的 8 卡租约在两批 shard 之间被人点掉，8 秒后被占卡程序占满显存。现在的门槛换的是判据而不是阈值——用「租约存活年龄」（该租约各 GPU 上最后一次进程观察与 `lease.last_heartbeat_at` 二者取晚，再与 `now` 相减），它对 CONFLICT 租约同样有定义，且 keepalive 租约与已失联端点两种情形豁免这道**存活窗口**，因此原否决意见指出的永久 409 不会发生。豁免的只有存活窗口：keepalive 租约仍然要求它的每张卡当前没有进程，否则以 `conflict_process_present` 拒绝——清掉一个 worker 仍在跑的占卡租约，会把占了 80% 显存的 worker 留在卡上而没有任何租约指向它，停卡计划也再找不到它。已失联端点是唯一连这层证明也给不出的情形（机器已经不答，无法证明卡是空的），沿用原有的整体豁免。判据只有一份：`_manual_release_verdict` 同时供 release-empty 放行与 snapshot 的 `manual_release` 投影使用，`blocked_reason` 与该路径会抛的错误码同名，因此 App 里灰掉的按钮与点下去会得到的 409 是同一个词。App 不再自己按 GPU 状态推断可释放性：`LeaseRecord.manualRelease` 缺字段即视为不可释放。

**桌面端**：`confirmEmptyLeaseCleanup` 的确认框展示 `last_process_observed_at`（相对时间，或「从未观测到」）。刻意不设「多久以内算危险」的阈值——那等于在客户端重建一套放行标准，而这条路径存在的意义正是救那些没有标准可依的租约。点击层数不变，紧急恢复不因此变难。`desktop/Fixtures/maintenance.json`（8 秒前，复刻事故形态）与 `conflict.json`（两小时前）覆盖另外两个文案分支。`zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 均通过。（当时 Swift 单元测试因缺 Xcode 的 `XCTest` 无法在本机运行；见下文第三轮，已迁到 swift-testing 并在本机跑通。）

**已知缺陷（未修，本轮后单独做）**：`reassign_lease_gpus` 中途换卡不更新 `lease.issued_at`，`LeaseResource` 也没有"这张卡何时加入本租约"的列，故换卡后 `lease_recent_average` 的夹紧退化为无效。修它需要新增 `LeaseResource.assigned_at` 列与 migration，届时 `last_process_observed_at` 的下界一并改为 `max(issued_at, assigned_at)`。只在人工换卡时发生。

第二轮实机复验：`gpu_apply` 3.6s；申请后 `recent_average: null`、`current` 可读、`telemetry_window` 不发布假窗口；`open_leases` 含自己；两个管理工具 required 正确；`group_selection_required` 携带候选组。Python 全量测试 `616 passed`。


第三轮（失联主机的死锁，同夜）：

- **`_endpoint_is_unreachable`（`service.py`）是新增的唯一判据。** 取 `ProviderState(provider="raw-ssh", endpoint_id)`：无记录或 `last_error is None` 即为可达；`last_success_at` 为空视为失联；否则 `now - last_success_at > collector.stale_after_seconds`。判据客观、只读既有列，无新迁移。
- **`release_empty_conflicted_lease` 补全了它缺的另一半语义。** 它原本只实现「证明卡是空的」，撞上「已经无法证明任何事」就永久 503 `conflict_observation_failed`：采集失败 → 拿不到新鲜度证明 → 租约释放不掉 → `delete_endpoint` 撞 409 `endpoint_has_active_leases` → 服务器删不掉 → 卡永久锁死在账本里。每条恢复路径都向那台已经不存在的机器要证据。失联时跳过新鲜度证明与逐卡进程检查（`for gpu in [] if unreachable else gpus`），`release_reason` 记为 `server unreachable; operator settled the ledger`。
- **`delete_endpoint` 同理：失联时 409 让位，并调 `_settle_leases_for_deleted_endpoint` 结算残留租约**（`release_reason = "server deleted while unreachable"`），审计 summary 带 `unreachable` 与 `settled_lease_ids`，不留指向已删 endpoint 的悬空租约。该查询覆盖全部情形：`LeaseEndpointCommitment` 只与同一批 GPU 的 `LeaseResource` 同时写入，不存在只有 commitment 没有卡行的活跃租约。

**没有加 override 参数或 `force` 开关。** 这正是第二轮评审否掉的「加门槛再加旁路」：判据由客观事实（采集确实失败且已过期）决定，动作本来就只能由人从 App/REST 发起。三档语义因此是完整的一张表，不是两条路径：采集成功且卡上有进程 → 拒绝（fail closed，不变）；采集成功且卡上无进程 → 释放（不变）；采集失败且已失联 → 允许人结算账本。

**fail closed 在这里保护不了任何东西。** ServerPilot 从不停用户 workload，更停不了一个够不着的；一条过期的账本记录也拦不住别人使用那张卡。它唯一的效果是把卡锁死在自己账本里。

测试钉住两个方向（`tests/test_service.py`）：主机**仍在应答**时照旧 `conflict_observation_stale` / `endpoint_has_active_leases`（一次网络抖动清不掉活主机上的租约），失联后才放行并写入对应 `release_reason`。

**桌面单元测试改用 swift-testing，本机不再需要 Xcode。** 62 个用例从 XCTest 迁到 `@Test`/`#expect`/`#require`，`desktop/build-macos-app.sh test` 从 `xcode-select -p` 推导 `-F .../Library/Developer/Frameworks` 与 rpath，CommandLineTools 环境下即可运行（装了 Xcode 的机器同样适用）。这条取代了上文「本机无法运行 Swift 单元测试」的说明——`testLeaseRecordCarriesWhenItLastRanSomething` 已在本机跑通。迁移中暴露一个真实问题：XCTest 串行执行同一个 class，swift-testing 默认并行，共享 `StateRouteURLProtocol` 静态状态的用例会互相踩；两个 suite 因此显式标注 `@Suite(.serialized)`，而不是依赖隐式执行顺序。

第三轮实机复验（现网控制面）：p4725 的 8 笔遗留占卡租约全部结算、endpoint 删除，账本从 26 张卡收敛到真实存在的 18 张，指向 p4725 的残留 GPU 行为 0，孤儿告警随之消失。Python 全量测试 `618 passed`，Ruff 通过；`zsh desktop/build-macos-app.sh test` 报告 `62 tests in 2 suites passed`。未改数据库 schema，无新迁移。


第四轮（登记一台服务器为什么造不出能用的机器）：

起因是一条现网记录：另一个 Agent 线程登记的 `server-10-40-1-28-p7770` 在 App 里是红的，`observation_profile=server-script-v1`、`server_group_id=null`、`keepalive_policy=disabled`，`ProviderState.last_success_at` 为 `null`（`Host key verification failed`，`known_hosts` 里没有 `[10.40.1.28]:7770`）。对照 `EndpointCreate` 与 MCP 工具签名后确认：**四个字段里有三个不是调用方选的。**

- **`server_group_id` 在 MCP 上根本不存在。** `EndpointCreate` 与 `EndpointUpdate` 一直带这个字段，`gpu_add_server` / `gpu_update_server` 两个工具都不暴露它。分组决定这台机器能否被 `gpu_apply(server_group_id=...)` 选中，所以经 MCP 登记的服务器**必然**是不可分配的；补齐前 Agent 无论怎么调都造不出一台能用的机器。两个工具现在都接受它。
- **`observation_profile` 的默认值本身是错的，而且有两个。** `config.py:EndpointConfig` 默认 `linux-nvidia`，`schemas.py:EndpointCreate` 默认 `server-script-v1` —— 同一字段两个默认值，取决于从 YAML 还是 REST/MCP 进来。后者指向「远端自带采集脚本」这个最特殊的契约，用在裸机上连得上却读不到卡。统一为 `linux-nvidia`；profile 的选择说明从平铺枚举改为「哪种机器选哪个」，放在参数 `Field(description=...)` 上而不是全局 instructions（后者每轮都要付费，且 2400 字符上限未动）。
- **`keepalive_policy` 刻意不暴露。** 它必须与 `keepalive_adapter_id` 成对设置（现网四台 keepalive 主机是 `policy=idle_keepalive` + `adapter=server-script-v1`，而它们的 `observation_profile` 是 `linux-nvidia`，两者不可互相推导）。把两个耦合字段交给 Agent 正是本轮缺陷的形状；keepalive 仍是人在 App 上设的策略，不影响 apply/release。

**登记即验证。** `POST /api/v1/endpoints` 改为 `async`，创建后对该 endpoint 做一次采集，再返回新增的只读 `observation`（`observed` / `observed_at` / `gpu_count` / `error`，由 `service.endpoint_reachability` 从既有 `ProviderState` 与 `GPUDevice` 派生，无新列、无新迁移）。放在 REST 而不是 MCP 工具里，App、CLI、MCP 因此共用同一条路径。

**失败不回滚。** endpoint 保留：它是操作者「我要用这台机器」的记录，host key 或端口是人自己修的，修好后下一轮采集自己接上，不需要第二次登记；回滚还会与 idempotency 重放相互作用。

**两个客户端的等待时间随之改为按服务端预算。** 登记现在要等一次采集（插件 `PLUGIN_OBSERVE_TIMEOUT_SECONDS=45`，直连一个 SSH connect timeout），超过 `CONTROL_PLANE_READ_TIMEOUT_SECONDS=20`。`client.py` 的 `_CONTROL_PLANE_CLAIM_PATHS` 集合改为 `_CONTROL_PLANE_PATH_TIMEOUTS` 表（claim 200s / 登记 60s），同类判据收进一处。桌面端同形：`BrokerStore.mutationTimeout(forPath:)` 给 `api/v1/endpoints` 60s、其余 mutation 保持 10s —— 此前所有 mutation 固定 10s，登记必然在控制面还在连的时候超时。

**桌面端把这个结果说出来。** `addEndpoint` 改走 `performMutationWithPayload`，`registrationOutcome` 把 `observation` 变成三句人话：已连上并发现 N 张卡 / 已连上但没有发现 GPU（提示确认观测方式）/ 还没有连上（带原因）。未连上时同时写入 `errorMessage`，不再是「正在确认状态」加一分钟后一行红字。

第四轮验证：Python `622 passed`（新增 4 条：登记失败带原因并保留记录、登记成功报告卡数、登记即可归组、登记超时高于它等待的采集预算），Ruff 通过；`zsh desktop/build-macos-app.sh test` 报告 `66 tests in 3 suites passed`（新增 4 条 Swift 用例）；`build-macos-app.sh` 与 `verify-macos-app.sh` 通过。现网 `p7770` 在本轮期间已被外部修好（host key 补齐、profile 改为 `linux-nvidia`、归入 `baidu-baige`），现为 ONLINE / 4 卡，可作为该诊断的事后印证，但不是本轮改动的产物。

仍未做：`api.py` 里 32 个同步 `def` 路由未改为 `async def` + `in_domain`。它们由 FastAPI 自动丢线程池，本来就不在事件循环上，观测到的卡顿不由它们造成；改动会覆盖每个路由体，且把读也串行到单 worker，收益需要先有实测支撑。`hanhai22` 插件依赖一个 12 小时有效期的外部 ControlMaster（由 Storyboard 项目的 `tools/run/hanhai_session.py up --auto` 建立并复用同一个 `~/.ssh/cm/` socket）。socket 过期后该组静默退回上一次成功观测的数字，`largest_allocatable_block` 变 `null`；实测重开 master 后 151 秒内自行恢复（`largest_allocatable_block: 5`，40 张空闲卡分散在多节点）。MCP 目前不投影 `monitor.last_error`，所以 agent 拿到 `null` 时没有可执行的下一步。

### 2.0.0 候选（当前工作树）

以下结果针对当前工作树，在准备发布 `v2.0.0` 时复跑：

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `556 passed`，`uv run --reinstall-package serverpilot pytest -q` 通过。新增覆盖插件 v3 `limits` 校验（未知键拒绝、`lease_ends` 与 `max_lease_seconds` 一致性）、两种 `allocation` 的组级 limits 推导、`largest_allocatable_block` 在单次上限未知时为 `null`、以 `server_group_id` 路由到插件 apply，以及旧 `count\|name` 容量值的向后兼容解码 |
| Ruff / `git diff --check` | 通过 |
| macOS App | `zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 通过；根目录唯一 `ServerPilot.app` |
| 插件契约 | 本机 `hanhai22` 与随包 `slurm-immediate` 的 `info` 均返回合法 `schema_version 3` 与 `limits`；升级前已保留 `hanhai22.v2.bak` |
| 实盘数据投影 | 以 sqlite backup API 制作运行库的**只读副本**，用当前源码生成快照：委托型集群作为正常分组投影，带组名与共享工作目录；`scheduler_servers` 不再出现；该登录节点不再落入 `cpu_only_servers`；两个分组分别推导出 `direct` / `on_release` 与 `delegated` / `hard_kill_at_time_limit` / `3600`。全程未写入运行库，未重启 daemon |

上述实盘投影使用只读副本，因此它证明的是**当前源码对真实数据的投影结果**，不代表正在运行的 daemon 已加载这份代码。

## 进程观察的单一判据

**一个进程是当前事实，直到这台服务器自己连续、不间断的完整采集把它落下满 `collector.process_absence_grace_seconds`（默认 60 秒）。** 此前一次不含它的完整采集就立刻把该行置 `active=False`。这不是「阈值太敏感」：`nvidia-smi --query-compute-apps` 是与 GPU 查询分开的探针，它返回空列表时同时代表「确实没有进程」和「这个 PID namespace 看不到进程」，而 `observation_complete` 只由 GPU 探针决定，于是容器化服务器上一次看不见进程的采集被当成了进程结束的完整证据。

- **判据是证据链，不是墙上时钟。** 窗口记在 `process_observations.absent_since`：完整采集里缺席则起表，被观测到则清零，**采集不完整或采集失败则清零**。断连期间没有证据产生，因此断连不计入缺席，而是让缺席从头开始。p8908 在 45 分钟内断连三次（最长 62 秒），任何一次都不会再退休一个仍在运行的进程。
- 因为断连不再计入，窗口不需要为断连时长留余量，取 60 秒即可：按预设采集周期这是十余次连续、健康、完整且都不含该进程的观测。它不再需要与 `stale_after_seconds` 排序，也不再从它派生——两者回答不同问题，各自独立。
- **「这张卡上现在有没有进程」只有一个判据、一个 owner。** 退休发生在证据到达的地方（`ingest_observation` → `_advance_process_absence`），所有读侧（`_current_processes`、`snapshot` 的批量查询、`bind_workload`）一律只问 `active`。读侧再加一层 wall-clock 过滤就是用第二个时钟回答同一个问题，而且会把「只是停止应答的服务器」上的进程悄悄退休——这正是本控制面不能有的 fail-open。
- `finalize_keepalive_stop` 的拒绝谓词随之修正为「目标卡上出现任何不是本卡 expected keepalive worker 的当前进程即拒绝」；worker 自身是否停掉仍由 `observation_not_before` barrier 判定。不这样分开，占卡 worker 自己的行会让每一次停卡与每一次 `reclaim_keepalive_for_claim` 都 409。
- **验证通过的停卡会直接退休该 worker 的进程行。** 年龄判据存在是因为「一次采集没列出它」有歧义，而这里没有歧义——ServerPilot 命令了这个 worker 停止，又看到停卡后的完整采集不再带它。
- 这不是被否决过的那条 release-empty 门槛：服务端没有新增任何「最近有进程就拒绝」的阈值，只是 `release_empty_conflicted_lease` 已经在读的那个「当前进程」集合本身变准了。
- **卡的周转代价已收敛。** 释放后卡仍会保持 `BUSY_UNMANAGED` 直到缺席窗口走满，但那是 60 秒的连续健康观测，不再叠加断连时长；`test_a_card_its_owner_released_comes_back_once_the_absence_closes` 钉住 release → re-apply 会收敛。
- schema 变更一列：`process_observations.absent_since`（migration `20260831_0034`）。

## 占卡起卡冷却期

**`inventory.keepalive_start_cooldown_seconds`（默认 600 秒）：一张 GPU 上最近一次 `kind='workload'` 的 `LeaseResource.released_at` 距今不足冷却期时，`list_keepalive_transitions` 把它判为 `ineligible` 并给出剩余秒数，而不是 `start`。** workload 租约的释放是控制面唯一的「主人干完了」信号，而分阶段任务两批之间的空窗与它长得一样；p8908 里释放后 8 秒占卡就占满了 65 GiB 显存。冷却期直接由既有列现查现算，不加列、不加迁移。只作用于**起卡**：`plan_keepalive_reclaim` / `reclaim_keepalive_for_claim`（Agent 申请时占卡让位）走 `_eligible_gpus(include_reclaimable_keepalive=True)` 这条独立路径，不受影响。

## 尚未完成的验证

Swift 单元测试已迁到 swift-testing，`zsh desktop/build-macos-app.sh test` 在本机（仅 CommandLineTools）可跑，62 个用例通过；XCUITest 仍需 Xcode，本机未运行。原生 APP 的实机按钮证据属于 `1.5.5`；`1.5.6` 在补齐根 App 构建与定向现场验收前，不能沿用该证据。尚未完成的桌面辅助体验检查包括 VoiceOver、键盘完整焦点顺序、缩放重排、色彩对比度测量与 Reduce Motion；不能仅凭截图宣称这些辅助功能完全合规。

以下运行环境能力仍未验证：

- host PID 的启动 ticks 尚未由 collector schema v2 公开；因此 worker 恢复使用 sealed namespace worker 证明、UUID 唯一 driver PID、boot ID 与新鲜观测交叉确认。若要把 host PID 重用窗口进一步收紧到启动 ticks，需单独演进 collector 协议并部署新入口，不能把 namespace ticks 当作它的替代品。**进程观察的宽限期把这个重用窗口从一个采集间隔放宽到宽限期本身**（`username` 为空的宿主 PID 分支没有 start-time 可比，身份复用只以「同 GPU、同 boot、行仍 active」为条件）：宽限期内同一 PID 被另一个同样不可见的 CUDA 进程复用时，新进程会被并入旧行。这只可能发生在原进程确实已退出的分支上——原进程还在跑时它的 PID 不可能被复用——后果是一张卡被继续记在已死的租约名下直到那个外来进程退出，方向上是扣住卡而不是发出去。尚未在现场观测到该重用。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 其他 MCP 客户端的现场联调。
- 服务器分组、五工具 MCP、浏览器界面移除与统一集群模型已在源码落地，但尚未记录针对该合同的现场 daemon/MCP 联调：本轮只用运行库的只读副本验证投影，没有重启 daemon，也没有让升级后的插件真正跑过一次 `observe`。因此 `scheduler_capacity` 的新键（`largest_free_block`、`vram_mib`、`max_gpus_per_lease`、`cpu_cores_per_gpu`、`memory_mib_per_gpu`）尚无现场证据，实盘副本中它们仍来自升级前的旧编码而为空。已安装连接器描述是否已刷新，不在本文件宣称范围内。
- MCP 进程已经退出后，由调用方重新发起的新工具调用没有跨进程稳定 token，不能与旧调用判定为同一次传输重放；当前公开五工具合同不增加该 token。
