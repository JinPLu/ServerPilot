<p align="center">
  <img src="desktop/assets/ServerPilot%20Icon.png" width="96" alt="ServerPilot icon">
</p>

<h1 align="center">ServerPilot</h1>

<p align="center">
  <strong>Agent 自己拿卡，人类实时监控。</strong><br>
  给 Agent 的 MCP · 原生桌面 App（macOS）· 开源
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="#-核心功能">核心功能</a> ·
  <a href="#-快速开始">快速开始</a> ·
  <a href="#-agent-用法">Agent 用法</a> ·
  <a href="#-边界与安全">边界与安全</a> ·
  <a href="#-文档">文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12%2B-2563EB?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/macOS-native%20App-111827?logo=apple&logoColor=white" alt="Native macOS App">
  <img src="https://img.shields.io/badge/MCP-5%20routine%20tools-7C3AED" alt="Five routine MCP tools">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-334155" alt="MIT License"></a>
</p>

<p align="center">
  <img src="docs/assets/serverpilot-workflow-cartoon.png" width="960" alt="ServerPilot：将零散 GPU 汇总为可供 Agent 协作的资源池，并由人类监控">
</p>

<p align="center"><sub>概念示意图，不代表真实资源状态或 App 页面。</sub></p>

> 🤖 Agent 会写代码、跑实验了，GPU 还需要一张张指定吗？

对 Agent，ServerPilot 是一个 MCP：查卡、申请、归还，以及登记或更新主机。对人，它是一个 macOS App：看多台服务器的空闲、占用、任务和异常。CLI 和 MCP 在 Windows 上同样可用，桌面 App 仅 macOS。没有浏览器界面。

一个本机用户，管理多台服务器与协作 Agent；资源状态、申请和人工纠错始终围绕同一份本机控制面快照。

| 核心价值 | ServerPilot 提供什么 |
| --- | --- |
| 统一资源事实 | GUI、CLI 和 MCP 共用 daemon 的已提交快照，不再分别推算或采集状态。 |
| Agent 三步闭环 | Agent 通过 `gpu_status → gpu_apply → gpu_release` 完成日常 GPU 协调。 |
| 人类实时监控 | 人可以查看服务器、任务、归属和异常，并在需要时执行受限纠错。 |
| 逐卡空闲占卡 | 可选的占卡功能只让出被申请的卡，用后按策略恢复待命，不影响同机其他卡。 |

## ✨ 核心功能

### 🛰️ 人类实时监控：多台服务器，一张图

按设置的采集间隔更新服务器状态；GPU 空闲、占用、任务归属和采集异常，都在 App 里直接看。服务器可以放进一等分组：分组携带共享工作目录、环境说明和数据/权重说明；成员继承该工作目录，或按服务器覆盖。环境说明只供阅读，不会被执行或注入。可申请容量按 组 → 服务器 → SKU 展示，不是逐张空闲卡菜单。

### 🧩 Agent 自主调度：有空卡，Agent 自己领

MCP 正好五个工具：

- 🔍 `gpu_status`：查看分组后的可申请容量、忙卡归属，以及自己已持有卡的遥测
- 🔑 `gpu_apply`：申请 GPU（`server_group_id?`、`server_id?`、`gpu_count=1`、`task?`）
- ♻️ `gpu_release`：明确归还
- ➕ `gpu_add_server`：登记一台主机
- ✏️ `gpu_update_server`：更新安全的主机元数据

申请成功后，Agent 会拿到 SSH 连接、远端工作目录、CUDA selector 和 `lease_id`，不用再猜服务器、目录或 GPU 编号。删除服务器只在 App 和 REST，不是 MCP 工具；该服务器还有进行中租约时会拒绝。

### 🛠️ 人类纠错反馈：平时不打扰，需要时再纠正

Agent 正常干活时，人只看全局。发现归属不对、连接异常或遗留占用，再人工确认后纠正：可以处理遗留租约，也可以在保持卡数不变的前提下改派 GPU。

### 🐶 一个小彩蛋：空闲 GPU，先占着（可选）

逐卡占卡可以把确认空闲的 GPU 保持在待命状态。Agent 申请时只让出目标卡，归还后恢复待命，不影响同机其他 GPU。

只用于你有管理权限的服务器，不碰未知或他人正在运行的任务。

占卡 worker 以独立会话启动（`start_new_session=True`），与控制面分离。控制面停止后，这些卡会一直被占着，直到控制面恢复后 reconcile，或有人手动停掉它们；控制面退出时不会自动释放。

控制面不可用时，可以用这三条命令把状态恢复回来，它们都不依赖 daemon 在线：

```bash
serverpilot keepalive inspect --endpoint <server-id>   # 看远端在跑哪些占卡 worker
serverpilot keepalive stop --endpoint <server-id>      # 停掉它们，放开这些卡
serverpilot daemon reclaim                             # 端口被非 launchd 进程占住时交还
```

`daemon reclaim` 只处理「有 ServerPilot 在应答 8787、但不归 launchd 管」这一种情况；daemon 归属正常时它什么都不做。端口归属校验失败的提示会指出占用进程的 PID 和命令行。

## 🚀 快速开始

从源码启动需要 [Python 3.12+](https://www.python.org/)、[uv](https://docs.astral.sh/uv/) 和 macOS 或 Windows。桌面 App 仅 macOS；Windows 上使用 CLI 和 MCP。

### 1. 🧰 启动本机控制面

macOS 上 **CLI 就是后端**。`uv tool install` 装的是 daemon 实际会跑的那份控制面；桌面 App 只是界面，打开 App 不会启动或替换这个进程。

**macOS**，从源码安装，需要 [Python 3.12+](https://www.python.org/) 和 [uv](https://docs.astral.sh/uv/)：

```bash
git clone https://github.com/JinPLu/ServerPilot.git
cd ServerPilot
uv tool install --force .
serverpilot daemon install --source-root "$PWD"
serverpilot daemon status
```

`daemon install` 注册用户 LaunchAgent，由它拉起这份 `uv tool` 安装，且只在 macOS 上可用。以后升级包装后，还要确认 `http://127.0.0.1:8787/health/live` 里的进程版本确实换了，见[升级检查清单](docs/UPGRADE_CHECKLIST_zh.md)。

**Windows**：桌面 App 仅 macOS，但 CLI 和 MCP 入口在 Windows 上同样可用。从源码跑，使用 `serverpilot serve --db <path> --inventory <path>` 并保持该进程在线；Windows 上还没有受监管的 daemon 安装。

无论哪边，服务都监听 `http://127.0.0.1:8787`。

### 2. 🖥️ 登记你的 GPU 服务器

普通服务器 / 自建节点用内置的 `linux` 观测 profile：远端不需要安装任何东西，直接在 App 里登记 SSH 连接和一个绝对远端工作目录即可。最新一次采集成功后，GPU 才进入可申请状态。详细契约见[观测与占卡](docs/OBSERVATION_zh.md)。

共享集群（Slurm / LSF / PBS）不要按裸机接入：用本机插件接管观测，只登记当前用户自己的作业，申请走该插件的 `apply` / `release`。随包参考插件是 `slurm-immediate`，没有另一套调度器提交面。接入方式见[观测与占卡](docs/OBSERVATION_zh.md)。

### 3. 🤖 接入 Agent

```bash
serverpilot mcp install --client codex     # 或 claude、cursor
serverpilot mcp policy --install --client codex
```

`serverpilot mcp install` 只写启动命令：Codex 与 Claude Code 调用它们的 `mcp add`，Cursor 合并进 `~/.cursor/mcp.json`，不会覆盖你已有的其他 server。它不刷新客户端已经缓存的工具列表；要重连该 server（Cursor：Disable → Enable，或重载窗口）才会重新 `tools/list`。

想自己粘贴配置就用 `serverpilot mcp config --client all`，它只打印不写盘。标准配置块是：

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

CLI 和 MCP 入口在 Windows 上用法相同；桌面 App 仅 macOS。三个客户端的完整说明见 [Agent / MCP 指南](docs/AGENT_MCP_zh.md)。

## 🤖 Agent 用法

```text
gpu_status → gpu_apply(server_group_id=<分组>, gpu_count=<启动配置>, task="任务名") → 使用返回的分配 → gpu_release(lease_id)
```

- 日常申请签名是 `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)`。Agent 不传 GPU ID，`gpu_apply` 负责选卡，一份租约始终只落在一台机器上。`gpu_count` 来自启动脚本或配置中的任务并行度，安全默认 1，绝不从空闲容量推断。
- 已分组主机（裸机 `direct` 和插件 `delegated`）传 `server_group_id`，再由分配器在组内 best-fit；`server_id` 只留给未分组主机。插件接入的集群以正常分组出现，带 `allocation`、`limits` 和 `largest_allocatable_block`（一次申请能拿到的最大卡数，不是池子剩余总数；`null` 表示未知，不要编造数字）。
- 登记和更新主机用 `gpu_add_server`、`gpu_update_server`。MCP 不提供删除；在 App 或 REST 移除服务器，有进行中租约时会拒绝。
- 申请前先看分组的工作目录、环境说明和数据/权重说明。成员继承组工作目录，或按服务器覆盖。环境说明只供阅读，不会被执行或注入。
- `gpu_status` 按 组 → 服务器 → SKU 讲可申请容量（`name`、`vram_mib`、`total_count`、`available_count`），不是逐张空闲卡菜单，也不带遥测：空闲卡上能看到的负载来自 ServerPilot 自己的占卡程序，分配前会被停掉，不能据此认为这张卡被占用。遥测跟着租约走——`gpu_status(lease_id=…)` 返回 `leased_gpus` 的近 10 分钟均值和 `lease` 汇总（`min_memory_free_mib`、`slowest_gpu`），用来判断自己的任务有没有把卡用好。GUI 与 MCP 读的都是 daemon 同一份 REST 快照，不会重复 SSH 采集；GUI 的逐卡瞬时遥测另有 REST 投影。首次只读采集会把 endpoint 标记为 GPU、纯 CPU 或尚未确认；纯 CPU 服务器保留 CPU/内存监控，会在 `cpu_only_servers` 里列出供参考，但不参与 GPU 分配；这里和 GUI 的 CPU/内存都按该服务器实际拥有的额度算——容器上是 cgroup 配额，不是宿主机的核数和 MemTotal。
- SSH 后先进入返回的 `workspace.path`，再使用 CUDA selector。`workspace.path` 是远端工作目录，不是代码仓库。
- CUDA 初始化或工作负载启动失败时，立即 `gpu_release`。
- `no_capacity` 表示不分配、不排队；不要在同一轮反复申请。

## 🖥️ 打开桌面 App

桌面 App 仅 macOS；CLI 和 MCP 入口在 Windows 上同样可用。设置页会显示本机 MCP 入口的绝对路径和可直接粘贴的 `mcpServers` JSON。复制后即可交给 Codex、Claude Code 或 Cursor。找不到可执行文件时，同一处会说明原因并给出安装提示，而不会编造路径。

### macOS

先装 CLI——那才是后端。然后打开桌面 App 看状态、归属和异常。App 不自己跑控制面。

```bash
open "./ServerPilot.app"
```

没有浏览器界面。人工改派只更新租约和 CUDA selector，不迁移正在运行的进程；受影响的 Agent 要按新 selector 重启工作负载。

## 🛡️ 边界与安全

- ServerPilot 管理自己的占卡进程和插件侧分配，不启动、停止、迁移或抢占你的工作负载。占卡功能会在远端启停每卡一个 CUDA 占卡进程（约占该卡 80% 显存）；`gpu_apply` 交出该卡前会先停掉它。声明了 `apply` / `release` 的插件会在申请、归还时执行对应的分配操作。
- 服务器状态来自固定采集：内置 `linux` 探针（固定只读查询，远端不需要安装任何东西），或本机插件的 `observe`。插件调用契约是四个固定动词 `info`、`observe`、`apply`、`release`。不接收任意远端命令，也不提供密码或私钥。契约见 [观测与占卡](docs/OBSERVATION_zh.md)。
- 采集过期、连接异常、未知进程或资源冲突时，本机校验一律拒绝分配（fail closed）。这只对采集**报告的事实**成立：SSH 用户属于受信前提，委托型集群还额外信任插件可执行文件。插件若被替换或本身恶意，漏报了计算进程，该卡就会被判为可申请。
- 控制面默认只监听本机 loopback。没有认证：`X-ServerPilot-Actor` 只是审计标签，任意本机进程都可以带上这个头，以 `allocator` 角色创建 endpoint、改占卡策略、申请 GPU，甚至冒充其他 actor 释放其租约。同一用户账户下的本机进程互不隔离，都相当于操作者本人。GPU UUID 与 endpoint 是资源身份边界。

## 📚 文档

- [Agent / MCP 指南](docs/AGENT_MCP_zh.md)
- [观测与占卡](docs/OBSERVATION_zh.md)
- [升级检查清单](docs/UPGRADE_CHECKLIST_zh.md)
- [当前实现与验证状态](docs/IMPLEMENTATION_STATUS_zh.md)
- [安全说明](SECURITY.md) · [贡献指南](CONTRIBUTING.md)

## License

[MIT](LICENSE)
