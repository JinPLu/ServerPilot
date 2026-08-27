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

`serverpilot-mcp` 只暴露五个工具：`gpu_status`、`gpu_apply`、`gpu_release`、`gpu_add_server`、`gpu_update_server`。这就是全部 MCP 表面。`gpu_add_server` 的 `observation_profile` 直接写在参数 schema 里，接受内置 `linux-nvidia`、`linux-host`、`server-script-v1`，或本机已发现的插件 ID。默认配置不需要 `enabled_tools` 白名单。服务器删除和其他生命周期操作走 App 或 REST。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

1. `gpu_status(server_id?, lease_id?)` 分三组回答三个问题，遥测只出现在占用归属于调用者的那一组。**连接与工作目录逐服务器返回一次**：`server_groups[]`（及需要时的 `ungrouped_servers[]`）里每台服务器给出 `server_id`、结构化 `ssh {host, port, user}`、结构化 `workspace` 与兼容字段 `workspace_path`；占用行只带 `server_id` 外键指回。先读组级 `workspace_path`、`environment_notes`（环境/数据/权重说明）和容量，再决定申请哪一组。
   - 可申请容量不再按空闲卡逐行列出，而是按**分组 → 服务器 → GPU 型号/显存**汇总：每台服务器的 `gpus[]` 只含 `name`、`vram_mib`、`total_count`、`available_count`，因此两台 4 卡机不会看起来像单机 8 卡。这里不返回遥测，也不返回 `keepalive` 或逐张 `gpu_id`：未租出的卡上能观测到的负载来自 ServerPilot 自己的占卡程序（固定按总显存的 80% 持有），分配前会先停掉，把这份读数给调用者，只会让一张空闲卡看起来是满的。对这一组，调用者唯一要决策的是选哪一组、申请几张，容量汇总足以支撑这个决策。
   - `busy_gpus[]` 是**别人占着的卡**：`gpu_id`、`index`、`status` 和人类可读 `task`，不含遥测——「谁占着」是可以据以决策的信息，「别人的任务把卡用得多满」不是。所以判断「有没有空卡、被谁占着」仍然只需要这一次调用。
   - 传入自己的 `lease_id` 时返回 `leased_gpus[]` 与 `lease` 汇总，这是**自己租到的卡**。此时卡上的每个进程都是调用者自己的负载，遥测才可解读：逐卡以 `recent_average`（近 10 分钟均值：显存已用/可用、`memory_used_pct`、GPU 与显存控制器利用率、温度）为主，`current` 是同一张卡的最近一次采集并保留 `observed_at`，用于区分样本缺失或过旧与真的负载很轻。`lease` 给出 `lease_id`、`task`、`gpu_count`、`telemetry_gpu_count`、`telemetry_window`、`recent_average`（含全租约的 `min_memory_free_mib`），多卡时另给 `gpu_utilization_spread_pct` 与 `slowest_gpu`。这几个数正对应调优决策：利用率持续偏低指向输入侧瓶颈，`min_memory_free_mib` 决定还能不能加 batch，`slowest_gpu` 暴露多卡任务里拖后腿的那张——把这两项平均掉恰好会盖住最值得处理的情况。时间窗描述对同一次采集的所有卡相同，因此放在 `lease.telemetry_window`；某张卡的窗口与其他卡不一致时才落到该卡的 `window_override`，绝不把部分失败的采集伪装成一个共享窗口。`lease_id` 已释放、被空闲回收或不在本次 `server_id` 范围内时返回 `no_leased_gpus` 并说明原因。

   这里的读数与 GUI 来自 daemon 同一份 REST 快照，不会另行 SSH 采集；GUI 的逐卡瞬时遥测另有 REST 投影，不受本路径约束。首次只读采集还会把 endpoint 标记为 GPU、纯 CPU 或尚未确认；确认是纯 CPU 的 endpoint 会在 `cpu_only_servers` 中返回服务器标识、在线状态、CPU 数和可用内存，但仅供说明，永不参与 GPU 分配。**共享调度器集群**（Slurm / LSF 等）未入账时出现在 `scheduler_servers`，只带 `free_gpu_count`、`gpu_name` 和「按需申请，不排队」——这是容量提示，不是账本里的卡；对它调用 `gpu_apply(server_id=…)` 才真正入账。`workspace` 固定表达「远端工作目录」语义：`{path, kind=working_directory, use_as_cwd=true, code_location=not_provided}`。没有 GPU 时返回「无 GPU」；有 GPU 但一张都申请不到时返回 `no_capacity`。
2. `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)` 直接申请。已分组的 direct 主机必须传 `server_group_id`，由 ServerPilot 在组内 best-fit；不要用 `server_id` 去钉住已分组的 direct 主机——`server_id` 留给 scheduler/plugin 或显式未分组兼容路径。`gpu_count` 必须来自启动脚本/配置里的任务并行度（`devices`、`--nproc_per_node`、`num_processes`、`--gres` 等），绝不能由服务器总卡数或空闲数反推；默认 `1` 是安全默认，多卡任务再显式写出准确数量。`task` 用用户给定的任务名，或当前目标的简短人类可读概括，不读客户端 UI 标题；未提供时记为「未命名任务」。ServerPilot 自动选 GPU，Agent 不提供 GPU ID。对调度器集群：如果账本里还没有卡，申请时 ServerPilot 会先调用该 endpoint 插件的 `apply`（Slurm 参考插件用 `srun --immediate`），再按插件回执里的真实节点 / UUID 认领，Agent 不感知这一步。**连接、工作目录和整份租约的 CUDA selector 同样逐服务器返回一次**，在 `servers[]` 中；单服务器租约会额外在顶层重复一份，那是平时最常读的路径。申请结果里的 `gpus[]` 每行只有 `server_id`、`gpu_id`、`gpu_index`、`cuda_ordinal` 和 `gpu_cuda_visible_devices`。四类信息必须分开理解：`ssh` 是连接参数；`workspace.path` 是 SSH 后先进入的远端 cwd；`code_location=not_provided` 表示 ServerPilot 不提供也不暗示代码仓库位置；GPU UUID 只作物理身份。执行时设置返回的 `CUDA_DEVICE_ORDER=PCI_BUS_ID`，再用该服务器的 `cuda_visible_devices` ordinal 集合选择整份租约，或用每行 `gpu_cuda_visible_devices` ordinal 选择对应一张卡；不得把 UUID 直接填入 `CUDA_VISIBLE_DEVICES`。跨 endpoint 的结果不生成有歧义的顶层 server/SSH/workspace/CUDA 字段，逐服务器信息一律从 `servers[]` 读取。随后做最小 CUDA 初始化检查，再启动工作负载。
3. 最小 CUDA 初始化检查失败或工作负载没能启动时，立即调用 `gpu_release(lease_id)`；它回显该租约的 `lease_id` 与终态 `state`，供逐个确认。在当前任务里记下失败的 `server_id` 和原因，后续申请避开同一环境。工作负载停止时同样要释放。

确认空闲的占卡 GPU 仍出现在可用容量里。`gpu_apply` 正式分配这张卡之前，会自动停掉该卡的占卡 worker、定向刷新并确认空闲，然后返回一份普通工作租约；Agent 不需要、也不能手工执行占卡停止流程。

新增服务器必须同时登记一个绝对路径的 `workspace_path`（例如 `/srv/serverpilot-workspace`）。历史 endpoint 没有这份元数据时，状态里明确返回空值，需要通过现有的 endpoint 更新接口补齐，ServerPilot 不会猜子项目路径。这个字段是远端操作的工作目录元数据，不是代码仓库路径：ServerPilot 不会自动创建或删除远端目录，也不会因此获得启动项目工作负载的权限。密封占卡 helper 只采用固定布局 `${workspace_path}/serverpilot-keepalive`；adapter 先在这个 workspace 下执行只读的 `./serverpilot-keepalive --protocol-info`，确认 v3 和所需能力后才执行固定的逐卡启停命令；身份恢复同样只读，执行 `./serverpilot-keepalive --inspect --schema-version 3`，并与最新的采集观测交叉确认。不从远端 `PATH` 查找，也不接受调用方提供的 PID、helper 路径、命令或参数。旧 v2 wire/state 一律 fail closed，不会收养、删除或停止旧状态。

`no_capacity` 和 `group_selection_required` 都是结构化返回，不是工具错误：`gpu_apply` 拿不到卡时返回 `{"no_capacity": {reason, message, gpu_count, server_id, server_group_id}}`；已分组 direct 主机未选组时返回 `{"group_selection_required": {reason, message, gpu_count, server_id, server_group_id, …details}}`，其中会带上各组路径、环境/同步说明和逐服务器 GPU 形状，且不创建请求或租约。`gpu_status` 在视野内一张可申请卡都没有时同样返回 `no_capacity`。这些都要当数据读，不要当成故障重试；只有 `Transport closed` 这类才是传输错误，最多重试一次。

`no_capacity` 不会排队，也不代表传输失败；此时 `busy_gpus` 已经列出了占用者，不必为了看占用再查一次。同一轮最多再刷新一次状态，下一次尝试留给下一轮或后续工作周期，不要在当前这轮里反复申请。`Transport closed` 与 `no_capacity` 分开处理：前者最多重试一次，再失败就报告传输错误。

例行租约一直持续到显式 `gpu_release`、App 人工处理或空闲回收。**只申请真正会用的卡**：租约里持续空闲的单张卡会被单独收回，其余仍在工作的卡不受影响——所以申请后请按返回的 `cuda_visible_devices` 用上全部卡，或者一开始只申请需要的数量。例行租约不需要 bind、renew、heartbeat、coordination，也不需要调用方提供 `idempotency_key`：MCP adapter 为每次工具调用生成内部重放键，只在本地 HTTP 传输失败时用同一个键自动重试一次；参数相同的新调用仍会创建新租约。一个任务持有多份租约时，必须维护显式的 `lease_id` 清单，由申请者逐个释放并确认结果为 `released`，不能因为其中一份已释放就把整个任务视为完成。MCP 进程退出后，调用方另发的新调用不会被判定为旧调用的传输重放。

ServerPilot 只协调 GPU。申请成功后直接 SSH 上去跑工作负载是正常路径；被禁止的是通过 SSH、SQLite、静态服务器清单或 `nvidia-smi` 绕过 ServerPilot 去发现、指定、申请或释放 GPU。已登记且当前凭据可达的 endpoint 可以直接 SSH，不要求先加入 Codex saved hosts。非 GPU 的远端操作——Git 同步、文件维护、只读环境检查——不需要申请 GPU 租约；普通 SSH 操作本身不等于绕过 ServerPilot。

## 边界

- 服务器登记走 `gpu_add_server` / `gpu_update_server`；其余生命周期与管理操作走 App 或 REST，不进入默认 Agent 上下文。
- App 负责人工查看和纠错；默认 Agent 路径不需要额外生命周期步骤。
- ServerPilot 返回 SSH 连接参数但不提供密码、私钥或 shell；它复用当前用户已有凭据，也不代替项目自己的远端执行授权。

## 自检

```bash
serverpilot daemon status --json
serverpilot --help
python3 scripts/install_agent_policy.py all --print
```
