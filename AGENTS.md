# ServerPilot (`serverpilot` package)

- 本机 loopback GPU 协作控制面；GUI、CLI、MCP 共用 REST/领域逻辑。ServerPilot 管理自己的占卡进程与插件侧分配的生命周期，但不启动、停止、迁移或抢占你的 workload。
- `README.md` 是用户入口；当前交付与 gate 只写 `docs/IMPLEMENTATION_STATUS_zh.md`；全局 MCP 安装见 `docs/AGENT_MCP_zh.md`，外部 Agent 的运行契约以 MCP instructions 为准。
- 源码在 `src/serverpilot/`；`build/`、`dist/`、`*.egg-info/`、`state/` 是生成物或运行状态。`inventory.yaml` 仅描述静态资产，不能证明 GPU 可用。

## 实现边界

- `service.py` 拥有调度、租约、队列、状态和审计；`api.py` 组合接口；持久化契约在 `database.py`、`models.py`、`src/serverpilot/migrations/`。CLI 的运营命令和 MCP 必须走 REST；仅 `init`、`serve`、`backup`、`restore`、`collect once` 等本地维护入口可直接组合领域服务，不得直连 SQLite 或复制领域规则。
- `daemon reclaim`、`keepalive inspect`、`keepalive stop` 是控制面不可用时的人工恢复入口。它们只读控制面数据库取端点连接事实，其余一律经密封 adapter 与只读采集；不得写库，也不得复制领域规则。只能由人或 Agent 显式调用，不得成为后台策略。
- Collector 对内置 profile 只能执行固定只读 SSH 探针；对已发现插件只调用 `observe`。插件的四个固定动词按调用方分工：发现流程调 `info`，Collector 调 `observe`，`service.py` 在分配与归还时调 `apply`、`release`（后两者仅在插件声明对应能力时）。不得接收 shell、读取私钥、完整命令或环境。插件是本机可执行文件，密封的是调用契约而不是实现来源。
- GPU UUID 与 endpoint `id` 是身份边界；同 IP 不同端口不可合并。telemetry/采集异常、非托管进程、维护或冲突一律 fail closed；这是对采集所报告事实的本机校验，SSH 用户与远端采集入口被假定为受信。
- `project_id + task_ref` 是工作任务的稳定身份。默认 Agent 路径严格只有 `gpu_status(server_id?, lease_id?)`、`gpu_apply(server_id?, gpu_count=1, task?)`、`gpu_release(lease_id)` 三个工具；遥测只随租约投影，可申请卡只讲容量；连接与远端工作目录逐服务器投影在 `servers[]`，不逐卡重复；不依赖特定客户端的身份、UI 标题或专用环境变量。`task` 使用用户任务名或当前目标的简短人类可读概括。ServerPilot GUI 不实现消息、收件箱、未读状态或通信表单。
- 控制面无认证：`X-ServerPilot-Actor` 是审计标签，同一用户下的本机进程都相当于操作者。非 loopback、访问控制、远端运行时或自动 allocator 的开放须单独批准。
- 测试和迁移使用临时数据库与 fake provider，不碰实时 `state/`；迁移不得覆盖活动数据库。

## 修改与验证

- Python 3.12；依赖改动同步 `uv.lock`。持久化或公共行为改动同步 migration、文档和测试。
- 运行最贴近改动面的测试与 Ruff；除非用户明确要求只读 shadow 采集，否则不得连接真实 GPU。
- 仓库中的唯一桌面应用固定为根目录 `ServerPilot.app`。不得在 `dist/`、`build/`、`~/Applications` 或仓库其他位置创建、复制或保留第二个 `ServerPilot*.app`，也不得用编号副本规避覆盖。
- 每次桌面端修改后必须运行 `zsh desktop/build-macos-app.sh`，该脚本原地替换根目录 `ServerPilot.app`；随后运行 `zsh desktop/verify-macos-app.sh`。评审和启动一律使用根目录这一份，不能打开旧路径或缓存副本。

## 发布与版本

- 推送含用户可见行为的改动前，先在 `CHANGELOG.md` 和 `CHANGELOG.en.md` 的 `Unreleased` 中用简短、面向结果的文字记录；不要把测试、内部 gate 或提交历史写成发布说明。
- 仅推送提交不等于正式发布。正式版本发布须经用户明确要求，并完成所需验证、更新 `src/serverpilot/__init__.py` 的 `__version__`（打包与桌面版本都从它派生）、将 `Unreleased` 归入版本条目、提交、tag 和 GitHub Release。
- 获准正式发布时默认使用 `master`；其他 Git 工作流、分支、PR 或发布目标须由用户明确指定。

<!-- TEAMWORK_PROJECT_START -->
## Teamwork Project Instructions

- Project label: `ServerPilot`.
- Teamwork adds no required project-local workflow or state. It creates no empty directory, schema, or mandatory stage chain. Native host modes stay in charge. Follow this project's normal instructions and invoke a named Skill only when its trigger matches.
- User-accepted reusable results live under `docs/teamwork/<kind>/` as one of `discussions`, `research`, `debug`, `plans`, `reviews`, `reports`, or `experiments`. Chat, host plans, and todos are not cross-session memory.
<!-- TEAMWORK_PROJECT_END -->
