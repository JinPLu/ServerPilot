---
status: active
superseded-by:
created: 2026-08-26
updated: 2026-08-26
---

# Record: ServerPilot 1.8.0 + 未提交插件工作的开源可维护性

## Current synthesis

- Subject identity: ServerPilot 1.8.0 + 未提交插件工作的开源可维护性审查这一持续目标

REVISE。底层设计判断多数正确且应保留（单写者 SQLite + `BEGIN IMMEDIATE`、活跃租约 GPU 唯一偏索引、fail-closed 观测、密封 SSH argv、官方 FastMCP + stdio、客户端中立性）。但四个面各有系统性缺陷：工作区含随时会被 `git add .` 发布的私有集群拓扑；公开安全声明与代码不符；MCP 把业务结果建模成协议错误且工具同步阻塞事件循环；`service.py` 已是 11,680 行单类，`api.py` 长成第二个领域层。桌面端 macOS 无发布链路、双前端已在漂移。

## Candidate identity and review contract

- Frozen candidate: `master` @ `d4cc449`（tag `v1.8.0`）+ 未提交的插件子系统工作区
- Authoritative owner and independence: Root agent 汇总，**非独立**——同一 Root 随后执行了据此产生的整改。取证由五个 subagent 分头完成、彼此不共享结论；汇总与裁决不独立于执行方
- Scope and exclusions: 覆盖 MCP 规范符合度、仓库/打包/CI/发布、后端架构与并发、桌面端与发布分发、安全边界与声明真实性。不覆盖：算法正确性、UI 视觉审美、性能基准
- Successor: 整改计划见 `docs/teamwork/plans/open-source-readiness-overhaul.md`

## Criteria and protected boundaries

判据来自项目自述与开源惯例：`AGENTS.md` 声明的模块边界与"插件只调用 observe"、README「边界与安全」四条声明、MCP 规范与主流 MCP server 惯例、Python 开源项目的打包/CI/社区文件基线、"一个行为一条路径"的项目规则。

不得改变的既有正确行为：routine MCP 恰好三工具且客户端中立；`uq_active_lease_resource_gpu` 偏唯一索引；stale 观测不算容量；`adapters.py` 的固定 SSH argv 与 `BatchMode=yes` / `StrictHostKeyChecking=yes`；插件调用 argv-only 无 shell 且带输出上限。

## Direct evidence

- `rg` 计数：`plugins/hanhai22` 与 `src/serverpilot/bundled_plugins/hanhai22` 各 24 处私有标识；`pyproject.toml:75` 已把 `bundled_plugins/*` 打进 wheel；`.gitignore` 不含 `plugins/` 或 `marketing/`
- `service.py:7622` 调 `apply_plugin`、`7987` 调 `release_plugin`，二者均在 `_write` 的 `BEGIN IMMEDIATE` 事务内，`plugins.py:247-251` 超时 60 秒
- 实测 MCP stdio：`initialize` 返回 `serverInfo version='1.29.1'`（MCP SDK 版本，非 ServerPilot 1.8.0）；`tools` 为三工具；`gpu_status` `isError: False`
- `server_keepalive.py:209` `start_new_session=True`；worker 主循环 `880-889` 无截止时间、无心跳；`api.py:1104-1111` lifespan 只取消本地采集任务
- 基线：`uv run ruff check .` 通过；`uv run pytest -q` 恰好 2 失败，均在 `tests/test_agent_policy.py`

## Findings

| Severity | Finding | Precise evidence | Impact / failed criterion | Smallest correction |
| --- | --- | --- | --- | --- |
| 高 | 私有集群拓扑随时会被发布 | `plugins/hanhai22`、`src/serverpilot/bundled_plugins/hanhai22` 未被 gitignore；`pyproject.toml:75` 打进 wheel；`build-macos-app.sh` 打进 App | 一次 `git add .` 即泄漏 SSH 别名、分区、QoS | gitignore 收口 + 移到本机用户插件目录 + 写泛化参考插件 |
| 高 | "不启停 workload"声明与代码矛盾 | `adapters.py:442-518` 起停远端 CUDA 占卡进程；`service.py:7622/7987` 调插件 apply/release | README 安全清单失真 | 改写为"管理自有占卡进程与插件侧分配，不碰你的 workload" |
| 高 | `AGENTS.md:10`"插件只调用 observe"为假 | 同上 | 项目自述的边界文档不可信 | 改为四动词契约 |
| 高 | loopback 无鉴权且 actor 头可伪造 | `api.py:1164-1167`、`service.py:653-704`、`_can_manage_lease` 仅比对 `actor_id` | 任何同 UID 本地进程可释放他人租约；README 未披露 | README 补真实信任模型；真实鉴权单列待批 |
| 高 | 插件子进程在 SQLite 写事务内执行 | `service.py:7622`、`7987` + `plugins.py:247-251` | 写锁最长持有 60 秒；apply 成功后回滚泄漏集群侧分配 | 照 `service.py:6515-6516` 的空闲回收写法，事务内收集引用、提交后调用 |
| 高 | `service.py` 单类 11,680 行 218 方法 | `snapshot()` 单方法 1,139 行；keepalive 1,380 行；Slurm 1,160 行 | 无法安全演进 | 按 seam 十步拆分 |
| 高 | `api.py` 成为第二领域层 | `create_app` 内联 keepalive 状态机 `477-756`、回收 `758-881`、插件 claim `883-952` | 违反 `AGENTS.md:8-9` 自述边界 | 外移为 `KeepaliveReconciler` 与 `ClaimOrchestrator` |
| 高 | macOS 无发布链路 | 仅 `build-macos-app.sh` + `codesign --sign -`；Windows 有完整 GHA release | 外部用户拿不到可信构建 | 建对等 macos release 工作流 |
| 中 | `no_capacity` 被建模成协议错误 | `mcp_server.py:1634` → `client.py:74-78` → `ToolError`；而 `gpu_status` 在 `718-722` 走成功路径 | Agent 需正则匹配 `"broker HTTP 409"` | 统一为结构化工具结果 |
| 中 | `serverInfo.version` 上报 SDK 版本 | 实测 `1.29.1`；`mcp_server.py:35-39` FastMCP 不接受 version | 客户端无法识别服务端版本 | 构造后设置底层 server 的 version |
| 中 | MCP 工具同步阻塞事件循环 | `daemon.py:627-632` 最长 15 秒；`client.py:52-64` 同步 httpx；`client.py:156` 300 秒 sleep | 单次调用可冻结整个 MCP 进程 | 改 async + `httpx.AsyncClient`，ensure 移入 lifespan |
| 中 | 控制面死亡后远端 keepalive 永不退出 | `server_keepalive.py:209`、`880-889`；`api.py:1104-1111` | 卡被物理占死，非 ServerPilot 用户也被挡 | 用户定案：不加租期，加显式恢复入口 |
| 中 | keepalive/插件 SSH stdout 无字节上限 | `adapters.py:400-401`、`493-495`、`566-568`；采集路径有 1 MiB 上限 `233-259` | 恶意远端可 OOM 本机 daemon | 复用采集路径的同一上限 |
| 中 | 三套并行分配路径 | 内置 `_eligible_gpus`；`create_resource_claim` `5197-5211` 对 GPU 直接 422；插件 apply 作为集群侧分配器 | 一个行为三条路径 | 收敛为单一 admission，插件作 provider |
| 中 | 四套快照投影 | `snapshot` / `control_plane_state` / `coordination` / `_compact_gpu_status` | 同一事实四处推导 | 单一 snapshot 构建器 + 纯函数视图 |
| 中 | 双前端无可执行契约 | 压力阈值 macOS 70/90 vs Windows 60/80（`ServerPilot.swift:2934-2938` vs `app.js:150-153`）；历史曲线缺口处理不一致 | IA 改动必须做两遍，否则漂移 | 抽共同消费的 token 与阈值 |
| 中 | Windows 端口回退劈裂控制面 | `windows_launcher.py:137-145` 占用时退到 8788+，而 MCP/CLI 仍指 8787 | 两个 broker 各持一半状态 | 照 macOS 的做法拒绝启动 |
| 中 | CI 只有 ubuntu + 3.12，ruff 只开 pyflakes | `.github/workflows/ci.yml`；`pyproject.toml:88` `select = ["F"]` | 支持声明无 CI 背书；39 个 Swift 测试从不运行 | 三 OS × 3.12/3.13 矩阵 + 纳入 `swift test` + 扩 ruff 规则 |
| 中 | 版本字符串五处副本 | `VERSION`、`pyproject.toml:7`、`__init__.py:3`、`keepalive_protocol.py`、`Info.plist`；第六处 `IMPLEMENTATION_STATUS_zh.md` 已停在 1.7.0 | 靠测试维持一致 | `VERSION` 单源派生 |
| 中 | 文档语言反向 | README/CHANGELOG.md/AGENTS.md/5 份 `docs/*_zh.md` 全中文；英文仅 4 份 | 国际社区发现成本 | 英文优先 + `README.zh-CN.md` |
| 低 | `ResourceAllocation` 缺活跃唯一约束 | `models.py:600-623` vs GPU 侧 `models.py:790-795` | host-capacity 认领是 check-then-act | 照 GPU 侧加偏唯一索引 |
| 低 | 采集循环吞全部异常 | `api.py:1085-1088` | 持续故障表现为"安静空闲" | 记录并告警 |
| 低 | 死代码与不可达写 API | `_gpu_dict` `3375-3422` 无调用；`create_maintenance` `9238` 无 POST 路由 | 维护负担 | 删除 |
| 低 | `SECURITY.md` 无披露流程 | 仅"私下联系维护者" | 无法负责任披露 | GitHub Security Advisories + 支持版本 |
| 低 | 缺社区文件 | 无 `CODE_OF_CONDUCT`、issue/PR 模板、Dependabot | 开源基线不全 | 补齐 |

## Criterion status

| Criterion | Status | Evidence |
| --- | --- | --- |
| 仓库可安全公开 | 未达成 | 私有插件与 `marketing/` 未 gitignore；已提交测试含 `<内网主机>` / `<内部路径名>` / `<内部路径>` |
| README「只协调归属，不启停 workload」 | 矛盾 | keepalive 与插件 apply/release 确实起停远端进程 |
| README「只做固定只读采集」 | 部分达成 | 内置 profile 确为三条固定命令；插件 apply/release 是远端变更 |
| README「一律 fail closed」 | 部分达成 | 本地校验严格；建立在"上报事实"之上，说谎的 collector 可得 AVAILABLE |
| README「只监听 loopback」 | 达成 | `cli.py:159-160`、`daemon.py:76-88`、`windows_launcher.py:333` |
| `AGENTS.md` 模块边界 | 未达成 | `api.py` 拥有领域编排；MCP/CLI 就插件绕过 REST |
| MCP 规范符合度 | 部分达成 | 官方 FastMCP + stdio + 中立性成立；结果建模、版本上报、异步性、annotations 不合格 |
| 一个行为一条路径 | 未达成 | 三套分配路径、四套快照投影、双 MCP registry、多处兼容参数 |

## Verdict

REVISE。无阻断性安全漏洞（无 `shell=True`、无凭据落盘、无非 loopback 绑定），但在私有内容外泄风险、声明真实性、MCP 结果建模与核心模块可维护性四项上不满足开源可维护标准。整改路径已由用户定案并落盘为计划。

## Residual uncertainty and bounded recheck

- 未验证：CI 矩阵扩到 macOS/Windows/3.13 后的实际失败面。已知一处预测失败：`tests/test_macos_daemon.py:849` 读小写 `desktop/fixtures/`，在 Linux 上区分大小写。
- 未验证：泛化后的 `slurm-immediate` 参考插件在真实 Slurm 集群上的 `apply`/`release` 行为。本次复查只读，未连真实集群。
- 未量化：`service.py` 拆分后各模块的实际行数与循环依赖，需在拆分过程中逐步确认。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-26 — 复查首次落盘

五个独立 subagent 并行取证后由 Root 汇总。用户据此定案三个方向分叉并授权执行整改。
