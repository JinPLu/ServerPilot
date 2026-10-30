---
status: active
superseded-by:
created: 2026-08-11
updated: 2026-08-29
---

# Record: Gpu Broker Native Ui Workflow 2026 08 01 — 稳定候选评审

## Current synthesis

- Subject identity: 原生 UI 稳定候选（根目录唯一 `ServerPilot.app`）的评审判决这一持续目标
- 判决 ACCEPT：候选满足材料验收标准；残余风险仅为完整 Xcode 环境下的 XCUITest 与可访问性补验。

## 候选、范围与标准

- 稳定候选：仓库根目录唯一的 `ServerPilot.app`；前端 SHA `a48055759c63b956adfcd3f7502726a0dc1f79d540ef67d5b505c447f5de8d30`；后端 SHA `843053116886ed68473df1e6cdf927952689c3288b6131fae815c880f6188042`。
- 范围：资源列表与表头、排序、服务器详情、Usage 历史图、设置与采集间隔、浅色主题，以及 MCP 协调 URI、立即 `no_capacity` 公共契约、采集设置原子性、迁移和唯一 App 落盘。
- 保护边界：GUI 不实现 Agent 通信或收件箱；不向公开 GUI 暴露 Agent/owner/URI；租约不授权启动任务；采集仅固定只读探针；MCP 协调 URI 不授予认证或调度权限；不保留等待队列；只使用根目录唯一 App。
- 验收标准：浅色界面；服务器、项目/任务、GPU 配置和可分配列有清晰表头；柱状图承担状态颜色而非数字着色；排序与键盘导航可用；多 GPU 历史序列使用不同颜色；刷新与 5/10/30 秒采集设置可解释且持久；错误和空状态不泄露内部协作字段；迁移与公共契约测试通过。

## Outcome Fit

applicability: applicable

reason: 本候选有明确的 GUI、持久化、MCP 公共契约和唯一 App 交付标准。

evidence: 直接验收证据位于 `build/plan-closeout/evidence/*` 与 `build/plan-closeout/native-ui-acceptance-final3/manifest.json`；根目录 App 唯一性已核对；资源表头、排序、键盘 Tab、浅色主题、无可达 Agent/URI GUI、MCP URI 与 `no_capacity` 均有对应证据。

findings: 无未闭合的 outcome blocker。服务器表头已恢复，资源柱与标题对应；设置重复 label 与过时公开措辞已清除。

## Engineering Quality

applicability: applicable

reason: 候选包含 SwiftUI/AppKit、Python 服务、SQLite/Alembic 迁移、MCP 公共接口、构建与打包。

evidence: Python 全量测试 334/334 通过；Ruff 通过；Swift desktop/core 全量类型检查通过；`git diff --check` 通过；macOS build/verify 通过；独立 focused 23 项测试通过。迁移单头为 `20260812_0018`。采集间隔只有在数据库提交成功后才更新内存，提交失败回归测试通过。公开 GPU 申请在无容量时立即返回 `no_capacity`，不产生等待队列；list_tools 测试通过。根目录仅保留一个最新 `ServerPilot.app`。

findings: 无 BLOCKER 或 FOLLOW-UP。既有的 Xcode 工具链限制属于验收环境限制，不是候选实现缺陷。

## Real-Path Evidence

applicability: applicable

reason: 候选声明了真实 macOS App、浅色渲染、导航、设置、详情与历史图行为，需要宿主路径证据。

evidence: `native-ui-acceptance-final3/manifest.json` 记录 1024/1280/1440 宽度、列表/Usage/Settings/详情/错误/空状态/Inspector 证据；focused 23 项宿主验收通过。历史图按 GPU 序列分色；返回资源列表、表头、排序、搜索和键盘焦点均有可达路径。App 只按浅色外观交付。

findings: 无阻断性 real-path finding。当前机器没有完整 Xcode/XCUITest：8 项 XCUITest 声明为 0 执行；System Events 无法展开 error ScrollView opaque provider；高对比度/Reduce Motion 未取得行为级证明；详情/确认框没有 XCUITest 运行证据。这些限制已被证据清单标注，且不影响已覆盖的验收标准。

## Strict Gate Evidence

本次修改跨越持久化迁移和公开 MCP 契约，按 Strict Review 处理。迁移回归、公共 `no_capacity` 行为、MCP 协调 URI、采集设置提交原子性与唯一 App 落盘均有直接测试或构建证据；没有开放的安全、权限、数据或兼容性 blocker。

## Verdict

ACCEPT。候选满足材料验收标准；工程与真实路径证据支持浅色 GUI、清晰表头与排序、分 GPU 历史图、原子采集设置、MCP 协调和唯一 App 交付。已记录的 Xcode/XCUITest 缺口是可界定的环境补验，不构成当前发布阻断。

## Residual Risk and Next Action

残余风险仅为完整 Xcode 环境下的补验：重新执行 8 项 XCUITest，并补充高对比度、Reduce Motion、error ScrollView、详情/确认框的行为级证据。最小后续是提供完整 Xcode 环境后运行该补验；不改变当前 ACCEPT。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-11 — 评审判决落盘

Strict Review 覆盖 outcome fit、engineering quality、real-path evidence 与 strict gate，返回 ACCEPT 并记录 Xcode/XCUITest 缺口。

### 2026-08-29 — 补回迁移时丢失的候选 SHA

迁移进本文件时，「稳定候选」一行只保留了 App 路径，原记录同一行的前端 SHA 与后端 SHA 两个短句整条丢失，候选因此不再可被指认。按案卷原文逐字补回：前端 SHA `a48055759c63b956adfcd3f7502726a0dc1f79d540ef67d5b505c447f5de8d30`，后端 SHA `843053116886ed68473df1e6cdf927952689c3288b6131fae815c880f6188042`。实测补回前两个 SHA 在整个 `docs/teamwork/` 下的出现次数均为 0，即前端那半句同样已丢失，不只是后端那半句。其余正文与已有 History 条目未改。
