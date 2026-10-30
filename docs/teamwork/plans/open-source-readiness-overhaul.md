---
status: active
superseded-by:
created: 2026-08-26
updated: 2026-08-26
---

# Plan: ServerPilot 开源可维护性全面整改

## Intended result and selected direction

- Subject identity: ServerPilot 从"能用的个人项目"整改为可对外维护的开源项目
- Authoritative owner: Root agent（用户做方向决策与验收）
- Intended result: 仓库不含私有内容；公开的安全声明与代码一致；MCP 符合规范且经协议级测试；`service.py` 按职责拆分；macOS 与 Windows 有对等发布链路；英文优先文档并发布到 PyPI
- Selected direction: 用户在三个分叉上定案 —— 全量重构（含 `service.py` 拆分，非"先规范后重构"）、插件泛化为公开参考实现（`hanhai22` 移到本地私有目录）、英文优先且发布 PyPI
- Scope and protected behavior: 不新增未经批准的防御机制（鉴权、插件签名、SSH 客户端固定均单列待批）；不新增兼容路径、fallback、开关或为迁就调用方而加的默认参数；`uq_active_lease_resource_gpu` 唯一索引、`BEGIN IMMEDIATE` 单写者模型、`BrokerError` 域错误类型、fail-closed 观测规则、routine MCP 恰好三工具，这些是既有正确设计，保留

## Current execution plan

分六阶段。阶段内可并行，阶段间按依赖串行。ruff 规则集扩展会重排全仓库格式，必须独占一轮。

1. 阶段 0 私有内容与仓库边界：`.gitignore` 收 `plugins/` 与 `marketing/`；`hanhai22` 先复制到本机用户插件目录并验证发现成功，再从仓库删除；写泛化 `slurm-immediate` 参考插件；插件测试与 `PLUGINS_zh.md` 去个人化
2. 阶段 1 安全声明与代码对齐：README / `AGENTS.md` / `SECURITY.md` 改成真话（keepalive 与插件确实起停远端进程；loopback 无鉴权且 actor 头可伪造；fail-closed 建立在"上报事实"而非独立验证之上）；已提交文件里的实验室标识 scrub；补不依赖 daemon 的显式恢复入口
3. 阶段 2 护栏：CI 扩为三 OS × 3.12/3.13 并纳入 `swift test`；MCP 协议级 stdio 测试；版本改 `VERSION` 单源；随后独占一轮做 ruff 规则集扩展与 format
4. 阶段 3 MCP 规范对齐：`no_capacity` 改结构化工具结果；修 `serverInfo.version`；工具改 async 且 daemon ensure 移入 lifespan；补 tool annotations 与 pydantic 输入 schema；文档补标准 `mcpServers` 配置块
5. 阶段 4 后端架构：先修真实 bug（插件子进程在写事务内、async 里同步 subprocess、`ResourceAllocation` 缺唯一约束、互斥锁只在 `api.py`、采集循环吞异常），再按 seam 十步拆 `service.py`、把 `api.py` 的 keepalive 状态机外移，最后消除重复领域规则与兼容路径
6. 阶段 5 桌面端：建 macOS 发布链路（Developer ID、hardened runtime、公证、universal）；抽出两个前端共同消费的设计 token 与阈值；修 Windows 端口回退劈裂控制面
7. 阶段 6 开源定位：README 英文优先 + `README.zh-CN.md`；`pyproject` 元数据补 Linux/3.13 与 urls；PyPI trusted publishing；补 `CODE_OF_CONDUCT` 与 issue/PR 模板；剥离 `AGENTS.md` 的 Teamwork 块

## Authoritative project records

- 宿主计划：`~/.cursor/plans/serverpilot_全面整改_e1fcb7ad.plan.md`
- 复查结论明细：`docs/teamwork/records/open-source-readiness-review.md`
- keepalive 恢复入口决定：`docs/teamwork/records/keepalive-recovery-not-lease-ttl.md`
- 前置计划：`docs/teamwork/plans/server-plugin-mechanism.md`（插件机制本身，本计划把它的产物泛化后开源）

## Dependencies and parallel tracks

阶段 0 阻塞一切提交，先行。阶段 0、1、2（不含 ruff）三条线路径互不重叠，可并行。ruff 规则扩展必须在这三条线合并后独占执行，否则全局重排会与所有并行改动冲突。阶段 3 与阶段 5 互不重叠，可并行。阶段 4 内部十步严格串行，且必须在 ruff 之后，否则重构 diff 会被格式变更淹没。阶段 6 收尾。

## Verification

基线：`uv run ruff check .` 通过；`uv run pytest -q` 恰好 2 个失败，均在 `tests/test_agent_policy.py`（未提交的插件工作把 `scheduler_servers` 写进 policy 文本，与"scheduler 细节不进 routine MCP 帮助"的断言冲突）。任何新增失败都由引入方修复。

每轮验收由 Root 执行：跑测试与 ruff 对照基线、审 diff、核规则合规（有无新增兼容路径、有无未经批准的防御机制、是否删除了被取代的旧路径）。阶段 4 每一步单独跑最贴近的测试文件。桌面端改动跑 `zsh desktop/build-macos-app.sh` 与 `verify-macos-app.sh`。

## Risks and replan conditions

- `service.py` 拆分是十步串行的数周工程。若中途用户改变优先级，按步收敛：每步都保持 facade 重导出，可在任意步之间停下且测试为绿。
- CI 矩阵扩到 Linux 会暴露 `tests/test_macos_daemon.py` 读小写 `desktop/fixtures/` 的路径大小写问题（macOS 大小写不敏感故本地不显）。
- 阶段 1 的文档改写在阶段 6 会被翻译成英文。接受这次重复，因为错误的安全声明是当前最高信誉风险，不能等到阶段 6。
- 若用户后续批准真实本地鉴权（per-agent token 或 unix socket SO_PEERCRED），阶段 1 的信任模型段落需要重写，属于材料性改动，需重新落盘。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-26 — 计划首次落盘

五个并行复查（MCP 规范、仓库打包、后端架构、桌面端、安全边界）完成后，用户在三个分叉上定案：全量重构、插件泛化、英文优先并发 PyPI。随后用户授权执行并要求 Root 承担验收与规则合规审查。
