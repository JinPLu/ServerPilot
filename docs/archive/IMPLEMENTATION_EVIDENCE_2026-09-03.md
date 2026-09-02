# 2026-09-03 实施证据归档

这是 `2.0.0` 之前（`1.9.1` 及更早）各版本的历史验收记录，不代表当前实现或验证状态；当前结论见 `../IMPLEMENTATION_STATUS_zh.md`。

### 1.7.0 历史自动化结果

以下自动化结果来自当时的 `1.7.0` 工作树；测试使用临时数据库和 fake provider。它们证明该版本当时通过，不代表当前工作树或正在运行的进程已按同一套结果重新验收。

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `488` 项 collected，`uv run --reinstall-package serverpilot pytest -q` 通过；覆盖实际 ASGI body 限流与断连转发、并发限流、CSV 字段投影、Web actor CSRF、SQLite 原子备份、owner/operator 改派授权、direct 与 generic CPU/RAM 双向 admission、GPU 与主机近 10 分钟遥测均值、CUDA ordinal、MCP request-id 命名空间/传输重试/同参数多 lease，以及 Windows 桌面桥接白名单、错误映射、WebView2 本地 UI 主机与打包资源、keepalive v3 预检、固定 inspect、worker 身份校验、旧 wire/state 拒绝、PID 重用、marker、目标 GPU 映射和原子状态写入 |
| Ruff | `uv run ruff check .` 通过 |
| 数据迁移 | `1.7.0` 当时迁移头 `20260822_0031`；`endpoint_deletions` 保存已删除 endpoint 的墓碑，避免 YAML seed 复活；endpoint 的 `resource_kind` 默认 `unknown`，只由 collector 将 endpoint 更新为 `gpu` 或 `cpu_only`；`gpu_devices.cuda_ordinal` 初始为 NULL，只有当前 collector 观测才写入并恢复分配；`keepalive_current` 保存 `actual/error_reason` 与逐卡唯一进程身份，只把仍有 active resource 的活动 keepalive lease 转为无 TTL，保留 terminal keeper 与 workload 历史 expiry。当前源码另有 `20260827_0032` 服务器分组迁移和 `20260828_0033`（去掉调度器、规划与预设表）；本表不把这两条迁移记为已部署或已在运行库上执行 |
| MCP 上下文 | `1.7.0` 当时默认发现结果严格为 3 个工具，`gpu_apply` schema 为 `server_id / gpu_count / task`。当前源码日常面正好五个工具：`gpu_status`、`gpu_apply`、`gpu_release`、`gpu_add_server`、`gpu_update_server`；申请签名为 `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)`。adapter 使用进程随机命名空间与 MCP request ID 生成不公开的重放键；同一次本地 HTTP 传输失败只重试一次，不同调用不按任务名折叠。本行区分源码合同与当时发现结果，不表示已安装的客户端连接器描述已重新生成 |
| Agent 任务说明 | 默认 MCP 不依赖客户端身份、UI 标题或专用环境变量。`gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)` 的 `task` 接收用户任务名或当前目标的简短人类可读概括；未提供时使用「未命名任务」。`gpu_count` 来自启动配置，默认 1，不从空闲容量推断。`gpu_status` 的可申请容量按组/服务器/SKU 投影，不返回逐张空闲卡遥测：空闲卡上能观测到的负载是 ServerPilot 自己的占卡，分配前会停止，不能读作他人占用。遥测只在 `gpu_status(lease_id=…)` 的 `leased_gpus` 上返回，那里的每个进程都属于调用者自己的任务，用于判断利用率、剩余显存和多卡落差。endpoint `host_telemetry.recent_average` 仍提供 CPU 负载和内存占用均值供 GUI 使用。首次只读采集会将 endpoint 记录为 GPU、纯 CPU 或尚未确认；已识别的纯 CPU endpoint 保留主机容量，并以说明性的 `cpu_only_servers` 返回，但不会参与 GPU 分配。CPU 与内存容量由控制面统一解析为该 endpoint 实际拥有的额度（`host_telemetry.capacity`：容器取 cgroup 配额，整机才取 `cpu_count` / MemTotal），GUI 与 MCP 都只读这一份，不再各自从宿主机读数推算。`busy_gpus` 为忙卡返回人类可读 `task`。`gpu_add_server` / `gpu_update_server` 只做主机登记与安全元数据更新；删除仍只在 App 与 REST |
| macOS App 构建 | `zsh desktop/build-macos-app.sh` 通过，包含 Swift 桌面端编译；根目录 App 为 `1.7.0 / build 19` |
| standalone 验证 | `zsh desktop/verify-macos-app.sh` 通过 |
| Windows Desktop 打包规格 | `desktop/windows_launcher.py` 的桥接、输入白名单、错误映射和 UI 资源由单测覆盖；`uv run --extra windows pyinstaller ... ServerPilotWindows.spec` 的本机构建规格冒烟通过，实际 `.exe` 由 GitHub Windows Runner 交叉环境构建并上传 Release |
| 冗余机制扫描 | 运行源码和桌面端没有摘要计算、登录 token、占卡 `STARTING/HELD`、额外定时器或自动抢占；永久删除只走 REST/macOS 编辑页，有活跃租约或资源分配时拒绝；暂停/恢复与预约/维护 mutation 不再作为公共入口 |
| 文本与补丁完整性 | `git diff --check` 通过 |
| App 落盘 | 根目录唯一 `ServerPilot.app` |

### 1.5.6 定向现场验收

- 已将 v3 helper 部署到已登记 endpoint，并验证 `worker_attestation` 能力。原先被误投影为外部工作负载的空闲 keeper 已通过只读证明与新鲜采集恢复为 `desired=ON / actual=ON`；当前实际业务 workload 仍显示为「任务使用中」，没有被自动收养或停止。
- 在一台空闲 A800 endpoint 上用 routine MCP 完成 `gpu_status → gpu_apply → 远端 workspace cwd 下 CUDA gate → gpu_release → 下一次采集恢复`。gate 仅使用申请返回的 SSH、workspace 和 selector，确认 CUDA 可用且只可见一张已申请 GPU；释放后 keeper 回到 `desired=ON / actual=ON`。
- 已启动根目录 `ServerPilot.app` 读取实际 daemon 状态。当前 8 卡业务 workload 被逐卡显示为「任务使用中」，申请按钮禁用；没有显示为「归属待确认」或「可用」。

### 1.5.6 后续实机验收（同一已发布提交）

以下证据针对 Git 提交 `108750c`；只在两个确认空闲的 A800 endpoint 上使用正常 MCP 申请/归还路径。全程没有操作第三个 endpoint 上的八卡真实业务 workload，且每一个测试 lease 都已显式返回 `released=true`。

- **固定 helper 协议与 namespace 身份**：两台空闲 endpoint 都返回 `schema_version=3`、`implementation_version=1.5.6` 与 `per_gpu_keepalive / pidfd_identity / pci_bus_id / worker_attestation`。下列固定只读命令向 `--inspect` 提供精确 UUID 请求，1、2、4 卡分别在 0.79、0.84、1.06 秒内返回与请求数相同的 v3 worker attestation：

  ```sh
  # 将尖括号替换为已登记 endpoint 的值与绝对远端 workspace。
  printf '%s\n' '{"schema_version":3,"gpu_uuids":["GPU-…"]}' |
    ssh -p <port> <user>@<host> \
      'cd -- <absolute-workspace-path> && ./serverpilot-keepalive --inspect --schema-version 3'
  ```

  其中一个 endpoint 实际出现 helper namespace PID 与 NVIDIA driver PID 不同；attestation 仍以其唯一 driver-visible PID 和新鲜 collector 的 PID/boot ID 成功关联，证明该适配覆盖真实 namespace 差异。空 stdin 被 helper 拒绝为无效 JSON，是预期的只读 fail-closed 行为，不是 worker 异常。
- **单卡完整路径**：`gpu_status → gpu_apply(1) → CUDA gate → gpu_release → 采集恢复` 通过。gate 严格采用申请结果中的 SSH、远端 cwd 与 `CUDA_VISIBLE_DEVICES=0`，在已部署 PyTorch runtime 中得到 `torch 2.7.1+cu128`、`device_count=1`、A800 与 CUDA tensor 初始化成功；释放后 35 秒内由 `desired=ON / actual=OFF` 过渡回 `ON / ON`。恢复前后 inspect 都成功，被测试卡获得新的 helper/driver PID 与 ticks，未测试卡不变。
- **两卡与逐卡 selector**：`gpu_apply(2)` 返回完整 `CUDA_VISIBLE_DEVICES=0,1`；一个进程实际看到两张 A800，随后以 `0`、`1` 分别做 CUDA tensor 初始化与同步，都只看到一张 A800。释放后，未选两张卡始终 `ON / ON`，被选两张在 25 秒内恢复为 `ON / ON`。
- **daemon 与已安装 MCP stdio**：执行可逆的 `serverpilot daemon stop → serverpilot daemon start` 后，daemon 再次为 `live=true / ready=true`；一轮采集后六张空闲 A800 均为 `ON / ON`，真实八卡任务仍显示「任务使用中」。随后以已安装的 `serverpilot-mcp` 子进程完成真实 JSON-RPC initialize、工具发现和 `gpu_status(false)`：默认只发现 `gpu_status / gpu_apply / gpu_release`，并实际得到 loopback `/health/live`、`/health/ready` 与 snapshot 的 HTTP 200 响应。
- **错误路径自动化证据**：在临时数据库/fake adapter 中，旧 v2 helper 预检拒绝且不发送 mutation、attestation timeout、非唯一或格式错误 driver PID、helper 不兼容与 helper/collector 身份不匹配均 fail closed；重点组分别为 9 passed 与 6 passed。故意在共享 endpoint 注入外部进程、杀 worker、损坏状态或断网不属于本轮实机范围。

当前 inspect 为每张目标 GPU 串行执行一次固定的全机 NVIDIA compute-process 查询，而不是单次批量映射。现场 1–4 卡样本远低于 45 秒 adapter 预算，但样本量不足以说明高峰 P99；若后续有性能证据，再以保持「每张目标卡恰好一个 PID」与 fail-closed 解析为前提评估批量化。

四项核心功能的收敛决定（采集事实、人类监控 APP、Agent 申请·指派·释放、占卡与按卡让渡；冲突交人纠正；未使用服务器永久删除）记录在 `docs/teamwork/discussions/2026-08-12-four-core-product-scope.md`。该历史决定未覆盖本候选新增的 worker 身份校验；在建立明确的后继决定前，不能把它当作 `1.5.6` 的完整范围依据。

service 快照直接提供统一的 `publicly_available` 和简短中文 `public_status`；routine MCP 与桌面 App 只投影这份结果，不再各自判断占卡容量。API 与 Swift 模型分别校验 `desired=ON/OFF` 与 `actual=ON/OFF/ERROR`，遇到未知值会明确拒绝。

原生 APP 同样直接读取 `desired / actual / publicly_available / public_status`；`desired=ON, actual=OFF` 显示「占卡未运行」。工作租约是稳定的任务—GPU 指派：租用 GPU 上的 PID、启动时间和进程集合仅是采集事实。worker 重启、子进程替换或 bridge→后续队列的混合换代不会改变任务归属、触发 `CONFLICT` 或阻塞该租约；有有效工作租约且观察到计算进程时统一显示为「任务使用中」。服务启动和常规 reconcile 会将旧版本留下的 workload `CONFLICT` 归正为 `ACTIVE` 并关闭相应历史告警，不影响远端任务。占卡 helper 的 `ERROR/CONFLICT` 仍保持显式错误和不可申请，绝不作为工作任务收养。macOS App 通过独立 operator 路由让人调整任务—GPU 分配或释放已确认结束的任务；routine Agent 仍只能释放自己的 lease。Windows 桥接没有这条 operator 改派或 release-empty 路径。

macOS App 人工 GPU 改派现在也走独立的 loopback desktop operator 路由；普通 lease API 只能由 lease owner 调用，不能借 keeper reclaim 跨过授权。

## 1.5.5 历史现场验收（已脱敏）

以下是 2026-08-14 针对 `1.5.5` 的现场验收摘要，不代表 `1.5.6` 已通过同一批实机验收：

- 已验证本机 daemon、MCP stdio 与根目录原生 App 的安装、构建和 standalone 启动；默认 MCP 发现严格只有 `gpu_status`、`gpu_apply`、`gpu_release`。
- 在多台已登记 GPU endpoint 上完成了读状态、单卡/多卡申请、忙卡可见、显式释放、`no_capacity` 不排队，以及 CUDA ordinal selector 的最小 CUDA gate。
- 已验证逐卡占卡的精确让渡：申请或改派只停止目标 helper，释放后该卡按策略恢复；同机其他空闲卡不受影响。
- 已验证原生 App 的申请、查看、同卡数改派和释放；Web 的快速申请与 owner 释放也有单独按钮验收。
- 已验证 collector 对工作负载的自动归属和根 App 的并发刷新。本文件不再记录现场名称、主机、账号、路径、GPU UUID、任务或性能数字；`docs/audit/2026-08-13-real-e2e/` 的生产审计截图已从当前工作树删除。若仓库已有公开历史，仍需另行评估是否清理历史对象。
