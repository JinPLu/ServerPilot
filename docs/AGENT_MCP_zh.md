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
| Codex | `serverpilot mcp install --client codex` | `serverpilot mcp policy --install --client codex` |
| Claude Code | `serverpilot mcp install --client claude` | `serverpilot mcp policy --install --client claude` |
| Cursor | `serverpilot mcp install --client cursor` | `serverpilot mcp policy --print --client cursor` 后粘贴到 User Rules |

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

源码 / `uv tool install` 安装后 `serverpilot-mcp` 在 PATH 上（包括 Windows 上的 CLI 安装），直接写命令名即可。`serverpilot mcp config` 会自动解析出当前安装实际可用的那一个。桌面 App（仅 macOS）的设置页也展示同一份解析结果：绝对路径和可粘贴的 `mcpServers` 配置；找不到入口时会显示「未找到」和安装提示，而不会编造路径。

daemon 未运行时，macOS 上的 MCP 会尝试启动同一用户的 LaunchAgent；daemon 不兼容或未就绪时，调用会明确失败，不会创建备用数据库，也不会改走 SSH。

`serverpilot-mcp` 暴露的工具、参数和行为约束由 `src/serverpilot/agent_contract.py` 一处定义，运行时以 MCP server instructions 和工具 schema 的形式发给 Agent。默认配置不需要 `enabled_tools` 白名单。

## 日常 GPU 路径

```text
申请 → 使用返回的分配 → 释放
```

这条路径上的规则只有一个来源：`src/serverpilot/agent_contract.py`。Agent 在运行时从 MCP server instructions 读到它；同一份文本渲染成 `docs/AGENT_MCP_policy.en.md`，`.cursor/rules/serverpilot.mdc` 是它加上 Cursor 的 frontmatter。这两个文件是生成物：改契约改 `agent_contract.py` 再重新生成，`serverpilot mcp policy --check` 会把不一致直接打成 diff。本页不再复述规则，因为复述出来的那一份会先失效。

返回体里每个字段的形状不属于契约文本：以 MCP 工具 schema 和 REST 响应为准。

## 边界

- 服务器登记走 `gpu_add_server`（必填 `project_id`、`host`、`workspace_path`；分组主机同时给 `server_group_id`，不给就是未分组，`gpu_apply(server_group_id=...)` 永远选不到它）/ `gpu_update_server`（必填 `server_id`，其余安全元数据至少给一项）；其余生命周期与管理操作走 App 或 REST，不进入默认 Agent 上下文。登记会当场观测一次并在返回体的 `observation` 里给出结果（`observed` / `gpu_count` / `error`）：`observed=false` 说明记录建好了但机器没连上，原因在 `error` 里（例如 host key 未知），修好后下一轮采集自己接上，不要重复登记。两者的重放键由工具自己生成，调用方不传；重复登记同一台机器由 `endpoint_exists` / `endpoint_address_exists` 两条 409 挡住，拿到它们就改用 `gpu_update_server`。
- App 负责人工查看和纠错；默认 Agent 路径不需要额外生命周期步骤。
- ServerPilot 返回 SSH 连接参数但不提供密码、私钥或 shell；它复用当前用户已有凭据，也不代替项目自己的远端执行授权。

## 自检

```bash
serverpilot daemon status --json
serverpilot --help
serverpilot mcp policy --check
```
