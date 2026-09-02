# ServerPilot Agent / MCP 使用指南

ServerPilot 负责确认和协调算力归属；项目自己启动、停止已获授权的工作负载。工具参数和完整约束以 MCP server instructions / schema 为准，本页只写安装和日常路径。

## 安装与注册

在本仓库根目录安装本机 daemon：

```bash
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

按客户端注册 MCP，并安装短全局规则：

```bash
serverpilot mcp install --client codex     # 或 claude、cursor
```

`install` 只写入该客户端的启动命令，不刷新客户端已经缓存的工具列表。Cursor 这类客户端要 Disable → Enable，或重载窗口，才会重新 `tools/list`。漏做这一步时，客户端仍按旧 schema 调工具（现场出现过源码已是 5 个工具、客户端仍按旧的 3 工具 schema 调用）。

`install` 走客户端自己的机制：Codex 与 Claude Code 调用它们的 `mcp add`，Cursor 合并进 `~/.cursor/mcp.json`（保留已有的其他 server 和无关配置项）。客户端 CLI 不在 PATH 上时命令会明确失败，并提示改用 `config` 自行注册。

`serverpilot mcp config --client all` 只打印不写盘，输出每个客户端的注册命令或配置文件内容，用于手工粘贴与核对。

| 客户端 | MCP 注册 | 全局规则 |
| --- | --- | --- |
| Codex | `serverpilot mcp install --client codex` | `python3 scripts/install_agent_policy.py codex --install` |
| Claude Code | `serverpilot mcp install --client claude` | `python3 scripts/install_agent_policy.py claude --install` |
| Cursor | `serverpilot mcp install --client cursor` | `python3 scripts/install_agent_policy.py cursor --print` 后粘贴到 User Rules |

标准配置块（手工注册时使用）：

```json
{
  "mcpServers": {
    "serverpilot": {
      "command": "serverpilot-mcp",
      "env": { "SERVERPILOT_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

两条安装路径给出的 `command` 不同。源码 / `uv tool install` 安装后 `serverpilot-mcp` 在 PATH 上，直接写命令名即可；Windows 桌面压缩包不修改 PATH，要填解压目录里 `serverpilot-mcp.exe` 的绝对路径。`serverpilot mcp config` 会自动解析出当前安装实际可用的那一个。桌面 App 的设置页也展示同一份解析结果：绝对路径和可粘贴的 `mcpServers` 配置；找不到入口时会显示「未找到」和安装提示，而不会编造路径。

daemon 未运行时，macOS 上的 MCP 会尝试启动同一用户的 LaunchAgent；daemon 不兼容或未就绪时，调用会明确失败，不会创建备用数据库，也不会改走 SSH。

`serverpilot-mcp` 只暴露五个工具：`gpu_status`、`gpu_apply`、`gpu_release`、`gpu_add_server`、`gpu_update_server`。这就是全部 MCP 表面。`gpu_add_server` 的 `observation_profile` 直接写在参数 schema 里，接受内置 `linux`（默认，任何可 SSH 到达的机器——探测自己判断上面有没有 NVIDIA 卡）或本机已发现的插件 ID（接入共享集群）。默认配置不需要 `enabled_tools` 白名单。服务器删除和其他生命周期操作走 App 或 REST。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

1. `gpu_status(server_id?, lease_id?)` 分三组回答三个问题，遥测只出现在占用归属于调用者的那一组。**连接与工作目录逐服务器返回一次**：`server_groups[]`（及需要时的 `ungrouped_servers[]`）里每台服务器给出 `server_id`、结构化 `ssh {host, port, user}`、结构化 `workspace` 与兼容字段 `workspace_path`；占用行只带 `server_id` 外键指回。先读组级 `workspace_path`、`environment_notes`（环境/数据/权重说明）和容量，再决定申请哪一组。
   - 可申请容量不再按空闲卡逐行列出，而是按**分组 → 服务器 → GPU 型号/显存**汇总：每台服务器的 `gpus[]` 只含 `name`、`vram_mib`、`total_count`、`available_count`，因此两台 4 卡机不会看起来像单机 8 卡。这里不返回遥测，也不返回 `keepalive` 或逐张 `gpu_id`：未租出的卡上能观测到的负载来自 ServerPilot 自己的占卡程序（固定按总显存的 80% 持有），分配前会先停掉，把这份读数给调用者，只会让一张空闲卡看起来是满的。对这一组，调用者唯一要决策的是选哪一组、申请几张，容量汇总足以支撑这个决策。
   - `busy_gpus[]` 是**别人占着的卡**：`gpu_id`、`index`、`status` 和人类可读 `task`，不含遥测——「谁占着」是可以据以决策的信息，「别人的任务把卡用得多满」不是。所以判断「有没有空卡、被谁占着」仍然只需要这一次调用。
   - 传入自己的 `lease_id` 时返回 `leased_gpus[]` 与 `lease` 汇总，这是**自己租到的卡**。此时卡上的每个进程都是调用者自己的负载，遥测才可解读：逐卡以 `recent_average`（近 10 分钟均值：显存已用/可用、`memory_used_pct`、GPU 与显存控制器利用率、温度）为主，`current` 是同一张卡的最近一次采集并保留 `observed_at`，用于区分样本缺失或过旧与真的负载很轻。`lease` 给出 `lease_id`、`task`、`gpu_count`、`telemetry_gpu_count`、`telemetry_window`、`recent_average`（含全租约的 `min_memory_free_mib`），多卡时另给 `gpu_utilization_spread_pct` 与 `slowest_gpu`。这几个数正对应调优决策：利用率持续偏低指向输入侧瓶颈，`min_memory_free_mib` 决定还能不能加 batch，`slowest_gpu` 暴露多卡任务里拖后腿的那张——把这两项平均掉恰好会盖住最值得处理的情况。时间窗描述对同一次采集的所有卡相同，因此放在 `lease.telemetry_window`；某张卡的窗口与其他卡不一致时才落到该卡的 `window_override`，绝不把部分失败的采集伪装成一个共享窗口。`lease_id` 已释放、被空闲回收或不在本次 `server_id` 范围内时返回 `no_leased_gpus` 并说明原因。

   这里的读数与 GUI 来自 daemon 同一份 REST 快照，不会另行 SSH 采集；GUI 的逐卡瞬时遥测另有 REST 投影，不受本路径约束。首次只读采集还会把 endpoint 标记为 GPU、纯 CPU 或尚未确认；确认是纯 CPU 的 endpoint 会在 `cpu_only_servers` 中返回服务器标识、在线状态、可用核数与内存；CPU/内存一律按该 endpoint 实际拥有的额度投影（容器有 cgroup 配额时用配额，否则才是整机容量），节点的核数与 MemTotal 不会被当成它自己的份额，但仅供说明，永不参与 GPU 分配。插件接入的集群（Slurm / LSF 等）**没有**平行的 `scheduler_servers` 顶层桶：登录节点以和裸机相同的 `{server_id, workspace_path, workspace, ssh, gpus[]}` 形状出现在所属 `server_groups[]` 里，组名和共享工作目录不会丢。未知的 `total_count` 会省略而不是填 `null`，该登录节点也不会同时出现在 `cpu_only_servers`。每个分组另带三个字段：

   - `allocation`：`direct`（本机逐卡分配）或 `delegated`（交给该组声明了 `apply` 的插件）。
   - `limits`：组级上限，形状对两种分配相同。键为 `max_gpus_per_lease`、`max_lease_seconds`、`lease_ends`、`cpu_cores_per_gpu`、`memory_mib_per_gpu`、`apply_max_seconds`、`queues`。裸机组从本机卡数推导，`lease_ends` 为 `on_release`，`apply_max_seconds` 由一次直连申请实际花掉的超时推导（停一台主机再采集一次）；委托组来自插件 `info.limits`，并可用采集里的 `scheduler` 容量补 `max_gpus_per_lease` / 每卡 CPU / 每卡内存。`apply_max_seconds` 是**服务端这次申请最多花多久**，两类分组含义相同：调用方据此决定等多久，不要按 `gpu_count` 估算——服务端的成本与卡数无关。
   - 租约遥测（`gpu_status(lease_id=…)`）只覆盖**你自己持有这张卡期间**的样本：窗口下界是本次租约的签发时刻，所以刚拿到手时 `recent_average` 与 `telemetry_window` 都是 `null`，那是正常状态而不是错误——此时读 `current` 拿这一刻的读数。不夹紧的那份仍描述这张卡本身，供 App 使用。
   - `largest_allocatable_block`：**一次 `gpu_apply` 能拿到的最大卡数**，不是池子里还剩多少张。裸机组取组内单台机器上当前可申请卡数的最大值（一份租约只落一台主机）。委托组取插件报告的 `largest_free_block`，否则在已知时取 `min(free_gpu_count, max_gpus_per_lease)`。一个跨节点还剩 27 张卡的分区，若作业必须落在单节点，很可能一张 8 卡都开不出来。未知时该字段为 `null`——此时不要向用户或 Agent 编造数字；`0` 表示组里真的一张都申请不到。

   `workspace` 固定表达「远端工作目录」语义：`{path, kind=working_directory, use_as_cwd=true, code_location=not_provided}`。没有 GPU 时返回「无 GPU」；有 GPU 但一张都申请不到时返回 `no_capacity`。
2. `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)` 直接申请。已分组主机（裸机 `direct` 和插件 `delegated` 都一样）传 `server_group_id`；不要用 `server_id` 去钉住已分组的主机——`server_id` 留给显式未分组兼容路径。`gpu_count` 必须来自启动脚本/配置里的任务并行度（`devices`、`--nproc_per_node`、`num_processes`、`--gres` 等），绝不能由服务器总卡数、空闲数或 `largest_allocatable_block` 反推；默认 `1` 是安全默认，多卡任务再显式写出准确数量。申请张数大于该组 `largest_allocatable_block` 时，这次申请拿不到（委托组在上限未知、字段为 `null` 时不要猜一个数再去碰）。`task` 用用户给定的任务名，或当前目标的简短人类可读概括，不读客户端 UI 标题；未提供时记为「未命名任务」。ServerPilot 自动选 GPU，Agent 不提供 GPU ID。对 `delegated` 分组：申请时 ServerPilot 在该组恰好一个声明了 `apply` 的成员上调用插件（Slurm 参考插件用 `srun --immediate`），再按插件回执里的真实节点 / UUID 认领，Agent 不感知这一步。**连接、工作目录和整份租约的 CUDA selector 同样逐服务器返回一次**，在 `servers[]` 中；单服务器租约会额外在顶层重复一份，那是平时最常读的路径。申请结果里的 `gpus[]` 每行只有 `server_id`、`gpu_id`、`gpu_index`、`cuda_ordinal` 和 `gpu_cuda_visible_devices`。四类信息必须分开理解：`ssh` 是连接参数；`workspace.path` 是 SSH 后先进入的远端 cwd；`code_location=not_provided` 表示 ServerPilot 不提供也不暗示代码仓库位置；GPU UUID 只作物理身份。执行时设置返回的 `CUDA_DEVICE_ORDER=PCI_BUS_ID`，再用该服务器的 `cuda_visible_devices` ordinal 集合选择整份租约，或用每行 `gpu_cuda_visible_devices` ordinal 选择对应一张卡；不得把 UUID 直接填入 `CUDA_VISIBLE_DEVICES`。跨 endpoint 的结果不生成有歧义的顶层 server/SSH/workspace/CUDA 字段，逐服务器信息一律从 `servers[]` 读取。随后做最小 CUDA 初始化检查，再启动工作负载。
3. 最小 CUDA 初始化检查失败或工作负载没能启动时，立即调用 `gpu_release(lease_id)`；它回显该租约的 `lease_id` 与终态 `state`，供逐个确认。在当前任务里记下失败的 `server_id` 和原因，后续申请避开同一环境。工作负载停止时同样要释放。

确认空闲的占卡 GPU 仍出现在可用容量里。`gpu_apply` 正式分配这张卡之前，会自动停掉该卡的占卡 worker、定向刷新并确认空闲，然后返回一份普通工作租约；Agent 不需要、也不能手工执行占卡停止流程。

新增服务器必须同时登记一个绝对路径的 `workspace_path`（例如 `/srv/serverpilot-workspace`）。历史 endpoint 没有这份元数据时，状态里明确返回空值，需要通过现有的 endpoint 更新接口补齐，ServerPilot 不会猜子项目路径。这个字段是远端操作的工作目录元数据，不是代码仓库路径：ServerPilot 不会自动创建或删除远端目录，也不会因此获得启动项目工作负载的权限。密封占卡 helper 只采用固定布局 `${workspace_path}/serverpilot-keepalive`；adapter 先在这个 workspace 下执行只读的 `./serverpilot-keepalive --protocol-info`，确认 v3 和所需能力后才执行固定的逐卡启停命令；身份恢复同样只读，执行 `./serverpilot-keepalive --inspect --schema-version 3`，并与最新的采集观测交叉确认。不从远端 `PATH` 查找，也不接受调用方提供的 PID、helper 路径、命令或参数。旧 v2 wire/state 一律 fail closed，不会收养、删除或停止旧状态。

`no_capacity` 和 `group_selection_required` 都是结构化返回，不是工具错误：`gpu_apply` 拿不到卡时返回 `{"no_capacity": {reason, message, gpu_count, server_id, server_group_id}}`；已分组 direct 主机未选组时返回 `{"group_selection_required": {reason, message, gpu_count, server_id, server_group_id, …details}}`，其中会带上各组路径、环境/同步说明和逐服务器 GPU 形状，且不创建请求或租约。`gpu_status` 在视野内一张可申请卡都没有时同样返回 `no_capacity`。这些都要当数据读，不要当成故障重试；只有 `Transport closed` 这类才是传输错误，最多重试一次。

`no_capacity` 不会排队，也不代表传输失败；此时 `busy_gpus` 已经列出了占用者，不必为了看占用再查一次。同一轮最多再刷新一次状态，下一次尝试留给下一轮或后续工作周期，不要在当前这轮里反复申请。`Transport closed` 与 `no_capacity` 分开处理：前者最多重试一次，再失败就报告传输错误。

例行租约一直持续到显式 `gpu_release`、App 人工处理或空闲回收。**只申请真正会用的卡**：只要这份租约里还有卡在跑任务，其中持续空闲的单张卡会被单独收回，仍在工作的卡不受影响——所以申请后请按返回的 `cuda_visible_devices` 用上全部卡，或者一开始只申请需要的数量。例行租约不需要 bind、renew、coordination，也不需要单独发心跳——`gpu_status(lease_id=…)` 本身就是心跳，控制面据此知道持有者还在，一份处在两批任务之间、所有卡上当前都没有进程的租约因此一张卡也不会被空闲回收拿走；也不需要调用方提供 `idempotency_key`：MCP adapter 为每次工具调用生成内部重放键，只在本地 HTTP 传输失败时用同一个键自动重试一次；参数相同的新调用仍会创建新租约。一个任务持有多份租约时，必须维护显式的 `lease_id` 清单，由申请者逐个释放并确认结果为 `released`，不能因为其中一份已释放就把整个任务视为完成。MCP 进程退出后，调用方另发的新调用不会被判定为旧调用的传输重放。

ServerPilot 只协调 GPU。申请成功后直接 SSH 上去跑工作负载是正常路径；被禁止的是通过 SSH、SQLite、静态服务器清单或 `nvidia-smi` 绕过 ServerPilot 去发现、指定、申请或释放 GPU。已登记且当前凭据可达的 endpoint 可以直接 SSH，不要求先加入 Codex saved hosts。非 GPU 的远端操作——Git 同步、文件维护、只读环境检查——不需要申请 GPU 租约；普通 SSH 操作本身不等于绕过 ServerPilot。

## 边界

- 服务器登记走 `gpu_add_server`（必填 `project_id`、`host`、`workspace_path`；分组主机同时给 `server_group_id`，不给就是未分组，`gpu_apply(server_group_id=...)` 永远选不到它）/ `gpu_update_server`（必填 `server_id`，其余安全元数据至少给一项）；其余生命周期与管理操作走 App 或 REST，不进入默认 Agent 上下文。登记会当场观测一次并在返回体的 `observation` 里给出结果（`observed` / `gpu_count` / `error`）：`observed=false` 说明记录建好了但机器没连上，原因在 `error` 里（例如 host key 未知），修好后下一轮采集自己接上，不要重复登记。两者的重放键由工具自己生成，调用方不传；重复登记同一台机器由 `endpoint_exists` / `endpoint_address_exists` 两条 409 挡住，拿到它们就改用 `gpu_update_server`。
- App 负责人工查看和纠错；默认 Agent 路径不需要额外生命周期步骤。
- ServerPilot 返回 SSH 连接参数但不提供密码、私钥或 shell；它复用当前用户已有凭据，也不代替项目自己的远端执行授权。

## 自检

```bash
serverpilot daemon status --json
serverpilot --help
python3 scripts/install_agent_policy.py all --print
```
