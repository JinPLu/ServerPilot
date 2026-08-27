# 升级检查清单

包换了、源码对了，不等于现场在跑新契约。下面这些步骤**没有自动入口**；漏做时通常不报升级失败，只是旧进程、旧客户端缓存或旧远端入口继续工作。

升级后按顺序核对。每一项都写了「怎么确认」和「漏做会怎样」。

## 1. 确认本机控制面进程换了版本

`uv tool install --force .` 只换磁盘上的包。macOS 上 daemon 由 LaunchAgent 拉起，进程不会因为包装好了就重载内存里的模块。同一语义版本号也可以仍在跑旧代码。

怎么做：

```bash
serverpilot --version
serverpilot daemon stop
serverpilot daemon start
# 或：serverpilot daemon ensure --source-root "$PWD"
curl -s http://127.0.0.1:8787/health/live
```

`/health/live` 里的 `version` 必须和 `serverpilot --version` 一致。`serverpilot daemon status` 只报告 installed / loaded / live / ready 和数据路径，**不含版本号**，不能用来判断进程是否换过。

漏做：CLI 已经是新版本，8787 上仍是旧控制面。MCP 工具数、插件 schema、分组投影都会按旧进程回答。

## 2. 重连 MCP 客户端，强制重新 `tools/list`

`serverpilot mcp install` **只写启动命令**（Codex / Claude 的 `mcp add`，或 Cursor 的 `~/.cursor/mcp.json`）。它不刷新客户端已经缓存的工具 schema。

Cursor：对该 server Disable → Enable，或重载窗口。其它客户端同样要让 MCP 连接重建一次。

漏做：客户端仍按旧 schema 调工具。现场出现过源码已是 5 个工具（`gpu_status` / `gpu_apply` / `gpu_release` / `gpu_add_server` / `gpu_update_server`），客户端仍按旧的 3 工具 schema 调用。

## 3. 重装 Agent 全局规则

```bash
python3 scripts/install_agent_policy.py all --install
```

Cursor 不能写盘：只能 `--print`，再手工粘贴到 User Rules。Codex / Claude Code 的 `--install` 会更新它们的全局 Markdown。

漏做：Agent 仍按旧规则理解工具和分组，例如继续找已经取消的 `scheduler_servers`，或用空闲容量反推 `gpu_count`。

## 4. 每台 GPU 服务器重装同版本 `serverpilot-collect`

控制面版本必须和每台被观测主机上的采集入口一致。入口必须在**非交互 SSH** 的 `PATH` 上，不是你手工登录时的 PATH。

```bash
# 本机
serverpilot --version

# 每台 GPU 服务器，装同一版本，然后：
ssh user@host serverpilot-collect --schema-version 2
```

第二条必须只打印一行 JSON。登录 shell 里能跑、非交互 SSH 失败，是最常见的假象。

漏做：采集失败或仍走旧入口。控制面按 fail closed 停掉该机分配，看起来像「服务器没了」而不是「没升级远端」。

## 5. 单独部署占卡 helper

占卡不走 PATH。adapter 只执行工作目录里的固定入口 `${workspace_path}/serverpilot-keepalive`（也就是 `./serverpilot-keepalive`）。仓库没有把 helper 自动分发到远端的命令。

在每台要开占卡的服务器上，把与控制面同版本的 helper 放到该机生效工作目录下，并确认：

```bash
ssh user@host 'cd -- <workspace_path> && ./serverpilot-keepalive --protocol-info'
```

输出必须是 `kind=serverpilot-keepalive`、`schema_version=3`，并含 `per_gpu_keepalive`、`pidfd_identity`、`pci_bus_id`、`worker_attestation`。`implementation_version` 等于构建它的 ServerPilot 包装版本；预检不按这个数字做兼容判断，但现场应用和本机包装版本对齐。

漏做：预检失败，返回 `keepalive_helper_incompatible`，不会发送任何变更。占卡开关在 App 里看起来能点，远端什么都不会启动。旧 v2 helper / 旧 state 直接拒绝，不会被收养。

## 6. 本机插件升到 v3

当前发现契约是 `schema_version: 3`，且 `info` 必须声明合法 `limits`。详见 [PLUGINS_zh.md](PLUGINS_zh.md)。

```bash
serverpilot plugin list
```

列表里必须能看到你依赖的插件。随包 `slurm-immediate` 已是 v3；用户目录里自己放的插件要自己改。

漏做：v2 或 `limits` 不合法的插件在发现阶段**静默消失**，`plugin list` 看不到，也不报错。对应集群从 `gpu_status` 里不见了，人容易以为「没装上」或「集群没容量」。`plugin add` 会当场拒绝；已经躺在插件目录里的旧文件不会。
