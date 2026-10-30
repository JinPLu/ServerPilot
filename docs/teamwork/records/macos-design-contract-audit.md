---
status: active
superseded-by:
created: 2026-08-21
updated: 2026-08-21
---

# Record: macOS App 设计契约一致性与可访问性审计

对象：`desktop/ServerPilot.swift`（4,358 行）、`desktop/ResourceUsageDashboard.swift`、`desktop/AppSupport.swift`
基准：`docs/DESIGN_SYSTEM.md` §2–§11
日期：2026-08-21

## Current synthesis

- Subject identity: macOS App 设计契约一致性与可访问性审计这一持续目标
- 静态审计已出结论：9 项符合、5 项偏差（D1–D5）、5 项可访问性发现（A1–A5），另在执行 A1 时新发现 D6。
- A1、D1、D2、D3、D4 已处理；D5、D6、A2–A5 仍开放，需实机走查或单独立项。

## 方法与覆盖边界

本轮只做**静态可查项**：把契约里的规范性语句映射到源码符号并逐条核对，颜色对比度按 sRGB 相对亮度公式计算。

**未覆盖**，需实机走查或截图，不能凭本文档宣称合规：

- VoiceOver 朗读顺序与内容
- 键盘完整焦点顺序（§10 要求搜索、筛选、可排序表头、服务器行、返回、主要动作均可键盘到达）
- 三个验收视口（1024×640 / 1280×800 / 1440×820）的实际重排
- 动画在 Reduce Motion 下的实际表现
- 本机未安装 XCTest（见 `docs/IMPLEMENTATION_STATUS_zh.md`），Swift 单测与 XCUITest 未运行

## 符合契约的项

| 契约 | 核对结果 |
| --- | --- |
| §2 仅浅色，不跟随系统深色 | `ServerPilot.swift:55` 固定 `NSApp.appearance = NSAppearance(named: .aqua)` |
| §2 禁止毛玻璃 / 实时模糊 / 渐变光晕 | `ultraThinMaterial`、`.blur(`、`LinearGradient`、`RadialGradient` 在两个界面文件中命中数均为 0 |
| §3 状态色取值 | `AppSupport.swift:17-19` 逐值吻合 `#3EB866` / `#F2B100` / `#FB2C37` |
| §3 交互色与文字色 | `controlAccentColor` / `labelColor` / `secondaryLabelColor`，与契约一致 |
| §4 侧栏三个一级页面 | `ServerPilot.swift:708/711/714` = 服务器 / 使用情况 / 设置 |
| §5 表格列顺序 | 八列顺序与契约完全一致 |
| §5 资源列使用完整专业名称 | GPU 利用率 / 显存占用率 / CPU 负载 / 内存占用率，无缩写 |
| §5 禁用泛化状态词 | `待处理`、`快照过期`、`fail-closed`、`允许延迟` 命中数均为 0 |
| §8 设置页只有三项 | 本机服务地址 / 采集间隔 / 版本，无额外项 |

## 偏差

### D1 · 服务器列表头图标与契约不符（低）

契约 §5 表格列指定表头图标为 `server.rack`；实现两处均为 `terminal`。

- `desktop/ServerPilot.swift:1625`（标准布局）
- `desktop/ServerPilot.swift:1648`（紧凑布局）

判定：**改实现**。契约里 `server.rack` 与"一行一个服务器"的语义一致，`terminal` 表达的是 SSH 指令这一列内容，属于可辩护的再设计，但契约未记录此变更。若要保留 `terminal`，应改契约而不是让两者长期不一致。

**已处理**（2026-08-21）：两处改为 `server.rack`。

### D2 · 资源列表头在紧凑布局带图标（低）

契约 §5 规定四项资源指标表头"无（以完整标题说明）"。标准布局正确使用 `icon: nil`；紧凑布局给出了 `chart.bar.fill` / `memorychip` / `cpu` / `memorychip.fill`（`ServerPilot.swift:1659-1665` 一带）。

判定：**改契约**。窄窗口下完整标题会被压缩，图标是合理补偿；但契约当前的措辞不允许，需要补一句紧凑布局例外。

**已处理**（2026-08-21）：§5 表格列补充「四项资源指标表头在标准宽度下不带图标；只有切换到紧凑布局、完整标题被压缩时才补图标」。

### D3 · 设置项名称与契约不符（低）

契约 §8 写 `数据采集间隔`，实现为 `数据更新间隔`（`ServerPilot.swift` 设置页）。

判定：**改实现**。§5 明确区分"刷新"（重读控制面状态）与"采集"（远端观测），`更新` 一词恰好落在这个已被契约刻意分开的歧义上。

**已处理**（2026-08-21）：可见标签、`accessibilityLabel`、`BrokerStore` 的两条提示、`FixtureModeUITests` 断言与 `capture-fixture-evidence.sh` 校验共 6 处同步。

### D4 · 使用情况页空状态文案不符合契约（低）

契约 §7 要求空状态显示 `当前没有资源分配。` 并提供 `申请 GPU` 动作。

实现 `desktop/ResourceUsageDashboard.swift:359-364` 的页面级空状态**已经带主动作**（`ContentUnavailableView` + `申请 GPU` 按钮 + `PrimaryActionButtonStyle`），结构符合 §9 的"一句事实 + 一个主动作"。仅文案为 `当前没有 GPU 使用记录`，与契约的 `当前没有资源分配。` 不一致。

判定：**改实现**（文案）。§7 通篇以"资源分配"为准，`使用记录` 会读成历史流水，而该页明确不提供历史任务流水。

**已处理**（2026-08-21）：页面级空状态改为 `当前没有资源分配。`；`:538` 的分组级空状态保留原文案。

附注：`:570` 的 `暂无资源` 位于 `ResourceStateSummary` 内联汇总条（高 52pt，展示已分配/运行/申请中数量），不是页面空状态，不适用"一个主动作"要求。

### D6 · 状态色被用于文字标签，违反 §3 既有规则（中）

本项在执行 A1 时发现，**第一轮审计遗漏**。

`DESIGN_SYSTEM.md` §3 已有明文："压力颜色只放在状态点和进度柱上。" 但以下 5 处用状态色渲染文字：

| 位置 | 内容 | 字号 |
| --- | --- | --- |
| `ServerPilot.swift:1838` | `工作区未设置` | 9.5 pt |
| `ServerPilot.swift:1992` | `N 待确认` | 8.5 pt |
| `ServerPilot.swift:2219` | 图表警告 `Label` | 10 pt |
| `ServerPilot.swift:2275` | `visualWarningLabel` | 继承 |
| `ResourceUsageDashboard.swift:776` | `还需选择 N 块 GPU。` | 11 pt |
| `ResourceUsageDashboard.swift:922` | `request.stateLabel` | 9 pt |

对照组：`NoticeBanner`（正文用 `ink`，状态色只做 16% 底色与 35% 描边）和 `ResourceInlineStat`（状态色只在 7pt 圆点上）是正确写法。

A1 把这些文字的对比度从 1.77 提到 3.50，但 8.5–11 pt 属于正文尺寸，WCAG 要求 4.5:1，仍不达标。

判定：**改实现**，但需要重构视图——这些多为 `Label(text, systemImage:)`，`foregroundStyle` 会同时染色图标与文字，要把颜色收回图标就得拆开 Label。**未处理**，因为改动面超出本轮，且属于独立的设计取舍（是否保留彩色警告文字）。

### D5 · 深色分支为不可达死代码（低）

`AppSupport.swift:21-45` 的 `surface` / `surfaceStroke` / `ambientSmoke` / `glassSmoke` 以及 `chartSeries` 都带 `isDark ? … : …` 分支，但 §2 与 `ServerPilot.swift:55` 已把外观固定为 `.aqua`，深色分支永不执行。

判定：不是契约违规，是维护风险——它让读代码的人以为深色模式受支持。清理属于可选项，不阻塞任何交付。

## 可访问性

### A1 · 状态色对比度不足（中）

首次测量。按 WCAG 相对亮度公式，对 `DesignTokens.surface`（浅色 `#F6F7F9`）与页面底色 `ambientSmoke`（`#E4E7EC`）：

| 令牌 | vs 表面 | vs 页面底色 | 非文本图形阈值 3.0 (WCAG 1.4.11) |
| --- | --- | --- | --- |
| `success #3EB866` | 2.37 | 2.05 | **不达标** |
| `warning #F2B100` | 1.77 | 1.53 | **不达标** |
| `danger #FB2C37` | 3.55 | 3.07 | 达标 |

减轻因素：契约 §5 为每个状态规定了文字用语，§10 要求"指标提供文字值与单位"，所以颜色不是意义的唯一载体，未构成阻断性缺陷。但状态点和进度柱作为图形对象本身低于阈值。

建议：把绿与琥珀在**填充用途**上加深（保持色相），文字与状态标签配对维持现状。不要仅靠加大面积来规避阈值——1.4.11 不看面积。

**已处理**（2026-08-21）：`success` `#3EB866` → `#339653`，`warning` `#F2B100` → `#AA7C00`，`danger` 达标不变。只改明度、保持色相。改后实测：

| 令牌 | vs 表面 | vs 页面底色 |
| --- | --- | --- |
| `success #339653` | 3.48 | 3.01 |
| `warning #AA7C00` | 3.50 | 3.03 |
| `danger #FB2C37` | 3.55 | 3.07 |

三者视觉权重一致。`DESIGN_SYSTEM.md` §3 颜色表与对比度下限同步更新。未取 4.5 档（`#287642` / `#866200`）：琥珀会变成橄榄褐，丢掉调色板 identity，而契约本就只要求这些色用于图形对象。

### A2 · 界面完全不随系统文字大小缩放（高）

`.font(.system(size:))` 硬编码 **140 处**，语义字体（`.body` / `.caption` / `.headline` 等）**0 处**。

这就是 `docs/IMPLEMENTATION_STATUS_zh.md` 里"缩放重排未验证"的直接成因：不是没验证，而是当前实现下没有任何东西会随系统文字大小改变。契约 §3 的字体小节也未说明缩放行为。

建议：迁移到语义 text style 或 `ScaledMetric`，并在 §3 补充缩放契约。这是本轮最大的一项，且会触及几乎所有视图——应单独立项，不要夹在别的改动里。

### A3 · Reduce Motion 覆盖单点（中）

`accessibilityReduceMotion` 全项目仅 `ServerPilot.swift:747` 一处。契约 §10 要求"Reduce Motion 下不得依赖动画解释变化"，需逐一走查其余动画点是否有降级路径。

### A4 · 图标按钮可访问名称（未判定）

两个界面文件共 44 处 `accessibilityLabel`。契约 §10 要求"每个图标按钮提供可访问名称"，但图标按钮总数需要人工清点才能判断覆盖率，本轮未完成。

### A5 · 次文字对比度（未判定）

`secondaryLabelColor` 是系统动态色，粗估约 3.7:1，低于正文 4.5:1。但该估值不可靠——需用系统实际解析出的颜色测量，且它是 macOS 原生约定，改动等于偏离平台默认。**不据此提出修改建议**，只记录待测。

## 建议的处理顺序

1. ~~**A1**（状态色对比度）~~ —— 已处理。
2. ~~**D1 / D3 / D4**（图标与文案）~~ —— 已处理。
3. ~~**D2**（紧凑布局图标）~~ —— 已改契约。
4. **D6**（状态色用于文字）—— 下一优先级。需拆 `Label` 把颜色收回图标，属独立设计取舍。
5. **A2**（Dynamic Type）—— 单独立项，改动面最大。
6. **A3 / A4 / A5** —— 需实机走查后才能给判定。
7. **D5** —— 可选清理。

以上除 A1 的数值计算外，均未经实机验证；D1–D5 的实现改动都需要 `zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 复验。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-21 — 审计落盘

把 `docs/DESIGN_SYSTEM.md` §2–§11 的规范性语句映射到 Swift 源码符号逐条核对，颜色对比度按 sRGB 相对亮度公式计算；VoiceOver、键盘焦点顺序、三个验收视口与 Reduce Motion 未覆盖。
