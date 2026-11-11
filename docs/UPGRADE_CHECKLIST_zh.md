# 升级检查清单

## 本机完整更新入口

先将源码检出同步到本次要运行的内容，然后在仓库根目录执行：

```bash
zsh desktop/update-macos-app.sh
```

需要 macOS、Python 3.12、uv 和 macOS Swift 工具链。这是本机 App 与后端的统一更新入口；`build-macos-app.sh` 和 `verify-macos-app.sh` 保留给单独构建、校验和 CI。

完整更新会原地替换根目录唯一的 `ServerPilot.app`，重装 `~/.local/share/uv/tools/serverpilot` 的正式后端，显式重启归属本用户的 daemon，并退出、重新打开根目录 App。校验同时检查源码、安装包和运行进程的版本及内容构建标识，以及 App 的新进程、标准路径和唯一实例。即使语义版本号没变，也要确认新代码已加载；任一步失败都不能报告更新成功。

经 bundle 身份确认的旧 App 副本会被清理；身份不明的同名 App 会使流程失败，避免误删。现有数据库、inventory、租约和 GPU workload 保留。流程只部署当前检出内容，Git 拉取、提交、版本号和 tag 发布另行执行。

后端从干净的临时源码目录打包，不复用仓库里的旧 `build/lib` 或 egg-info；临时目录不复制 App。构建期间即使 App 被重新打开，末尾也会先正常退出该进程，再启动并验证唯一的新实例。失败会说明停在哪一步，修复原因后重新执行同一命令；不会悄悄回退到旧版并报告成功。

本机更新入口不重启外部 MCP 客户端，也不向 GPU 服务器分发 helper；下面保留这些项目的后续检查。

## 升级到 4.0

4.0 起 MCP 只有三个工具，不保留旧工具，升级后核对以下项目：

1. 重启 daemon，确认 `/health/live` 的 `version` 是新版本。迁移保留全部数据和活跃租约，并重置它们的心跳与空闲计时。
2. 重启每个 MCP 客户端。旧客户端调用时会收到 `restart_client`。
3. 运行 `serverpilot mcp policy --install` 更新全局规则。
4. 想让 Claude Code 的 Agent 无需确认地申请和归还 GPU，运行 `serverpilot mcp install --client claude --allow-tools`。

升级前已有的租约显示在 `gpu_status` 的 `recoverable` 里，Agent 用 `gpu_apply(task=..., take_over=true)` 认领。


包换了、源码对了，不等于现场在跑新契约。本机 App 和后端由上面的完整更新入口负责；MCP 客户端缓存和远端 helper 仍需分别核对。

升级后按顺序核对。每一项都写了「怎么确认」和「漏做会怎样」。

## 1. 确认本机控制面进程换了版本

`uv tool install --force .` 只换磁盘上的包。macOS 上 daemon 由 LaunchAgent 拉起，进程不会因为包装好了就重载内存里的模块。同一语义版本号也可以仍在跑旧代码。

本机更新使用完整更新入口。需要排查运行状态时：

```bash
serverpilot --version
serverpilot daemon status --json
curl --noproxy '*' -s http://127.0.0.1:8787/health/live
```

`daemon status --json` 已报告 `installed_version`、`running_version` 及 installed / loaded / live / ready 和数据路径。`/health/live` 的 `version` 必须和安装版一致，`build_id` 则是该进程启动时冻结的代码内容标识。版本号相同不足以证明补丁已加载，完整更新入口还会比对构建标识并确认进程已重启。

漏做：CLI 已经是新版本，8787 上仍是旧控制面。MCP 工具数、插件 schema、分组投影都会按旧进程回答。

## 2. 重连 MCP 客户端，强制重新 `tools/list`

`serverpilot mcp install` **只写启动命令**（Codex / Claude 的 `mcp add`，或 Cursor 的 `~/.cursor/mcp.json`）。它不刷新客户端已经缓存的工具 schema。

Cursor：对该 server Disable → Enable，或重载窗口。其它客户端同样要让 MCP 连接重建一次。

漏做：客户端仍按旧 schema 调工具。现场出现过源码已是 5 个工具（`gpu_status` / `gpu_apply` / `gpu_release` / `gpu_add_server` / `gpu_update_server`），客户端仍按旧的 3 工具 schema 调用。

## 3. 重装 Agent 全局规则

```bash
serverpilot mcp policy --install
```

Cursor 不能写盘：只能 `--print`，再手工粘贴到 User Rules。Codex / Claude Code 的 `--install` 会更新它们的全局 Markdown。

漏做：Agent 仍按旧规则理解工具和分组，例如继续找已经取消的 `scheduler_servers`，或用空闲容量反推 `gpu_count`。

## 4. GPU 服务器无需任何安装

被观测主机上不需要装 ServerPilot，也没有需要跟控制面对齐的采集入口：观测是一条固定的
只读 SSH 探测。升级控制面不需要动任何一台 GPU 服务器。

要确认一台主机可被观测，只要确认非交互 SSH 能连上、而且 GPU 主机上 `nvidia-smi` 在那条
`PATH` 上（登录 shell 的 PATH 不算）：

```bash
ssh user@host true
ssh user@host nvidia-smi -L
```

第二条在纯 CPU 主机上没有输出是正常的，这类主机会按 CPU-only 登记，不参与 GPU 分配。

## 5. 单独部署占卡 helper

占卡不走 PATH。adapter 只执行工作目录里的固定入口 `${workspace_path}/serverpilot-keepalive`（也就是 `./serverpilot-keepalive`）。仓库没有把 helper 自动分发到远端的命令。

在每台要开占卡的服务器上，把与控制面同版本的 helper 放到该机生效工作目录下，并确认：

```bash
ssh user@host 'cd -- <workspace_path> && ./serverpilot-keepalive --protocol-info'
```

输出必须是 `kind=serverpilot-keepalive`、`schema_version=3`，并含 `per_gpu_keepalive`、`pidfd_identity`、`pci_bus_id`、`worker_attestation`。`implementation_version` 等于构建它的 ServerPilot 包装版本；预检不按这个数字做兼容判断，但现场应用和本机包装版本对齐。

要让「占卡负载」生效，`capabilities` 里还要有 `target_utilization`。没有它的旧 helper 照常占卡、停卡，只是忽略负载设置，按旧的固定节奏运行。升级 helper 后，已经在跑的 worker 仍是旧节奏，在 App 里改一次占卡负载（或结束再开始占卡）才会按新负载重启。

漏做：预检失败，返回 `keepalive_helper_incompatible`，不会发送任何变更。预检连不上（SSH 失败或超时）返回的是 `keepalive_helper_unreachable`，那是网络问题，不是版本问题。占卡开关在 App 里看起来能点，远端什么都不会启动。旧 v2 helper / 旧 state 直接拒绝，不会被收养。

## 6. 本机插件升到 v5

当前发现契约是 `schema_version: 5`，且 `info` 必须声明合法 `limits`。详见 [OBSERVATION_zh.md](OBSERVATION_zh.md)。

```bash
serverpilot plugin list
```

列表里必须能看到你依赖的插件。随包 `slurm-immediate` 已是 v5；用户目录里自己放的插件要自己改。

漏做：旧协议或 `limits` 不合法的插件在发现阶段**静默消失**，`plugin list` 看不到，也不报错。对应集群从 `gpu_status` 里不见了，人容易以为「没装上」或「集群没容量」。`plugin add` 会当场拒绝；已经躺在插件目录里的旧文件不会。
