# 服务器插件

插件是放在已知目录里的可执行文件。ServerPilot 只用固定动词调用它，不把用户或 Agent 的字符串拼进 shell，也不把 `BrokerService`、数据库或租约状态交给插件。

密封的是**调用契约**：动词固定、参数形状固定、输出严格校验。实现来源不再密封——放进插件目录的脚本由用户自己负责，和放进 `PATH` 的程序同一信任级别。

采集走 `observe`；真正申请和释放 GPU 时，`service.py` 会调用 `apply_plugin` 与 `release_plugin`。四个动词都是正式契约，不是只有观测。

## 动词

| 动词 | 谁调用 | 作用 | 输出 |
| --- | --- | --- | --- |
| `info` | 发现 / `plugin list` | 自述 | `{plugin_id, display_name, schema_version, capabilities, limits}`，可选 `description` |
| `observe` | 采集器 | 读取当前用户可见容量 | schema v2 JSON，与 `serverpilot-collect` 相同 |
| `apply --gpu-count N --task-ref REF` | `BrokerService` 在分配前 | 立即申请 | `{allocation_ref, gpus[], ssh{host,port,user}, workspace_path, cuda_visible_devices}` |
| `release --allocation-ref REF` | `BrokerService` 在释放租约时 | 释放该次分配 | `{state: "released"}` |

`info.schema_version` 必须是 `3`（`PLUGIN_SCHEMA_VERSION`）。`plugin_id` 必须匹配 `^[a-z][a-z0-9-]{1,39}$`，并与文件名一致。`capabilities` 取 `observe` / `apply` / `release` 的子集，声明了的动词才会被调用。排队式集群做不到「立即拿到」时只声明 `observe`。

`observe` 的采集 JSON 仍是 schema v2，和 `serverpilot-collect` 同一份合同；变的是插件 `info` 契约，不要把两个版本号混成一个。

### `info.limits`

v3 的 `info` **必须**带 `limits` 对象，四键齐全、不许多键。缺块、缺键、多未知键，都和错误的 `schema_version` 一样：`probe_plugin` 失败。

| 字段 | 含义 | 取值 |
| --- | --- | --- |
| `lease_ends` | 这次分配怎么结束。集群到点杀作业，和持有到调用方 `release`，对同一实验不可互换，所以必须声明，不能靠推断。 | `on_release` 或 `hard_kill_at_time_limit` |
| `max_lease_seconds` | 硬时限秒数。`hard_kill_at_time_limit` 时必填正整数（1–2592000，即最多 30 天）；`on_release` 时必须是 `null`。 | 正整数或 `null` |
| `apply_max_seconds` | 这次 `apply` 最多等多久。不声明等待窗口就写 `null`。 | 正整数（1–3600）或 `null` |
| `queues` | 当前契约不接受排队。 | 必须是 `false` |

内置观测 profile（`linux-nvidia` / `linux-host` / `server-script-v1`）等价于 `on_release`、两个时限 `null`、`queues: false`。随包 `slurm-immediate` 声明 `hard_kill_at_time_limit`、`max_lease_seconds: 3600`、`apply_max_seconds: 33`、`queues: false`。

### 停留在 v2 会怎样

已经放进插件目录、但仍返回 `schema_version: 2` 或缺少合法 `limits` 的可执行文件，**不会在发现时打出错误**。`discover_plugins` 对 `probe_plugin` 的失败直接 `continue`，`plugin list` 里看不到它，控制面当它不存在。文件还在目录里，人容易以为「已经装好了」。

`serverpilot plugin add` 走同一套 `probe_plugin`，会当场拒绝并报 `schema_version must be 3` 或 `limits` 校验失败。坑在已经就位、只靠发现的那条路径：升级控制面之后，旧插件会静默从列表里消失，对应集群不再被观测或申请。

坏输出、超时或超长 stdout 一律失败，不降级。`apply` 失败时释放掉已拿到的部分。

「集群此刻没有空闲」与「出错了」必须靠**退出码**区分，不能靠错误文字：`apply` 在确实没有可立即分配的卡时退出 **3**，其余任何失败（配额被拒、节点故障、SSH 不通、插件自身异常）退出非零的其它值。ServerPilot 只看退出码——3 会让本次申请落到普通的 `no_capacity`，其它值作为 `plugin_apply_failed` 报给调用方。把配额被拒当成「没卡」，会让 Agent 一直等一个永远不会出现的空位。

## 目录

发现按下面顺序搜索，**同名时后出现的覆盖前面的**：

1. 发行版内置：安装包内的 `bundled_plugins/`（源码在 `src/serverpilot/bundled_plugins/`）
2. 仓库检出目录：仓库根目录 `plugins/`（若存在；给本地开发用，不随发行版分发）
3. 用户目录：macOS `~/Library/Application Support/ServerPilot/plugins`，Windows `%LOCALAPPDATA%\ServerPilot\plugins`

发现时会对每个可执行文件跑一次 `info`；跑失败的文件直接忽略，不影响整个列表——包括仍停留在 v2、或 v3 但 `limits` 不合法的文件。`info` 不依赖集群配置，没配置好的参考插件也能被列出来。

## 安装

把可执行文件放进用户插件目录，或：

```bash
serverpilot plugin list
serverpilot plugin add ./my-plugin
```

`plugin add` 只做校验和复制，不签名，也不接远程仓库。站点配置（见下）不会随 `plugin add` 一起复制，要自己放在插件旁边。

## 接入其他服务器与集群

大多数服务器不需要插件。直接 SSH + 固定 `serverpilot-collect` 就能接入：有管理权限的服务器装一次入口脚本，在 App 里登记 SSH 与绝对远端工作目录即可。不要为每台普通服务器写插件。

插件只用来补内置采集覆盖不了的缺口。怎么选：

- **普通服务器 / 自建节点**：能用 `serverpilot-collect` 就用它，profile 选 `linux-nvidia` / `server-script-v1`，不需要插件。
- **共享调度器集群（Slurm / LSF / PBS）**：内置采集会把整个集群当成一台裸机，也分不清哪几张卡是别人作业占的。这时用插件接管观测，只登记当前用户自己的作业；可选实现 `apply`，用 `srun --immediate` 一类的立即申请。
- **只能看容量、做不到立即申请**的集群：只声明 `observe`，把空闲量放进 `scheduler` 提示，不实现 `apply`；ServerPilot 仍能看到容量，但分配会明确返回 `no_capacity`。

随发行版附带的参考实现是 `slurm-immediate`：Slurm 分区可配置、按用户过滤、用 `srun --immediate` 立即申请、释放时 `scancel`。换集群时改 JSON 配置，或照着它的结构改插件，而不是给 ServerPilot 加新分支。

## 参考插件：`slurm-immediate`

`info` 不读配置。`observe` / `apply` / `release` 只从**插件可执行文件旁边**的 `slurm-immediate.json` 读取集群参数，没有第二套查找路径，也不会猜测分区或默认集群。文件不存在或缺必填字段时立即失败，并指出要写哪个文件。

把插件复制到用户插件目录，再在同一目录写下配置，例如：

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

必填：`ssh_host`、`partition`、`gpu_name`、`vram_mib`、`gpus_per_node`、`cpus_per_gpu`、`mem_gib_per_gpu`。可选：`qos`、`account`、`ssh_port`、`control_path`、`workspace_path`。未写的 `qos` / `account` / `control_path` 不会传给 `srun` 或 `ssh`；未写 `workspace_path` 时用远端家目录。

行为：

- 复用已有 OpenSSH ControlMaster（`ssh -O check`）；master 不在时报错退出，绝不认证、不弹密码、不消耗 TOTP。
- `observe` 只报告当前用户自己的作业；没有本用户作业时返回 0 张卡，另带集群空闲提示（不进 GPU 账本）。
- `apply` 用 `srun --immediate`：作业不能马上启动则失败，不排队。
- `release` 对这次分配的 Slurm job id 调用 `scancel`。
- 计算节点上的 CUDA 只在该 Slurm 作业里有效，登录节点本身没有 GPU。

不要把站点拓扑写进仓库，或写进随发行版分发的插件文件。

## 插件该把什么留给 ServerPilot

插件只负责「怎么连这台、怎么读、怎么申请/释放」。ServerPilot 仍负责账本、分配、空闲回收和展示，所以：

- `observe` 的输出就是 schema v2（见 [COLLECTOR_SCRIPT_zh.md](COLLECTOR_SCRIPT_zh.md)），可以多一个顶层 `scheduler` 提示；不要把别人的作业或整个集群合成假卡。
- 真实 GPU 用真实 NVIDIA UUID；没有遥测来源时可以给身份行，作业内 `cuda_ordinal` 从 0 起排，让采集器能去重。
- `apply` 要能立即拿到，拿不到就失败（`no_capacity`），不要排队；`release` 只释放自己的那次分配。
- 生命周期（空闲回收、到期清空、租约展示）由 ServerPilot 管，插件不重复实现。

## 写一个插件

`info` 必须先能跑通，且不依赖站点配置。`observe` 必须打印 schema v2，见 [COLLECTOR_SCRIPT_zh.md](COLLECTOR_SCRIPT_zh.md)。插件若还声明 `apply` / `release`，输出必须符合上表，ServerPilot 才会用它申请和回收。
