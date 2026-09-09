# 观测与占卡：密封调用契约

服务器状态只来自两条路径：内置只读探针 `linux`，或本机插件的固定动词。两条路径共用
同一条边界——密封的是**调用契约**（动词固定、参数形状固定、输出严格校验），不是实现来源。
它们都不是第二个控制面，也不是远程命令入口：不接收 shell、argv、SSH 参数、机密信息或
Agent 自定义 target；资源身份、准入、租约和审计始终属于 ServerPilot。

endpoint 的 `observation_profile` 只能取两种值：内置的 `linux`，或本机已发现的插件 ID。
共享调度器集群（Slurm 一类）通过插件接入，和普通主机一样登记为 endpoint，不是单独的
调度器对象。空闲占卡走另一条固定协议，见下文「空闲占卡」。

未知 profile、未知 capability、过期或冲突的观测、不确定的远端结果，一律 fail closed。

## 内置 profile：`linux`

`linux` 是唯一内置的观测 profile，只对已登记主机执行固定的只读 SSH 探针，远端不需要
安装任何东西：

- 身份：`hostname` + `/proc/sys/kernel/random/boot_id`；
- GPU：固定的 `nvidia-smi --query-gpu=...` 与 `--query-compute-apps=...`；
- 主机资源：`/proc/loadavg`、`/proc/stat`、`/proc/meminfo`，以及可读时的 cgroup
  `cpu.max` / `cpu.stat` / `memory.max` / `memory.current`，用于把容器化主机的容量算成
  它实际拿到的份额而不是整机。

以上查询的具体命令行由代码固定（`src/serverpilot/observation_probe.py` 的 `GPU_QUERY`、
`PROCESS_QUERY`、`IDENTITY_QUERY`、`HOST_RESOURCES_QUERY`），endpoint 配置、REST/MCP
输入和 Agent 都不能替换或追加参数。没有 NVIDIA runtime 或没有 GPU 的主机仍然可以登记：
这类主机的 CPU/内存观测正常可用，只是不参与 GPU 分配。

多台主机各自独立探测、各有自己的连接与命令超时预算，一台主机的失败或超时不影响其他
主机的观测。到期仍没有新观测时，ServerPilot 把该主机视为连接或采集问题，停止分配它的
资源，不会拿旧数据冒充当前状态。

## 共用节点观测

普通 SSH 端点使用节点 `self`；插件端点使用计算节点标识。身份键为端点＋节点，GPU
再按 UUID 关联，PID 和显示序号不会跨节点合并。迁移保留普通服务器的 GPU ID 和租约关系。

两种入口执行 `observation_probe.py` 中同一份固定 Linux/NVIDIA 探针，并由 Collector
同一解析器校验。独立插件内的探针字面量由 `python -m serverpilot.observation_probe` 生成。
Slurm 插件只发现本人作业节点并关联分配；每节点每轮只读 SSH 采一次，不创建 srun 步骤，
不启动远端采集服务，不拿登录节点冒充计算资源。无法验证主机密钥或无法登录时保留分配事实，
实时使用量为未知并附原因。

REST `resource_nodes` 提供统一的节点身份、新鲜度、失败原因、GPU 关联和当前用量；
CLI、MCP、桌面消费同一投影。CPU 采用相邻 `/proc/stat` 差分；有明确 cgroup 限额时
沿用该范围，重启或计数回退清空基线。GPU 利用率按节点完整样本求平均，显存按容量加权。
部分节点失败不污染兄弟节点，失败和过期指标不填零。Slurm 节点即使观测空闲也必须经调度申请。

作业 CPU 时间与 RSS 保留为独立 accounting，不能充当节点使用率；整卡指标也不代表某进程
独占用量。分配设备只能用明确 UUID 或已验证的整节点分配关联，未知映射保留未知。

## 插件契约 v5

普通服务器使用内置 `linux` 只读探针；集群使用本机可执行插件。核心按能力处理，
不按站点、调度器名称或 GPU 型号分支。插件文件名必须等于 `plugin_id`。

| 动词 | 调用方 | 契约 |
| --- | --- | --- |
| `info` | 发现流程 | `schema_version=5`，声明能力、`limits`、`release_granularity`、`entry_kind` |
| `observe` | Collector | 只读返回 schema 3 节点原始探针输出、节点容量、分配事实；不认证 |
| `apply` | service | 接收卡数、任务、预计秒数、稳定 `request_id` 和需求 JSON；返回完整分配 |
| `release` | service | 按远端引用归还；只有确认结束才返回 `state=released` |
| `connect / disconnect` | daemon 连接协调器 | 可选、必须成对声明；只管理连接，不碰分配或用户程序 |

`apply / release` 必须成对声明，当前外部插件声明 `release_granularity=allocation`；
普通主机内置实现为 `device`。`entry_kind` 为 `ssh` 或 `allocation`。
`limits` 四键为 `lease_ends`、`max_lease_seconds`、`apply_max_seconds`、`queues=false`；
硬时限提供方必须返回实际截止时间。最长请求 30 天，站点可配置更小的上限。
发现失败及旧版插件在插件列表显示需要修复，不保留旧版执行协议。

分配正式保存在 `resource_allocations`，包含端点、提供方、远端引用、租约、位置、
进入信息、请求时长、实际截止时间、状态及确认时间。位置分别表达节点、真实 GPU UUID、
物理卡号和环境内设备编号；未知字段为空，未知设备列表为空，不伪造 GPU 或计算进程。
进入信息是结构化 `entry`：登录 host/port/user、workspace、显示用 argv、说明与 verified。
控制面不执行 entry.argv；GUI、CLI、MCP 消费同一结果。

`scheduler.observed_at` 是容量采样时间，`allocations[].observed_at` 是分配确认时间，
`workload_observed_at` 是计算活动采样时间。读取旧文件不能刷新时间。缺少容量时间时不展示
当前可申请容量；断连保留已知节点与分配位置并标注过期。外部同账号作业只展示，不绑定任务。

申请先持久化稳定标识，再调用远端；重复标识必须查询原申请，不能新建第二份分配。
结果不明返回 `allocation_unconfirmed`，再次调用先只读核对。只有专用退出码 3 表示
`no_capacity`，退出码 2 表示需求无效；其他错误不能当作资源不足。
归还失败保留占有和 `release_pending`，重新观测确认终态后才结束本机租约。
整份回收要求任务心跳失效且全部资源连续新鲜观测为空闲；运行、未知或观测中断都不满足。

用户显式开启连接后，daemon 持久化维持意图并在重启后继续；每端点最多一个连接操作。
认证失败或原因不明停止恢复，等待再次显式操作；明确网络不可达时等待网络恢复。
连接诊断只保存阶段、时间和脱敏信息，不保存认证会话、密码或动态码。

### 安装参考插件

搜索目录依次是随包 `bundled_plugins/`、检出目录 `plugins/` 和用户插件目录；同名用户插件优先。
macOS 用户目录为 `~/Library/Application Support/ServerPilot/plugins`。
`serverpilot plugin list` 查看发现结果，`serverpilot plugin add ./my-plugin` 校验并安装可执行文件。
站点配置独立放在可执行文件旁的同名 `.json` 文件；不随插件复制。

`slurm-immediate` 可复制并改成站点名独立安装，运行时不依赖其他项目。示例：

```json
{
  "ssh_host": "cluster",
  "partition": "gpu",
  "gpu_name": "Example GPU 80GB",
  "vram_mib": 81920,
  "gpus_per_node": 8,
  "cpus_per_gpu": 8,
  "mem_gib_per_gpu": 16,
  "max_lease_seconds": 86400,
  "ssh_port": 22,
  "workspace_path": "/home/alice/work",
  "entry_verified": false
}
```

前八项必填。可选 `qos`、`account`、`control_path`、`credential_path`。
`info` 可读非敏感配置、不能读取凭据；未配置仍可发现。
只有 `connect` 读取配置指向的 600 权限凭据文件（PASSWORD/SECRET），也支持已有 SSH 密钥。
`observe/apply/release` 仅复用现有 ControlMaster。显式连接命令：
`serverpilot endpoint connect ENDPOINT_ID`，停止维持加 `--disconnect`。

Slurm 申请将任务秒数向上取整为分钟传入 `srun --time`，使用立即申请窗口而不排队。
返回的进入方式使用已有 job id；`entry_verified=false` 表示该站点现场进入路径尚未验证。
GPU 环境内编号由 CUDA Driver API 的 UUID 映射确认，不能把物理卡号当作 CUDA 编号。
`release` 调用 scancel 后还要取得调度器终态，命令返回成功本身不等于归还已确认。

## 空闲占卡：`server-script-v1`

`server-script-v1` 现在只是一个 keepalive adapter id，不是观测 profile；观测层已经收敛
成上面两条路径。它对应一个固定协议，让 ServerPilot 在已验证空闲的 GPU 上挂一个占用
worker，防止显存被外部抢占：

| Adapter | 能力 | 允许的工作 | 不允许 |
| --- | --- | --- | --- |
| `raw-ssh` | observation | 固定的主机 / GPU 遥测和已观测 PID 的进程详情 | 任意 shell、读取私钥、写入租约 / 申请 |
| `server-script-v1` | endpoint_keepalive | 先预检 `serverpilot-keepalive --protocol-info`，再执行固定的 `serverpilot-keepalive --schema-version 3` 做空闲 GPU 占卡；身份恢复只读执行固定的 `--inspect --schema-version 3` | 项目任务启停、调用方指定 PID/GPU、路径或环境 |

endpoint 不要求用户手工填写 adapter。用户明确点击「开始占卡」时，ServerPilot 自动挂载
代码内置的 `server-script-v1` helper；公开接口仍然只有 `enabled: true|false`。开启时只对
已验证的空闲 GPU 做 reconcile；忙碌、未托管、冲突或状态过期的 GPU 一律不动。ServerPilot
传入精确的物理 UUID，helper 只能管理自己的 worker。每张 GPU 独立判断：一张 GPU 冲突不会
阻断同一服务器上其他空闲 GPU 的占卡启动。

adapter 在做任何启停变更前，先执行只读的 `--protocol-info` 预检。预检真正校验的是
`kind=serverpilot-keepalive`、`schema_version=3`（`KEEPALIVE_SCHEMA_VERSION`），以及这
四个能力：`per_gpu_keepalive`、`pidfd_identity`、`pci_bus_id`、`worker_attestation`。
helper 还会报告 `implementation_version`，该值等于构建它的 ServerPilot 包装版本
（`KEEPALIVE_IMPLEMENTATION_VERSION` 直接取自 `serverpilot.__version__`）；adapter **不**
按这个数字做兼容判断。预检失败返回 `keepalive_helper_incompatible`，不发送任何变更
payload。旧 v2 wire/state 版本直接拒绝。

恢复时只接受 helper 自己的 v3 状态里仍然存活、且带固定 marker 的 worker：helper 对指定
物理 UUID 做固定的 NVIDIA compute 查询，必须恰好得到一个 driver-visible PID，ServerPilot
再用这个 PID 和 Linux boot ID 与最新一次采集观测对照。历史工作负载租约如果遗留为
「归属待确认」，人可以在监控端执行「清理遗留归属」；ServerPilot 会先重新采集，确认相关
GPU 上都没有进程后才释放，只看 0% 利用率不会直接释放。

每张目标 GPU 都有独立的内部租约、worker 和健康状态。worker 按该卡 CUDA 可见总显存占用
约 80%、GPU 利用率约 80%，只用单个 PyTorch CPU 线程，稳态下无磁盘 / 网络 I/O；实际的
CPU、RSS、GPU 干扰和停止响应，仍须在获授权的目标主机上验证。

即时受管的申请，只有在普通分配失败、且服务已规划出完整并验证过的逐卡回收方案时，才会
停止这些 worker、重新采集确认为空，然后重试原申请。它不影响同机其他卡、未托管进程和
直接 SSH 任务；要在这台机器上直接 SSH 干活前，管理员应显式关闭该 endpoint 的占卡策略。

### 控制面不可用时的人工恢复

占卡 worker 以独立会话启动，与控制面分离；控制面停止后它们会继续占卡，没有自动释放
路径。三条显式命令覆盖这种情况，都不要求 daemon 在线：

| 命令 | 作用 |
| --- | --- |
| `serverpilot keepalive inspect --endpoint <id>` | 只读，经固定 `--inspect --schema-version 3` 报告远端仍在跑的 worker 及其 PID |
| `serverpilot keepalive stop --endpoint <id>` | 对该 endpoint 当前观测到的全部 GPU 执行固定停止命令 |
| `serverpilot daemon reclaim` | 端口被非 launchd 托管的 ServerPilot 占住时停止它并交还给 LaunchAgent |

它们只从控制面数据库读取 endpoint 的连接信息，GPU UUID 来自一次现场只读采集，其余全部
走密封 adapter；不写库，也不复制领域规则。endpoint 被暂停时这些命令仍然可用——这正是
需要它们的场景。三条命令只能由人或 Agent 显式调用，不是后台策略：控制面恢复后，
reconcile 会按既有策略重新决定占卡。

`daemon reclaim` 只处理「有 ServerPilot 在应答且实例标识匹配、但不归 launchd 管」这一种
情况。daemon 归属正常时它不动任何进程；SIGTERM 后进程仍在则报错交给人处理，不自行升级
到 SIGKILL。

## 不变边界

- adapter / 插件都不接触 `BrokerService`、数据库，不做租约 / 申请写入：`apply` / `release`
  只是让插件去申请或释放它管理的远端资源，`service.py` 校验插件返回的结果后才写入租约；
- 不新增通用 `execute()`、第二套认证或非 loopback listener；
- GPU 身份始终是 `endpoint_id:gpu_uuid`，adapter / 插件 ID 只作诊断来源。
