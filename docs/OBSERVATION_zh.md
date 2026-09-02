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

以上查询的具体命令行由代码固定（`src/serverpilot/adapters.py` 的 `GPU_QUERY`、
`PROCESS_QUERY`、`IDENTITY_QUERY`、`HOST_RESOURCES_QUERY`），endpoint 配置、REST/MCP
输入和 Agent 都不能替换或追加参数。没有 NVIDIA runtime 或没有 GPU 的主机仍然可以登记：
这类主机的 CPU/内存观测正常可用，只是不参与 GPU 分配。

多台主机各自独立探测、各有自己的连接与命令超时预算，一台主机的失败或超时不影响其他
主机的观测。到期仍没有新观测时，ServerPilot 把该主机视为连接或采集问题，停止分配它的
资源，不会拿旧数据冒充当前状态。

## 插件契约：`info` / `observe` / `apply` / `release`

插件是放在已知目录里的可执行文件，用于内置 `linux` 探针覆盖不了的场景——主要是共享调度器
集群（Slurm / LSF / PBS）：内置探针会把整个集群当成一台裸机，也分不清哪几张卡是别人作业
占的。普通服务器 / 自建节点不需要插件，直接登记 `linux` profile 即可。

ServerPilot 只用四个固定动词调用插件，不把用户或 Agent 的字符串拼进 shell，也不把
`BrokerService`、数据库或租约状态交给插件：

| 动词 | 谁调用 | 作用 | 输出 |
| --- | --- | --- | --- |
| `info` | 发现 / `plugin list` | 自述 | `{plugin_id, display_name, schema_version, capabilities, limits}`，可选 `description` |
| `observe` | 采集器 | 读取当前用户可见容量 | 固定字段的 JSON：身份、主机资源、GPU、进程，可选一个顶层 `scheduler` 提示 |
| `apply --gpu-count N --task-ref REF` | `BrokerService` 在分配前 | 立即申请 | `{allocation_ref, gpus[], ssh{host,port,user}, workspace_path, cuda_visible_devices}` |
| `release --allocation-ref REF` | `BrokerService` 在释放租约时 | 释放该次分配 | `{state: "released"}` |

`info.schema_version` 必须是 `3`（`PLUGIN_SCHEMA_VERSION`）。`plugin_id` 必须匹配
`^[a-z][a-z0-9-]{1,39}$`，并与文件名一致。`capabilities` 取 `observe` / `apply` / `release`
的子集，声明了的动词才会被调用；排队式集群做不到「立即拿到」时只声明 `observe`。

### `observe` 的 JSON 合同

顶层对象必须且只能包含（`schema_version` 恒为 `2`）：

```json
{
  "schema_version": 2,
  "identity": {"hostname": "node-a", "boot_id": "..."},
  "host": {
    "cpu_count": 64,
    "load_1m": 1.25,
    "cpu_total_ticks": 1000,
    "cpu_idle_ticks": 750,
    "memory_total_mib": 262144,
    "memory_available_mib": 196608
  },
  "gpu_probe_available": true,
  "gpus": [{"gpu_index": 7, "cuda_ordinal": 0, "gpu_uuid": "GPU-...", "name": "...", "total_vram_mib": 81920, "memory_used_mib": 0, "memory_free_mib": 81920, "gpu_utilization_pct": 0, "memory_utilization_pct": 0, "temperature_c": 35, "power_watts": 100.0, "pstate": "P0", "health": "OK"}],
  "processes": [{"gpu_uuid": "GPU-...", "pid": 123, "used_memory_mib": 1024, "executable": "python", "username": "gpu", "process_started_at": "2026-08-10T00:00:00+00:00"}]
}
```

可以多一个顶层 `scheduler` 对象，只表达「这个 endpoint 能按需申请、现在大约有多少空闲」，
不进入 GPU 账本，直到真正申请时才入账：

```json
{"scheduler": {"free_gpu_count": 30, "gpu_name": "NVIDIA A100-SXM4-80GB", "note": "按需申请，不排队"}}
```

`free_gpu_count` 和 `gpu_name` 必填，`note` 可选，只允许这三个字段。`gpu_index` 保留
`nvidia-smi index` 供界面识别；`cuda_ordinal` 固定表示设置
`CUDA_DEVICE_ORDER=PCI_BUS_ID` 之后的执行 selector，由插件按 `pci.bus_id` 排序算出，
两者不得混用。`identity`、`host`、每个 GPU 和每个 process 的字段集合是固定的，不能
扩展。数值必须是 JSON number（不能用字符串、NaN 或 Infinity），字符串不能含控制字符，
`process_started_at` 必须带时区。GPU UUID、index 和 CUDA ordinal 各自不得重复；process
的 `(gpu_uuid, pid)` 不得重复，且必须指向本快照里的 GPU。

没有 NVIDIA runtime 或没有 GPU 的机器，应返回 `gpu_probe_available: false` 和空的
`gpus`、`processes`，这样 CPU/内存观测仍然可用，同时这一轮不算完整 GPU 观测。

坏输出、超时或超长 stdout 一律失败，不降级。`apply` 失败时释放掉已拿到的部分。
「集群此刻没有空闲」与「出错了」靠退出码区分，不靠错误文字：`apply` 在确实没有可立即
分配的卡时退出码 **3**，落到普通的 `no_capacity`；其余任何失败（配额被拒、节点故障、
SSH 不通、插件自身异常）退出非零的其它值，作为 `plugin_apply_failed` 报给调用方。

### `info.limits`

v3 的 `info` **必须**带 `limits` 对象，四键齐全、不许多键；缺块、缺键、多未知键，都和
错误的 `schema_version` 一样让 `probe_plugin` 失败：

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `lease_ends` | 这次分配怎么结束：集群到点杀作业，和持有到调用方 `release`，对同一实验不可互换，必须声明，不能靠推断 | `on_release` 或 `hard_kill_at_time_limit` |
| `max_lease_seconds` | 硬时限秒数。`hard_kill_at_time_limit` 时必填正整数（1–2592000，最多 30 天）；`on_release` 时必须是 `null` | 正整数或 `null` |
| `apply_max_seconds` | 这次 `apply` 最多等多久——强制超时，不是说明：超过它就被终止并按失败处理。上限 180 秒（`client.CONTROL_PLANE_CLAIM_TIMEOUT_SECONDS` 是另一半，两者被测试钉在一起）。不声明等待窗口就写 `null`，回落到通用变更超时。声明超过 180 会让 `info` 校验失败，插件在发现阶段静默消失 | 正整数（1–180）或 `null` |
| `queues` | 当前契约不接受排队 | 必须是 `false` |

内置 `linux` profile 等价于 `on_release`、`max_lease_seconds: null`、`queues: false`，
且不声明 `apply_max_seconds`：直连申请的成本不由 profile 公布，而是由一次申请实际花掉的
adapter 与采集超时推导，由所属服务器分组投影出来。随包参考插件 `slurm-immediate` 声明
`hard_kill_at_time_limit`、`max_lease_seconds: 3600`、`apply_max_seconds: 33`、
`queues: false`。

已经放进插件目录、但仍返回 `schema_version: 2` 或缺少合法 `limits` 的可执行文件，
**不会在发现时打出错误**：`discover_plugins` 对 `probe_plugin` 的失败直接跳过，
`plugin list` 里看不到它，控制面当它不存在，文件还在目录里，人容易以为「已经装好了」。
`serverpilot doctor` 会把这类失败明确列出来；`serverpilot plugin add` 走同一套校验，
会当场拒绝并报错，坑只在已经就位、只靠发现的那条路径。

`info` 是子进程，发现会发生在每一轮采集和每一次申请上，所以它的结论按文件内容缓存：
目录每次都重新列，同一个文件不变就不再重新 fork，改写文件即失效。

### 目录与安装

发现按下面顺序搜索，同名时后出现的覆盖前面的：

1. 发行版内置：安装包内的 `bundled_plugins/`（源码在 `src/serverpilot/bundled_plugins/`）
2. 仓库检出目录：仓库根目录 `plugins/`（若存在；给本地开发用，不随发行版分发）
3. 用户目录：macOS `~/Library/Application Support/ServerPilot/plugins`，
   Windows `%LOCALAPPDATA%\ServerPilot\plugins`

```bash
serverpilot plugin list
serverpilot plugin add ./my-plugin
```

`plugin add` 只做校验和复制，不签名，也不接远程仓库；站点配置不会随 `plugin add`
一起复制，要自己放在插件旁边。本机插件以当前用户权限运行，放进插件目录的脚本由用户
自己负责。

### 参考插件：`slurm-immediate`

`info` 不读配置。`observe` / `apply` / `release` 只从插件可执行文件旁边的
`slurm-immediate.json` 读取集群参数，文件不存在或缺必填字段时立即失败，并指出要写
哪个文件：

```json
{
  "ssh_host": "cluster",
  "partition": "gpu",
  "gpu_name": "Example GPU 80GB",
  "vram_mib": 81920,
  "gpus_per_node": 8,
  "cpus_per_gpu": 8,
  "mem_gib_per_gpu": 16,
  "qos": "gpu",
  "account": "myaccount",
  "ssh_port": 22,
  "control_path": "/path/to/controlmaster/%C",
  "workspace_path": "/home/alice/work"
}
```

必填：`ssh_host`、`partition`、`gpu_name`、`vram_mib`、`gpus_per_node`、`cpus_per_gpu`、
`mem_gib_per_gpu`。可选：`qos`、`account`、`ssh_port`、`control_path`、`workspace_path`
（未写时用远端家目录）。行为：

- 复用已有 OpenSSH ControlMaster（`ssh -O check`）；master 不在时报错退出，绝不认证、
  不弹密码、不消耗 TOTP。
- `observe` 只报告当前用户自己的作业；没有本用户作业时返回 0 张卡，另带集群空闲提示
  （不进 GPU 账本）。
- `apply` 用 `srun --immediate`：作业不能马上启动则失败，不排队。
- `release` 对这次分配的 Slurm job id 调用 `scancel`。
- 计算节点上的 CUDA 只在该 Slurm 作业里有效，登录节点本身没有 GPU。

不要把站点拓扑写进仓库，或写进随发行版分发的插件文件。

### 写一个插件

`info` 必须先能跑通，且不依赖站点配置。`observe` 必须打印固定字段集合的 JSON（同上）。
插件若还声明 `apply` / `release`，输出必须符合上表，ServerPilot 才会用它申请和回收。

插件只负责「怎么连这台、怎么读、怎么申请/释放」，ServerPilot 仍负责账本、分配、空闲
回收和展示：真实 GPU 用真实 NVIDIA UUID；`apply` 要能立即拿到，拿不到就失败
（`no_capacity`），不要排队；`release` 只释放自己的那次分配；生命周期（空闲回收、
到期清空、租约展示）由 ServerPilot 管，插件不重复实现。

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
