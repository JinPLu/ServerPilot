# ServerPilot 当前实现与验证状态

更新时间：2026-11-05（Asia/Taipei）

当前包装版本为 `5.2.2`。下方最新条目是当前交付状态，
更早日期的验证条目保留为实施证据，不能替代本轮现场结论。

## 停占卡改走复用连接，连不上不再报“请升级 helper”（2026-11-05，5.2.2）

一个 Agent 会话在百舸 hyshi 申请 4 张卡，组里有 7 张空卡（单次最多 5 张），却连续三次被拒，返回 `keepalive_helper_incompatible`，提示“请先完成该服务器的 helper 升级”。用户问：“怎么申请不了”。

根因：空卡挂着占卡程序，分配前要逐卡停掉；停之前 adapter 先跑只读的 `--protocol-info` 预检。采集早已改走 daemon 的复用 SSH 连接，占卡 adapter 却每次新建连接，而且连接加执行一共只给 8 秒。当天新建连接变慢，`daemon.log` 记了 30 条 `protocol-info probe timed out`，全在 hyshi 的两台机器上；同一时间采集一直正常。超时又被归成“版本不匹配”，于是 fail closed，还把人引去升级一个没有问题的 helper。

改动：

- 占卡的预检、启停和核验三条 SSH 改用采集那份参数（`observation_ssh_argv` 改名 `endpoint_ssh_argv`），和采集共用同一个 `ControlMaster`。预检时限从 8 秒改成一次采集的时限 30 秒，`direct_claim_budget_seconds` 随之变大。
- 预检超时、ssh 起不来或 ssh 自身退出码 255，改报新的 `keepalive_helper_unreachable`；只有 helper 真的答了不兼容才报 `keepalive_helper_incompatible`。两者都发生在任何变更之前。
- 申请和停卡遇到这个码时返回 503，details 带 `retry_after_seconds: 30`；MCP 把它列为可重试的拒绝，Agent 拿到 `retryable: true`，不再是工具异常。

验证：全量 Python 800 项、Ruff 通过（在只含已跟踪文件的干净副本里跑：工作区里有 61 个同步冲突留下的 `* 2` 旧副本，其中两份迁移让 alembic 出现两个 head；逐个核对都等于 HEAD 或某个历史提交后，经用户同意删除）。`update-macos-app.sh` 一次跑通，运行中的后端构建含新错误码，复用连接的 socket 已重建。

未验证：没有在真实服务器上走一次“停占卡再分配”的新路径，更新后 hyshi 已没有空卡；也没有在慢连接下实测复用连接确实避开了超时。

## 指标条按余量着色，提醒写服务器地址（2026-11-04，5.2.1）

用户看了 5.2.0 的真实数据后，附上 Beszel 和 ServerPilot 的对比截图，说：“改成地址，我也希望服务器页这里的颜色也能像beszel这样有多种颜色，不要统一橙色或者灰色。设计一下”。这改变了 5.0.0 定下的“指标条平时是中性灰，GPU 利用率不告警”。

改动：

- `ResourceMetric` 改成四项指标同一条规则：80% 起黄色，95% 起橙色。黄线和方块变黄的线相同，CPU 的黄线因此从 90 降到 80，`endpointHighPressure` 也随之提前，这会影响悬停提示的“压力较高”、连接状态排序和 `attentionEndpoints`。
- 指标条正常时从灰色改成绿色 `meterRoom`，满了用橙色 `meterFull`。详情弹窗里的显存环走同一个 `metricColor`，也跟着变色。
- 最高一档起初照 Beszel 用红色，用户问：“95%以上是红，会不会和前面红色表示有错误冲突”。确实冲突：图例里红色是“有错误”，而且训练时显存占到 95% 以上很常见，正常跑任务的机器也会一片红。所以改成橙色，红色只表示出错；GPU 利用率也不再单独例外。
- 删掉 `metricTextColor`，数字一律深色；占卡和离线的读数仍是灰色。
- `持续无法连接`、`需要重新登录` 的文字从 `endpoint.id` 改成 `host:port`。提醒文字每次采集都会重写，所以还开着的提醒也会更新。

验证：全量 Python 795 项、Swift 91 项、Ruff 和规则检查通过，App 构建与包校验报告 5.2.1；夹具截图看了服务器页。夹具里本来没有 95% 以上的读数，临时调高一行截图确认橙色和黄色分得开，之后已还原。

发布后在日常环境确认：`update-macos-app.sh` 一次跑通，共 97 秒；开着的 4 条服务器提醒都写地址，已结束的 126 条旧记录不再被重写，仍是内部编号。

未验证：真实数据下的服务器页和 Retina 屏上的观感；增强对比度下的橙色，换成橙色后没有再截。

## 界面第一轮：白底细描边、页头、容量卡、字号收口（2026-11-04，5.2.0）

用户看了 cc-switch 后要求参考它优化整个产品，确认样稿后说“开工第一轮”，之后决定“先发布第一轮，第二轮先不要”。取舍和暂缓的第二、三轮见 [讨论记录](teamwork/discussions/cc-switch-reference.md)。容量卡前面试过三版都被否定（每卡一块扇形、多色圆环、蓝色画不空闲），用户的最后一条反馈：“圆环还是难以理解，并不清楚什么表示空闲什么表示不空闲，而且和下面具体颜色有些冲突的感觉。”

根因：标题和数字说的是空闲，蓝色弧线画的却是不空闲，两者说的不是一件事；蓝色又同时表示“利用率低”“运行中”和趋势线，所以和方块冲突。

总览三卡竖排的 bug：三张卡包在 `ViewThatFits` 里，它按理想宽度判断放不放得下。提醒文字虽然限制成一行，理想宽度仍是全文长度，一条长提醒就让它退到竖排。在夹具里加入长提醒后复现，和用户截图一致。

改动：

- 容量卡：`CapacityRing` 的绿色弧线画空闲，数字写在环里，左边用文字写在用、离线、异常和单次最多。
- 三张卡删掉竖排分支，始终并排；窗口最小 900 宽时内容区仍约 790，放得下。
- 趋势线和小曲线改用独立的 `chartLine`，状态词改成圆点上色、文字中性，错误保持红字。
- 页面改白底，卡片 10% 细描边，圆角从 16 降到 10，控件从 7 降到 6；按钮从胶囊改成圆角矩形，次要按钮改描边。
- 页头改成顶到窗口上沿的 52 pt 窄栏，页面标题从 26 降到 17，大数字保持 26。
- 服务器页筛选改成分段控件；任务列从 170 收到 164，保住 1280 宽下的每张卡列。
- 65 处裸字号全部走 `Typography`，10 pt 取消，新增测试拦住以后再写裸字号。删掉 `hero`、`metricLabel`、`secondary`、`glassSmoke`、`ambientSmoke`、`Radius.tile`。

验证：

- 夹具截图看了总览（900、1280、1440 宽）、服务器页、任务页、提醒页、设置页和详情弹窗，以及增强对比度下的总览。
- 页头进入了标题栏区域，临时加代码向真实窗口发送鼠标事件，确认刷新按钮能点到，测完已还原。
- 全量 Python 794 项、Swift 92 项、Ruff 和规则检查通过，App 构建与包校验报告 5.2.0。

未验证：

- 真实数据和 Retina 屏上的观感，夹具截图是 1 倍屏。
- 拖动页头空白处移动窗口：命中检查说可以，但没有真的拖过。
- 分段控件的滑动动画和键盘操作；0 张卡时的总览；菜单栏弹窗。
- 服务器页和任务页在真实数据下的样子：日常环境已更新到 5.2.0，更新脚本一次跑通，共 93 秒，总览在真实数据下显示正常，其他页面还没看。

## 后台改用普通优先级，修复更新时的误报（2026-11-04，5.1.1）

现象：把日常环境从 4.2.0 升到 5.1.0 时，`update-macos-app.sh` 连续两次报“daemon did not become ready”，而后台随后正常就绪。脚本在启动 App 之前就停了，所以用户那边没有 App 在运行。

原因：LaunchAgent 从第一个提交起就是 `ProcessType: Background`，没有记录理由。这个优先级下，机器忙时新安装后第一次启动超过 15 秒，也就是 `start` 和 `ensure` 的等待上限。缺字节码会放大它，但在普通优先级下不是问题。

实测（真实 launchd，临时 label、临时数据库、空清单，机器负载约 6，各两次，单位秒）：

| ProcessType | 无字节码 | 有字节码 |
| --- | --- | --- |
| Background | 16.9、17.2 | 4.6、8.8 |
| Standard | 1.9、2.0 | 0.8、0.6 |

改动：`render_launch_agent` 改成 `ProcessType: Standard`，15 秒的等待不变。没有选择只加长等待：MCP 连接也走 `ensure`，等太久会撞上客户端自己的连接超时；App 也得白等。下一次更新时 `daemon install` 会写入新 plist，所以不需要手动处理。

代价：后台的 CPU 不再让位。实测约占单核的 5%（15 分钟共 42 秒，含启动）。空闲时接口延迟两种优先级没有差别，中位数都在 5–13 ms，所以不能说它让日常响应变快。

验证：全量 Python 792 项、Swift 92 项、Ruff 和规则检查通过，App 构建与包校验报告 5.1.1。发布前有一次高负载下的全量运行（320 秒，正常 203 秒）失败 1 项，单独重跑和下一次全量都通过；当时没有记下是哪一项，这次改动不涉及时序。

发布后在日常环境确认（真实数据库和清单，负载约 8）：`update-macos-app.sh` 一次跑通，共 94 秒；进程启动到开始跑迁移不到 2 秒，此前是 17–22 秒；调度优先级从 4 变成 20。

未验证：负载更高时的表现。App 启动和 MCP 连接会被这个等待拦住是读代码得出的，没有实际观察到。同一次升级里，`swift scripts/macos_app_process.swift inspect` 有一次 120 秒超时，之后只要 0.5–1.4 秒，没有复现，原因不明，这次没有处理。

## 服务器表恢复两行，表头横条，字号四档（2026-11-04，5.1.0）

用户看过 5.0.0 的实际效果后提出四点：服务器列保持原来的设计，卡型放回每张卡列，表头学 Beszel，首页和任务页的字号规整。用户看过截图后确认发布，按以往“小版本”的叫法定为 5.1.0；MCP 工具、插件协议、CLI 和数据库结构都没变。

改动：`EndpointTableRow` 和 `SchedulerNodeTableRow` 的服务器、任务、每张卡三格恢复两行，删掉 `modelTag` 和组标题上的型号，只留一处。连不上时状态行写明读数多久前，这个事实原来在任务列，由 `lastReadingAge` 提供。`ServerActivity` 只剩总览磁贴在用，删掉 `secondary`。`EndpointTableHeader` 改成 40 pt 白色横条，每列用 `EndpointColumnHeader` 画图标、列名和箭头，图标写在 `EndpointSort.systemImage`。列宽调成每张卡 128、空闲 60、指标 80，保住 1280 窗口下的每张卡列。`Typography` 删掉 `hero`、`pageTitle`，`display` 从 34 改成 26，新增 15 pt 的 `sectionTitle`；原来指向 13 pt 的三处 `sectionTitle` 改用 `label`。主、次按钮文字从 12 改成 13，影响全 App。

验证：用桌面夹具看了服务器页（1024、1280、1440 宽，以及 Slurm 节点）、总览和任务页的截图。列标题没有被截断，1280 宽每张卡列和完整型号都显示，1024 宽每张卡列按设计折叠。全量 Python 792 项、Swift 92 项、Ruff 和规则检查通过，App 构建与包校验报告 5.1.0。`verify-macos-app.sh` 最后一步要和已安装的后端比对，日常环境还是 4.2.0，所以在开发目录会失败，升级日常环境后才算验证完。

未验证：真实数据下的观感；表头悬停和键盘操作只读了代码，没有手动点过；长型号（如带 `+1 类` 的混合机器）的截断效果。提醒、表单、弹窗和设置页的字号没有动，仍有 12、10 和 17。

## 5.0.0 为什么是主版本（2026-11-04）

用户把这次界面放松和问题修复定为一次大更新，决定用 5.0.0、合成一个提交。没有不兼容改动：MCP 工具和插件协议没变，REST 只新增字段，迁移 0042 在升级时自动执行。

## 修复空闲计时、提醒和轮询（2026-11-04，5.0.0）

来自对 ServerPilot 的一次检查，复现都用临时库和假时钟。决定见 [空闲回收记录](teamwork/records/idle-lease-reclaim.md) 的 2026-11-04 条目。

改动：
- 新增 `idle_checked_at`（迁移 0042）。看不到卡不足 5 分钟暂停计时，达到 5 分钟或跨过睡眠重新计时；集群分配只补跨睡眠。
- 提醒：数据不明时不算恢复；保持期不跨过观测空档；从未触发的记录直接删除；同一条 30 分钟内只通知一次。
- “有卡可用”：离线超过 5 分钟的服务器不再让整组沉默，短暂失联和恢复采集后的 5 分钟内仍然等待。
- App：revision 变化不再单独触发全量刷新，可见 12 秒、不可见 60 秒，最小化和被遮挡也算不可见。工具栏只在真正断线时写“连接已中断”。断线时总览不再给肯定结论。新增 ⌘W、⌘M、⌘H、⌘, 和窗口菜单；节点行可用键盘操作。
- 清理未引用的代码和指向旧页面的文案，`prune` 和占卡对账失败现在写日志。

没有改的：revision 仍然每次探测都增加。真实环境里读数每次都在变，只比较内容省不下请求，所以限速放在 App。

验证：Python 和 Swift 全量测试、Ruff；复现脚本的合盖、先失败后成功、抖动场景都符合预期。未验证：真实合盖；窗口遮挡和菜单快捷键的实际操作；真实环境下的刷新次数；集群分配只在测试里验证过。

## 参考 Beszel 放松界面（2026-11-03，5.0.0）

用户看过样稿后选择保留总览和服务器两页、只做放松。这是第一步，只改界面，没有数据库迁移；磁盘采集是第二步。

改动：新增 `ResourceMetric` 按指标定阈值，取代散落的 70/90、85 和 80，GPU 利用率不再告警，`endpointHighPressure` 也不再计入 GPU。服务器表改单行 56 pt，数字和 8 pt 指标条并排，型号移到组标题，状态并入任务列；`ServerActivity` 让表格和总览方块用同一句话。新增“视图”面板和默认隐藏的 GPU 温度列，选择存在 `UserDefaults`。节点投影新增 `last_usage` 给 App 灰显上次读数，`usage` 对过期节点仍为空，MCP 的 `_node_view` 不带这个字段。主按钮改深色，“利用率低”和趋势线用固定蓝 `loadBlue`。

和计划的差异：总览方块保留了灰色小曲线。去掉它会让 `/utilization-trends` 的 `servers` 变成没人用的数据。

验证：Python 和 Swift 全量测试、Ruff；虚构夹具在真实窗口截图看过总览和服务器页（1024、1280、1440）、节点行和“视图”面板。`verify-macos-app.sh` 的 App 包检查通过，最后一步比对已安装后端，本机后端仍是 4.2.0、没有 `runtime_identity`，所以在更新本机环境前会失败。未验证：真实数据下的外观；“视图”面板里的勾选和排序只看过显示，没有实际点过；提高对比度模式。

## 本机 App 与后端完整更新（2026-11-02，5.0.0）

新增统一入口 `zsh desktop/update-macos-app.sh`。流程正常退出旧 GUI，构建并原地替换根目录唯一 App，从干净的临时源码目录重装正式 uv-tool 后端，显式重启归属本用户的 daemon，再正常退出更新中途重新打开的 GUI，启动并验证唯一的新实例。更新互斥锁防止两次更新互相覆盖；不会操作 GPU workload，也不会复制或覆盖活动数据库。

运行校验同时要求安装包与源码内容指纹一致、daemon 启动时冻结的 `build_id` 与源码一致、daemon PID 更新、App PID 更新且路径/构建标识正确。App 构建与校验共用重复副本检查，覆盖仓库嵌套位置、编号副本和 Applications；只清理已确认身份的旧包或链接，身份不明则失败。单独构建和安装不等于更新成功。

验证：相关 Python 测试分批共 88 项通过（其中新增构建身份、App 副本和更新流程测试 38 项），Ruff、zsh 语法检查、Swift 6/macOS 14 helper 类型检查通过。本机完整更新最终跑通，App 构建/签名/安装校验通过，App 与 daemon 均为 4.1.2 且新进程已确认，源码、安装和运行中的后端指纹一致；没有保留第二份 App。

再次拉取验证：同步到 `64b4262`（4.2.0），保留本机更新流程的未提交修复，相关 Python 117 项、Swift 87 项及 Ruff 通过。统一更新入口完整运行成功，App 与 daemon 已切换到 4.2.0 的新进程，安装和运行构建标识与源码一致，只有根目录这一份 App。原暂存区保留；本轮没有提交、推送或另发版本。

提交前整理：大批暂存差异来自旧快照，与当前工作文件相互抵消，没有真实删除；备份旧索引及补丁后，按当前文件整理暂存区。两份编号 identity 文件均被正式代码及 worktree 回归取代；旧 `test_routine_no_capacity.py` 的 9 项测试均依赖已删除的 MCP 契约，不能原样提交，三份文件均保存在本地 Git 目录备份中。将其中“未发送成功后重试收到缺卡拒绝”的有效场景移植到正式 MCP 测试，模块 32 项通过；全量 Python 781 项、全仓库 Ruff 和锁文件检查通过。清理后再次完整更新本机，App 与后端仍为 4.2.0，均已重启并通过新内容指纹和唯一实例校验。

现场试运行先后拦住两种情况，修正原因后重新运行：旧 `build/lib` 把源码已删除的模块带进安装包，改为干净临时源码打包；构建期间 GUI 被重新打开，改为末尾在同一 native helper 中正常退出并重新启动。两种情况均已补回归，失败时不会报告更新成功。未做 GPU 任务或远端 helper 部署验证；外部 MCP 客户端仍由用户自行重连。

## 方块按负载着色、服务器组分界、趋势图文字（2026-11-02，4.2.0）

改动：新增 `CardLoad`，方块改说负载（空闲、利用率低、接近满、利用率未知、有错误、数据过期），接近满的阈值是当前利用率或显存占用达到 80%，读数与服务器表相同。总览状态胶囊改用同一词表，因此不再单列运行中、已分配未运行、外部进程的数量，这些仍写在服务器旁的文字里。服务器页每组一张卡片。趋势图的文字移出绘图区。

验证：Swift 新增 `CardLoadTests`；用虚构 `dashboard` 夹具在真实窗口截图看过总览与服务器页。未验证：真实数据下的外观；瀚海节点在计算节点可观测后的颜色；Increase Contrast 下的黄色。

## 服务器趋势按时间桶正确求平均（2026-11-02，4.2.0）

根因：SQLAlchemy 的 `/` 在 SQLite 上生成浮点除法，原查询按小数桶分组，同一 3 分钟内不同秒的采样被拆开；查询后转为整数桶时，服务器结果又覆盖了此前采样，因此可能只显示最后一次读数。

改动：查询改用 `//`，直接在数据库中按整数时间桶聚合。服务器 3 分钟桶和全局 30 分钟桶使用同一分组逻辑；响应结构和数据库 schema 不变，无需迁移。

验证：新增 2 项固定时钟的回归测试，覆盖同桶不同秒读数求平均、相邻桶边界、服务器隔离、多卡、空值和全局均值。修复前 2 项均失败（应为 50%，分别返回 100% 和 80%）；修复后趋势测试 7 项及双语日志一致性检查 1 项通过，Ruff 和差异空白检查通过。仅使用临时数据库与虚构观测；未连接真实 GPU，未验证日常 daemon 的趋势刷新。

## 任务进程时间不再跨时钟比较（2026-11-02，4.2.0）

4.1.2 让 `process_started_at` 变成服务器时钟后，`last_process_observed_at` 查询仍拿它和本机时钟的 `lease.issued_at` 比较。实机上两台服务器的启动时间比旧算法早约 3 分 52 秒，由此推断时钟有偏差。这个值只读，不影响释放和回收。

改动：改比较 `first_seen_at`（入库时的本机时间）。已知边角：分配前刚启动、分配后才第一次被看到的进程会被算进来。

验证：新增服务器时钟偏慢、偏快两个测试。未验证：真实任务跑在有偏差的服务器上。

## 占卡不再误报归属冲突（2026-11-02，4.1.2）

根因：进程启动时间 = 探测开始时的本机时间 − `ps etimes`，探测比平时慢超过 2 秒就新建一条进程身份，新旧记录在 60 秒缺席宽限内同时有效，与占卡的期望身份对不上。后台重启会清 SSH 复用连接，第一次探测要完整握手，所以重启后最容易出现。修改前的一台实机上，同一占卡进程的一次重启记录为 39.723 → 37.607 → 39.677 秒。

改动：探测改用 `TZ=UTC LC_ALL=C ps -o pid=,user=,lstart=,comm=`，启动时间由服务器给出。2 秒容差保留，理由改为服务器调整时钟时 `lstart` 可能轻微移动（这是推断，未实测）。

验证：单元测试覆盖慢探测不改变启动时间、日期补空格、命令含空格、无法解析的行。本机 macOS 的真实 `ps` 输出解析正确。未验证：Linux 服务器上的真实输出（项目规则不主动连接真实 GPU），以及部署后归属冲突是否消失，需要升级日常环境后观察。

## App 界面重新整理（2026-11-02，4.1.0）

用户要求更高级、更可读，参考 Apple 的设计理念；选定方向 A（原生 macOS）吸收 B（数据仪表）和家庭 App 的状态胶囊、分组方块。中性底，暂不做深色，设计规定的修改已与合作者协商。界面契约见 `docs/DESIGN_SYSTEM.md`。

改动：总览（状态胶囊、容量环、24 小时趋势、需要处理、服务器方块）、服务器表（两行行高、逐卡方块、GPU 小曲线、单一滚动条、无每组 4 行限制）、任务页（一任务一卡，替换左右分栏）、菜单栏弹窗（替换 NSMenu）、悬浮半透明侧栏并列出服务器组、Slurm 节点状态中文化。新增 `GET /api/v2/utilization-trends` 与能力 `utilization_trends`，租约投影增加 `duration_declared`。删除 `ResizableSplitPane.swift`。

验证：Python 全量 735 项、Swift 82 项、Ruff、`mcp policy --check`、App 构建校验通过。趋势查询在一份真实库副本上约 75 毫秒，返回约 9 KB。用新增的虚构 `dashboard` 夹具（经真实服务层生成）截图看过总览、服务器、任务、提醒、设置和服务器详情，并验证 1024×640 与 1440×820 的折叠。

未验证：侧栏在真实窗口里的半透明效果（夹具截图用 `cacheDisplay`，画不出桌面透出；本会话也没有屏幕录制权限）；菜单栏弹窗只单独渲染过内容，没有从真实状态栏点开；连接真实控制面时的趋势刷新；键盘与 VoiceOver 逐项走查。方块上的「N 分钟后回收」未做：状态投影没有回收时间，目前只显示多久没有进程。

4.1.1：真实窗口里侧栏发灰，改用系统 `NavigationSplitView` 侧栏列；虚构夹具下用真实窗口截图看过宽、窄两种布局。红绿灯仍在面板上方，试过空 toolbar、清安全区、`NSHostingController` 托管都改不了面板位置。趋势图的中断是真实采集空档（Mac 休眠），数据层保持 null，画面用虚线和“未采集 N 小时”表示。未验证：侧栏在深色壁纸上的透出效果。

## 连接时不再杀 daemon（2026-10-31，4.1.0）

多个会话反复 `CONNECT_TIMEOUT`。每个 MCP 启动时探测 daemon（0.8 秒超时），没有回应就 `kickstart -k`。
daemon 重启要几秒，下一个连接又把启动中的进程杀掉，排队后超过客户端 30 秒。现在进程还在就只等待，
只有版本不符或进程不存在才替换。

## 同项目会话共享任务（2026-10-31，4.1.0）

上线后 Agent 反复失败。日志 201 次调用里 44 次失败：`lease_forbidden` 为 0，主要是旧缓存 schema
（`Unknown tool`、用 `lease_id` 归还）和 Claude Code auto mode 分类器（5 次）。ServerPilot 放宽：
同项目会话互相可操作，`gpu_release` 接受 `lease_id`，`task` 也可填租约 ID，worktree 归属其仓库。
分类器在 ServerPilot 之外，要在 Claude Code 权限里放行。

## MCP 收为三个工具（2026-10-31，4.0.1）

4.0 的七个工具是按操作拆的，不是按 Agent 的用法设计的。30 天用量：96% 的申请是 1–2 张卡，
`gpu_status` 约是申请的 3.5 倍，新工具调用极少。现在只有 `gpu_status`、`gpu_apply`、`gpu_release`：
申请是声明，重复即恢复，`change` 改卡数，`take_over` 接手，查询任务即心跳。

同服务器有卡运行时，空闲卡回收从 60 分钟改为 30 分钟，不受心跳保护。依据：一天多遥测里 141 次
空档中位 1 分钟，4% 超过 30 分钟，12% 超过 15 分钟。样本短，阈值在 `idle_card_reclaim_seconds`。
再次申请同样卡数可保留空闲卡，上限三个窗口。

契约 3,322 → 2,159 字符。

本机已部署。用全新 stdio MCP 客户端对真实 daemon 跑通申请、重复申请、卡数不同被拒、`change` 加减卡、
遥测、归还，无工作负载。接管老租约已在生产库由真实 Agent 完成。已知问题：Claude Code 重连后仍用
缓存的工具 schema，已开始的会话要开新会话或重连 MCP 才能看到三个工具；旧调用 `gpu_lease`、
`gpu_resize` 返回 Unknown tool，`gpu_status(lease_id=...)` 的多余参数被忽略且不算心跳。

## 4.0 Agent 与 App 重设计（2026-10-31 发布）

计划见 `docs/teamwork/plans/v4-agent-and-app-redesign.md`。P0–P6 已完成，经用户决定发布；下方未验证项随版本带出。

已完成：迁移 0041、按服务器判定回收、`keep_idle`、提醒引擎、七个 MCP 工具、全部路由迁到
`/api/v2`（v1 已删除，测试约束）、App 总览页、提醒页、菜单栏、系统通知、结果提示、pulse 轮询、
任务页显示持有者与逐卡状态、GPU 详情入口、Slurm 节点到服务器详情的入口、统一卡状态词表，
以及 `activate`、`renew`、`bind`、`bind-observed` 的删除。

在真实数据库副本上验证（不启动采集，不连服务器）：迁移后 API 正常，接管老租约成功。
响应字符数：`gpu_status` 1,537（其中多数是环境说明）、容量 322、`view=servers` 7,177、
`view=nodes` 最大 4,979、接管 383、心跳 262。状态读取约 19 毫秒，pulse 约 1 毫秒、54 字节。
夹具上的预算测试不覆盖真实长度的任务名和环境说明，所以摘要和 servers 的实际值高于夹具。

保留未删：`import-servers`、`history` 无使用证据但也无删除依据。`bind_observed_workload` 无路由
可达，但其测试守护仍作用于已有绑定记录的进程归属逻辑。

本机实机验证（2026-10-30，已切换到该构建，包装版本号仍是 3.1.0）：迁移在实时库完成，3,263 条
租约保留，6/6 在线，占卡正常。总览、服务器、任务、提醒页和 GPU 详情看过实际画面。第一条真实
“资源闲置”提醒自动产生并写入 `notified_at`。关窗后窗口数为 0、进程保留、状态项显示提醒数，
再次打开恢复单窗口。真实 stdio 下 `serverpilot-mcp` 握手、工具清单、项目解析、`gpu_status` 和
`if_changed` 通过。备份在 `~/Library/Application Support/ServerPilot/backups/`。

真实 MCP 客户端（Claude Code）在 `lab-04` 空闲卡上跑通申请、加卡、读遥测、还卡、归还，
无工作负载；归还的卡按 600 秒冷却恢复占卡。服务器表行可打开详情。

未验证：系统通知横幅是否真的弹出（投递已执行，授权结果未确认）；菜单栏的外观；Slurm 节点详情
入口；键盘、VoiceOver、缩放（已修一处：服务器行的辅助功能按下动作曾被吞掉，其余行未逐一核查）；
Cursor 的 clientInfo/roots；Slurm `exec_prefix` 与真实分配、进入、归还（`entry_verified=false`）。

已知差异：MCP 文本仍是带缩进的 JSON，实测比紧凑形式约长 30%。全局规则未安装，因为 Codex 和
Claude 的全局规则里本来就没有 ServerPilot 块，新契约经 MCP instructions 送达。

## 状态读取被插件发现拖慢（2026-10-30）

现象：桌面 App 反复显示「连接已中断 · 显示上次数据」。只读测量本机控制面，
`/api/v1/state` 在 0.3–19.6 秒间波动，App 的 6 秒超时频繁触发；连 `/health/live`
也会卡 1–9 秒，说明是整个进程停顿而非单个接口慢。进程采样显示事件循环线程约 46%
时间在等 GIL，另一工作线程大量执行 `stat` / `lstat`。

根因：插件 `info` 已缓存，但每次查找插件仍重新扫描插件目录并对每个路径 `resolve()`；
一次状态读取按 endpoint、GPU、连接多次查找，约 2,500 次文件系统调用。每次调用都释放
并重新争抢 GIL，有任何其他线程忙时每次最多等一个切换间隔。

修复：包目录只解析一次；探测缓存按列出的路径加 stat 的设备号与 inode 识别文件，
不再逐级 `resolve()`；`_read` / `_write` 内的一次领域操作只做一次插件发现，操作之外
每次查找仍看当前目录，运行中放入的插件仍能被发现。

验证（本机 DB 只读副本 + 本机插件文件，不连服务器）：一次状态读取的文件系统调用
约 2,500 → 23；在一个 CPU 忙线程竞争下 11.1 秒 → 1.35 秒，无竞争约 0.05 秒。
新增 `test_a_state_read_discovers_plugins_once`；Python 全量 699 项与 Ruff 通过。
剩余竞争开销来自 SQLite 查询本身，未在本轮处理。

第二处：升级本机 daemon 后仍有约 4% 的状态读取超过 6 秒。用 py-spy `--gil` 采样 3 分钟，
领域线程持有 GIL 28.8 秒，其中 10.3 秒是 `get_endpoint_keepalive_summary`：采集后每次占卡
对账在无需动作时都算一份完整汇总作为返回值，而采集路径丢弃这个返回值。现在对账不再返回
汇总，只有需要它的显式开关接口在对账后自己读取。

现场实测（本机 daemon，每 2 秒读取一次 `/api/v1/state`，约为 App 12 秒轮询的 6 倍负载，各 5 分钟）：

| | 修复前 | 仅插件发现修复 | 两处都修复 |
| --- | --- | --- | --- |
| 中位 | 1.30 秒（30 次） | 0.66 秒 | 0.39 秒 |
| 超过 6 秒 | 约 13–15% | 6/150 | 0/150 |
| 最长 | 19.6 秒 | 20.3 秒 | 5.7 秒 |

剩余热点为状态快照对每张 GPU 分别查询（`_keepalive_gpu_status`、`_node_observation_error`、
`_active_lease_for_gpu`），以及采集入库，未在本轮处理。

## 普通服务器与插件共用节点观测（2026-09-08）

本轮替代先前仅汇总「已领分配」的方案。普通 SSH 与插件计算节点共用固定 Linux/NVIDIA
探针、解析、持久化和资源投影；插件协议 v5 / 观测 schema 3。Slurm 只补充节点目录、
分配关系及作业 accounting。CPU、内存、GPU、显存与进程在 REST、CLI、MCP、桌面使用
同一节点观测；节点表显示当前值，作业 CPU 时间与 RSS 单独显示。

节点身份为 endpoint＋node_id，GPU 再按 UUID 定位；迁移 0040 保留普通服务器 GPU ID
及租约关系。节点局部失败保留资源身份与分配事实，指标显示未知；调度管理卡即使观测空闲
也不可绕过提供方申请。未采到计算节点时不展示登录节点的 CPU／内存代替。

已验证：普通／插件同输入解析一致，多节点同序号和 PID 隔离、CPU 差分与重启基线、
委托空闲卡不可直接分配、迁移保留资源与租约身份；额外 18 项相关测试通过。
Python 全量 695 项回归通过，额外节点／迁移 18 项通过（其中 3 项为新增）；Ruff、
MCP policy 与 diff 校验通过。桌面 75 项测试通过；实机发现并修正旧无障碍口径文案后，
再次构建与校验根目录唯一 App，并重启验收。

本机已部署：停止控制面并备份至 `state/backups/pre-node-v5.sqlite3`，更新 uv 工具安装和
本机插件后启动，迁移正常、health ready。14:50 的 API、CLI 和新启动的 MCP 进程均返回
百舸三个节点的实时观测，以及瀚海 gnode07 的 `host_key_rejected` 与空用量。桌面已核对
「资源指标：节点当前值」与外部作业行。已启动的旧 MCP 连接仍保留旧进程代码，需要客户端
重连才能使用新投影；新 MCP stdio 路径已实测。

现场依赖更新（14:54）：用户明确回复「允许」，已在登录节点的用户 known_hosts 登记此前
确认的 gnode07 ED25519 公钥，指纹 `SHA256:PO9d4bGGydmy9krRfpyYwUfgFjBx3nwSgdnB38jN4cc`。
严格主机校验已通过，实际 SSH 准入随后返回 `Permission denied (publickey,password)`。
只读检查发现登录节点 ~/.ssh 仅有 known_hosts，没有用户公钥凭据；未读取私钥或输入密码。
后台 API 已更新为 `auth_failed`，用量保持未知，作业 1158293 仍 active、gnode07、8 GPU。

官方登录手册说明账号密码和二次认证，并明确禁止 SSH 密钥登录；其计算节点适用范围尚需
管理员确认，不能据此自行新增账号密钥。当前需要计算节点认可的认证方式；主机密钥问题
已解决，账号准入仍未通过，因此真实 GPU 连续采样及各端真实用量验收仍未完成。
未创建 srun 步骤、未改变训练、未启动远端采集进程。

## 通用分配模型与插件 v5（2026-09-08）

状态：**实现完成；现场连接接管、重启与只读核对通过，真实申请／进入／归还路径待验证。**

- 正式分配表与连接意图表已迁移。普通主机保持内置 SSH 与逐卡分配；插件以能力声明接入。
  服务层统一拥有申请与归还，REST 不再编排插件申请；旧约束 JSON 释放引用迁入正式记录。
- 分配结果包含租约任务关联、远端引用、节点、已知 UUID／物理卡号／环境内编号、请求时长、
  实际截止时间、进入信息和确认状态。未知不填零；旧记录缺位置保持未确认。
- 申请前持久化稳定标识；远端响应丢失或本机租约写入失败时保留原申请，重试先只读核对。
  归还失败保留占有及待确认状态，不能通过本机到期时间假装远端已归还。
- 整份分配回收要求任务失联、完整且一致的新鲜空闲观测持续达到现有阈值；观测中断重置空闲证据。
  插件分配不能逐卡改派，不能本机续租延长提供方硬时限。外部同账号作业只展示、不接管。
- 插件 `info` 升至 v5，新增可选 `connect/disconnect`；认证只在 connect 内执行。
  连接维持意图持久化，认证失败与原因不明停止自动恢复，明确网络不可达等待网络恢复。
  未配置或旧版插件的发现错误可见。
- 容量、分配确认、设备活动各用各自采样时间；重新读取不能刷新旧数据。
  节点位于 REST endpoint.nodes，当前容量单独提供；断连仍可看上次位置。
- GUI 保留单张表、纵向分组、每组最多四行滚动。组标题显示连接状态与操作，节点可查看任务及
  外部作业，申请成功展示位置／截止／进入方式，未确认申请有可见入口。
  CLI `gpu apply` 显式传时长，`who` 展示分配；MCP 仍为五工具，gpu_apply 必填 duration_seconds。
  契约唯一源和生成政策已同步。

验证：Python 临时数据库与 fake provider 覆盖申请、幂等、失联、响应丢失、本机写失败、
整份回收、归还失败保留占有、旧引用迁移、外部作业、独立容量新鲜度与普通主机回归。
146 项接口／插件／MCP／keepalive 定向检查、60 项 Collector 回归及 3 项隔离 MCP 协议检查通过；
后续生命周期定向检查通过。Swift 73 项通过；Ruff、生成政策校验与根目录应用构建验证通过。

本机交付与现场证据：

- 控制面停止后创建 `state/backups/pre-provider-v4.sqlite3`，再安装源码并启动迁移。
  瀚海独立插件位于用户插件目录，站点配置只引用原凭据路径，没有复制凭据或依赖 Storyboard 代码。
- 显式开启 hanhai22-p22 连接返回 connected（复用现有会话）；daemon 重启后 enabled=true 保留。
  13:05 的只读采集为 ONLINE，18 个节点、53 张分区空闲卡、单节点最大块 8；此数字是当时观测，
  不代表之后仍有容量。旧虚构 GPU 行已在完整观测后退出设备账本。
- 同账号外部作业 1158293 的只读 Slurm 结果为 RUNNING、gnode07、8 GPU，设备映射和活动未知，
  控制面未绑定项目／租约、未进入或修改该作业。现场采用 TRES 字段，已在插件读取路径覆盖。
- 根目录唯一 `ServerPilot.app` 已重启并实机核对：已连接状态、节点表、四行滚动、列对齐正常。

仍未知：此前人工 SSH master 退出的底层原因；本轮未主动断开现有会话，因此未现场验证密码／
TOTP 重新认证。真实 Slurm 申请、进入已有分配和归还须在获准的专用测试分配上执行，
`entry_verified` 保持 false，不能宣称整条现场执行链路已经验证。

## GPU 分区节点列表与组内排序（2026-09-07）

保留原服务器页的单张表、纵向分组段头、44 pt 行与响应式列宽。Slurm 有在线节点明细时
只显示当前 GPU 分区的节点，去掉重复的登录入口「按需申请」行，该说明保留在组标题。
每组最多 4 行，短组按内容高度展示，长组显示纵向滚动条。
横向卡片方案已按用户纠正撤回。所有组的滚动内容与表头保留相同的滚动条留白以保持列对齐。

连接列统一为 `host:port`；Slurm 节点显示 `登录入口 host:port / 节点名`，没有虚构计算节点
SSH 端口。节点名在 endpoint 内唯一，原有 GPU UUID、租约与申请路径不变。
插件 `scheduler.nodes` 经校验、既有容量 JSON 持久化与 REST 原样传递到 Swift 模型。
未采集的节点压力指标为 `—`，维护节点不展示可申请数字。

排序统一作用于组内全部行，排序键由渲染行提供，避免“显示按需、却按分区总空闲排序”。
缺失指标不再映射成 -1，两个方向都置后；相同值按地址/节点名自然排序。
切换排序后组内回到顶部，刷新不会主动重置滚动。

验证与交付：

- 插件、插件分配、MCP 分组定向测试共 40 项通过；Swift 72 项通过（包含缺失值、自然地址顺序、
  同值稳定排序、IPv6 地址显示和节点解码）。Ruff 与 `git diff --check` 通过。
- 根目录 `ServerPilot.app` 已构建，`desktop/verify-macos-app.sh` 通过。
- 删除重复入口行后再次构建、校验并重开根目录 App；实机滚至组内末尾，只有节点行，
  「按需申请」仍显示在组标题。
- 已更新本机插件、安装当前控制面并重启 daemon；读取本机 REST 观察到 ONLINE、18 个真实 GPU 节点。
  未手动连接 GPU、未提交测试作业，真实数据来自既有后台采集。
- 根目录 App 实机点击验证：空闲降序为 7/8、6/8…，升序以 0/8 开始；普通主机的未知空闲行
  留在数字行后；GPU 利用率为 65%、1%、—。滚动条能到 1（列表末尾），切换排序回 0。
  这些数值仅记录排序检查时的观测，不作为当前容量承诺。

## 瀚海容量停在三卡（2026-09-07）

本轮读取本机 REST `/api/v1/state`，瀚海 `largest_allocatable_block=3`、
`free_gpu_count=12` 来自最后成功观测 `2026-09-04T14:26:30Z`，并非三卡配额。
当时最新尝试为 `2026-09-07T02:53:00Z`，错误链为
`cluster_free_gpu_count → parse_gres_used_indexes → parse_idx → int('N/A')`。
本机插件的 `max_gpus_per_lease` 实际为 8。

已修随包 Slurm 插件及本机 `hanhai22` 的远端解析器：索引为 `N/A` 时读取 Slurm
明确报告的占用数量，无法识别数量仍报错；删除只供旧测试使用的本地重复解析器。
控制面快照使用既有 `freshness.fresh` 判据，过期后不再投影 scheduler capacity，
因此组级最大可申请块为 `null`，MCP 不再展示旧容量。

验证：插件、插件分配与 MCP 分组三个测试文件共 40 项通过，Ruff 与 diff 检查通过；
本机插件远端 helper 已在本地验证并原地更新，未调用 SSH 或申请作业。
本机插件更新后，已有后台采集于 `2026-09-07T02:56:23Z` 恢复，随后读取本机 REST
确认 `ONLINE`、`free_gpu_count=57`、`largest_free_block=8`、错误清除，观测年龄 4.5 秒。
这证明采集修复已在真实路径生效；未提交作业，8 仅为 GPU 空闲块，不是立即分配保证。
控制面屏蔽过期容量的改动已随后续节点列表交付一并安装、重启；过期分支由定向测试覆盖。
Storyboard 的使用规则同时指出 CPU 余量和单节点作业形状会限制可用卡数；
本轮不把分散空卡等同于一次可申请容量，也未扩展排队、多节点或时限语义。

## 当前工作树：观测层重做（2026-09-03，已在本机运行并验证）

用户报告服务器反复显示「连接失败」、经常六台同时。根因是 `ssh_connect_timeout_seconds = 8`
同时充当 OpenSSH 的 `ConnectTimeout` 和整次观测的 wall-clock 上限，而实测一次成功观测均值
2.3–2.9 秒、尾部 7.8–12.8 秒；六个探测又在同一个 `asyncio.gather` 里以 `stagger_seconds=0.0`
同时发出，共享同一起点与同一条线，于是任何整体抖动让所有主机在同一轮越线。失败三轮后
`collector_endpoints_due` 还会把端点冻结 60 秒不再探测，机器恢复了界面仍显红。

已执行的改动：

- **传输**：`adapters.observation_ssh_argv` 是唯一的选项列表，加 `ControlMaster=auto` /
  `ControlPersist=300` / `ServerAliveInterval=5 × CountMax=3`，master 由第一次探测隐式打开，
  没有监督任务也没有重试包装；daemon 启动清空自己的 control 目录。control 路径必须加引号
  ——真实目录含 "Application Support" 的空格，不加引号 ssh 会把整条选项判为
  "extra arguments at end of line"，全机所有探测一起失败。
- **预算**：`config.SSHBudgets` 把连接（10s）与远端命令（20s）分开，`probe_deadline = 30s`；
  `config.stale_after_seconds(interval) = interval + 30` 成为唯一定义（原来是 `interval * 3`，
  比一次合法的慢探测还短）。不变式 `interval < master_dead(15) < probe_deadline(30) <
  stale_after(35)` 由 `test_budget_ordering` 锁住。
- **节奏**：删除整轮 `gather`、`collect_once`、`collector_endpoints_due` 与
  `DEGRADED_ENDPOINT_PROBE_SECONDS`；改为每端点一个 asyncio task，确定性起始相位加 ±10% 抖动。
  keepalive reconcile 仍是独立 task 并由 shutdown 等待，不能内联——否则关机取消探测循环会取消
  正在进行的占卡启动，留下一个占着卡却没有账本记录的 worker。
- **观测面**：`linux-nvidia` / `linux-host` / `server-script-v1` 收口为 `linux`；删除
  `_OBSERVATION_QUERIES` 与 `RAW_SSH_HOST_ONLY_QUERY`，`observe_endpoint` 只剩两臂
  （内置探测 / 插件 `observe`）。
- **错误**：`collector.classify_failure` 是唯一分类点，13 个闭合错误码写进
  `provider_states.last_error_code`；rc=255 且 stdout 含 `__SERVERPILOT_GPU__` 判 `remote_error`
  （255 既是 ssh 的失败码又是合法远端退出码）。桌面端改读该码，删掉子串匹配级联。
- **可达性**：新增 `serverpilot.freshness`，`Freshness` / `freshness_of` 成为唯一所有者；
  `endpoint_reachability`、`_endpoint_is_unreachable`、snapshot 的 monitor 推导都读它。
  快照 `monitor` 新增 `error_code`、`last_success_age_seconds`、`stale_after_seconds`。
- **生命周期与日志**：`_daemon_executable` 只解析 uv tool 那一份（删除 PATH/sys.executable
  候选与 `SERVERPILOT_DAEMON_EXECUTABLE`）；install 会删除旧 `local.gpu-broker.daemon.plist`
  而不只是 bootout；`Database.migrate()` 永远用包内 migrations（不再优先 cwd 祖先的源码树）；
  新增 `logging_setup.py`，日志带 UTC 时间、PID、endpoint 名并轮转。
- 迁移 `20260903_0035`：改写 `endpoints.observation_profile` 为 `linux`，新增
  `provider_states.last_error_code`（不回填猜测的码）。

**本机实测（同一台机器，改前 / 改后）**：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 单次观测耗时（`collected_at - observed_at`） | 均值 2.27–2.90s，最大 7.8–12.8s | 均值 0.15–0.37s，最大 0.83s |
| 采集失败 | 过去 48h 内 100% 是 `TimeoutError ... after 8 seconds`，峰值 121 次/小时 | 新构建运行期间 0 次 |
| 同一 10 秒窗口 ≥3 台同时失败 | 约 15 次/小时 | 0 次 |
| 日志噪声（`No such revision` / `Errno 48` / instance-id 不匹配 / `Unknown child process`） | 462 / 462 / 373 / 12 | 0 / 0 / 0 / 0 |
| 复用连接 | 无（每 5 秒每台一次完整握手） | 6 个 master socket，6 个 `ssh [mux]` 进程 |

`hanhai22-p22` 仍是 `plugin_error`，这是既有且真实的情况：该集群不能用密钥、每次登录要一个
一次性验证码，插件按设计复用人工打开的 ControlMaster，需要人工先 `ssh -fN hanhai22`。

## 当前功能

1. **信息采集**：固定只读探针采集服务器 CPU、内存、GPU、进程和历史趋势；APP 刷新只读取这份状态。共享集群通过本机插件的 `observe` / `apply` / `release` 接入，随包参考插件为 `slurm-immediate`，没有独立的调度器提交面。GPU 与主机资源都投影最近 10 分钟均值，endpoint 快照在 `host_telemetry.recent_average` 提供标准化 CPU 负载和内存占用率，详情页的逐卡显存环图仍以当前观测绘制。
2. **人类监控与纠错**：macOS 桌面 App 展示服务器分组、任务与 GPU；Windows 上没有桌面 App，CLI 和 MCP 仍可用。没有浏览器界面，也不再提供 `/ui/*` 页面。分组携带共享工作目录、环境说明和数据/权重说明；成员可继承或覆盖工作目录。环境说明只供阅读，不会被执行或注入。App 刷新只读取本机 daemon 已提交快照。保持卡数不变并选择新 GPU，以及释放已确认空闲占用，只在 macOS App 与 REST operator 路径上提供。目标 GPU 正在占卡时，macOS / REST 改派先按卡停止并刷新确认，再更新分配，随后提示对应 Agent 按返回的 `CUDA_VISIBLE_DEVICES` 重启任务。
3. **Agent 操作**：源码日常 MCP 正好五个工具：`gpu_status`、`gpu_apply`、`gpu_release`、`gpu_add_server`、`gpu_update_server`。申请签名为 `gpu_apply(duration_seconds, server_group_id?, server_id?, gpu_count=1, task?)`：`gpu_count` 来自启动脚本或配置中的任务并行度，安全默认 1，不从空闲容量推断；已分组裸机先选定组，再在组内 best-fit 一台主机，一份租约始终只落在一台机器上；插件接入的分组同样可以直接以 `server_group_id` 申请，由 `service.claim_provider` 在该组恰好一个声明了 `apply` 的成员上落地，`server_id` 仍用于未分组主机。`gpu_add_server` 登记主机，`gpu_update_server` 更新安全元数据；删除不是 MCP 工具。`gpu_status` 按 组 → 服务器 → SKU 投影可申请容量（`name`、`vram_mib`、`total_count`、`available_count`），不是逐张空闲卡菜单，不含遥测与 `keepalive`；连接与远端工作目录随该服务器投影一次。插件接入的集群不再有平行的 `scheduler_servers` 顶层桶：它的登录节点以同一个 `{server_id, workspace_path, workspace, ssh, gpus[]}` 形状挂在所属分组内，未知的 `total_count` 省略而不是填 `null`，且不再同时出现在 `cpu_only_servers`。没有逐卡 GPU 行的分组也会被投影，因此手工设置的组名与共享工作目录不会丢失。每个分组另投影 `allocation`（`direct` / `delegated`）、`limits`（`max_gpus_per_lease`、`max_lease_seconds`、`lease_ends`、`cpu_cores_per_gpu`、`memory_mib_per_gpu`、`apply_max_seconds`、`queues`）和 `largest_allocatable_block`。`largest_allocatable_block` 的语义固定为**一次申请能拿到的最大卡数**：委托型取插件上报的单节点最大空闲块，缺失时只在同时已知单次上限时回退为 `min(free_gpu_count, max_gpus_per_lease)`，两者都未知时为 `null`，绝不用跨节点池子总数冒充；直连型取组内单台主机的最大可申请卡数。默认响应附带紧凑 `busy_gpus`（`server_id`、`gpu_id`、`index`、`status`、人类可读 `task`），占用情况不需要第二次调用，`server_id` 可将响应收窄到一台服务器。申请成功后的 `gpus[]` 仍按卡返回，每行只保留 `server_id` 外键和该卡自身字段。遥测只随租约投影：`gpu_status(lease_id=…)` 返回 `leased_gpus` 逐卡 `recent_average` 与 `current`，以及 `lease` 汇总（`telemetry_gpu_count`、`min_memory_free_mib`、多卡时的 `gpu_utilization_spread_pct` 与 `slowest_gpu`）。近 10 分钟均值的时间窗描述由 `lease.telemetry_window` 承担，逐卡窗口与其他卡不一致时才落到该卡的 `window_override`，部分失败的采集不会被投影成一个共享窗口；`lease_id` 已释放、被回收或不在本次收窄范围内时返回 `no_leased_gpus`。`gpu_release` 回显被结算的 `lease_id` 与 `state`。连接、工作目录、代码位置和设备选择仍分开投影：`ssh` 只负责连接；`workspace {path, kind=working_directory, use_as_cwd=true, code_location=not_provided}` 明确远端 cwd 且不暗示代码路径；`workspace_path` 继续保留。GPU UUID 只作物理身份，`gpu_index` 保留采集时的 `nvidia-smi index`；collector schema v2 另按 PCI bus 顺序生成 `cuda_ordinal`，lease 返回 `cuda_device_order=PCI_BUS_ID`、顶层完整 `cuda_visible_devices` ordinal 集合和逐卡 `gpu_cuda_visible_devices` ordinal。数据库中没有当前 `cuda_ordinal` 的 GPU 不参与分配。启动前做最小 CUDA gate，失败立即释放并在当前任务内避开同一环境。租约持续到显式释放、macOS App / REST 人工改派或释放空闲占用，或被两阶段空闲回收收回；容量不足返回 `no_capacity`，未选定合适分组时返回 `group_selection_required`，两者都不排队且同一 turn 不反复轮询。空闲回收只依据观测且**按卡粒度**：每张卡各自计时，某张卡在**新鲜**采集下持续没有计算进程时单独被收回，同一租约中仍在工作的卡不受影响；全部卡都被收回时租约转 `EXPIRED_EMPTY`——但**只要这份租约在任何一张卡上都没有当前进程，收回任何一张卡都还要求持有者本身已经沉默**。`_lease_liveness_age_seconds` 是控制面唯一的存活判据：取该租约各活跃 GPU 上任意进程观察的最后 `last_seen_at`（不看该行的 `active` 标志）与 `lease.last_heartbeat_at` 的较晚者；租约在任何一张卡上都没有当前进程、且在 `idle_lease_reclaim_seconds` 内还有任一存活证据时，只告警不收回——因为「此刻每张卡都没有进程」正是分阶段任务两批之间的样子。这道门覆盖**每一张**卡，不只是最后一张：一张卡的遥测抖一次，或某个阶段只用了这份租约的一部分卡，各卡的空闲计时就会错开，只在「全部卡同时到期」时才生效的门恰好挡不住这些情形。心跳不需要 Agent 新增调用：MCP `gpu_status(lease_id=…)` 经 `POST /api/v1/routine/leases/{lease_id}/heartbeat` 刷新 `last_heartbeat_at`，该路由不写审计、不 bump revision，对已释放或不属于调用者的租约返回 `recorded: false` 而非报错。原先「声明了 duration 的租约豁免观测空闲回收」已被这条判据取代：routine claim 固定清空 `expires_at`，那条豁免按构造永远轮不到最需要它的租约。**这对 CLI / App 路径是可见的行为变化**——声明了时长的租约此前完全不参与观测空闲回收，现在与其他租约同一套判据，卡持续空闲且持有者连续沉默超过回收窗口时同样会被收回；声明的时长仍然决定租约何时到期。租约里**还有卡在跑进程**时这道门不生效：那时闲着的卡就是这份租约用不上的卡，仍按卡收回，一个仍在跑的进程护不住同租约其余空闲的卡。当租约的全部 GPU 持续空闲，超过 `idle_lease_alert_seconds`（默认 600s）记 `idle_lease` 警告告警，超过 `idle_lease_reclaim_seconds`（默认 3600s）且持有者同样沉默时以 `EXPIRED_EMPTY` 释放并写审计 `lease.idle_reclaimed`。出现进程或采集变 stale 都会清零 `lease_resources.idle_since`，因此计时始终是一段完整观测到的空闲窗口，采集中断不会累积成回收。一次 MCP 调用具有内部重放键并在本地传输失败时只重试一次；新的同参数调用仍能取得第二个 lease，多个 lease 由申请者逐个确认释放。
4. **空闲 GPU 占卡**：明确分开持久意图与当前进程状态。endpoint 的 `desired` 只有 `ON / OFF`，只随用户开关改变；逐卡 `actual` 只有 `ON / OFF / ERROR`，由 helper 操作与新鲜采集更新。内部逐卡归属不再使用 TTL，并持久保存唯一的 collector PID、boot ID 和进程启动时间；远端 helper 本地状态保存其 PID namespace 内的 PID、Linux boot ID、`/proc` 启动时钟和固定 worker marker，停止前使用 pidfd 重新校验并发信号，PID 重用或 marker 不匹配时绝不 kill。恢复时 helper 先确认该 namespace worker 仍是自己，再以固定 NVIDIA 查询证明目标 UUID 恰有一个 driver-visible PID；ServerPilot 只在该 driver PID 与 boot ID 同新鲜 collector 的唯一进程一致时重新登记 worker，且以 collector 的启动时间写入持久状态。helper namespace 的 ticks 不被伪装为 host PID 的启动身份；当前 collector schema v2 尚未提供可端到端比较的 host ticks。PID-only 或旧 marker 状态直接 fail closed，不提供旧版收养路径。额外或替代业务进程为 `ERROR/CONFLICT` 并 fail closed。helper 状态文件和数据库备份都通过同目录临时文件、fsync 与原子替换发布。

一等分组持久保存共享 `workspace_path`、环境说明和数据/权重说明。未分组 endpoint 仍必须自带绝对远端路径；已分组 endpoint 可继承组默认工作目录，或按 endpoint 覆盖。新增或更新服务器时 REST、五个 MCP 工具中的登记/更新工具和原生 APP 都要求最终能解析出绝对远端路径；`gpu_status` / `gpu_apply` 在不改变持久化模型的前提下将生效路径投影为结构化 `workspace`，明确这是远端 cwd、不是代码路径。历史记录迁移保留且未知路径保持空值，不猜测项目子目录。该字段只是元数据和操作指引，不创建/删除远端目录、不授权启动 workload；环境说明只供阅读，不会被执行或注入。密封占卡 helper 固定布局为 `${workspace_path}/serverpilot-keepalive`（此处为生效工作目录），adapter 先只读执行 `./serverpilot-keepalive --protocol-info`，确认 v3 和 `worker_attestation` 能力后再执行固定启停命令。身份恢复时只读执行固定 `--inspect --schema-version 3`，不依赖远端 `PATH`，也不允许 caller 传入 PID、路径、命令或参数；预检失败不发送 mutation，旧 v2 wire/state 直接 fail closed。

占卡 GPU 对 APP、REST 和 MCP 仍计为可用；`desired=ON, actual=OFF` 时 GPU 空闲则仍可申请，同时下一轮按策略重新启动 helper。真正分配前，Agent 申请和 macOS App / REST 人工改派都复用同一个「选中 GPU → 逐卡停止 helper → 定向采集 → 结束占卡记录 → 普通申请或改派」实现。

loopback 控制面不使用登录 token：没有 token model、登录页面、签发接口或撤销接口。服务器永久删除只在 REST `DELETE /api/v1/endpoints/{id}` 与桌面 App；它故意不是 MCP 工具。旧 daemon 缺少 `endpoint_delete` 时会被替换为内置后端。删除会写入墓碑，YAML 清单在重启或 `sync_inventory` 时不会把已删服务器复活；用户显式重新添加同一 endpoint 时清除墓碑。有进行中租约时 fail closed，且不停止远端进程。服务器没有停用/排空这一轴：`endpoints.lifecycle_state`、`endpoints.enabled` 与 `gpu_devices.enabled` 已随写它们的方法一并删除，`monitor.status` 与 `gpu.state` 不再有 `DISABLED` / `DRAINING`。一次申请只留一份身份：`allocation_requests` 已删除，`task_ref`、`purpose`、`constraints_json`、`duration_seconds` 四个字段随 `20260903_0037` 迁到 `leases` 上并回填，`leases.request_id` 一并去掉；从来没有一行请求进过 `QUEUED`，`GET/POST /api/v1/requests`、`POST /api/v1/requests/{id}/cancel`、`request` CLI 命令组和 `GET /metrics` 因此一并删除，快照与控制面状态不再有 `requests`，`no_capacity` 改为在 `details.excluded` 里按原因给出计数。`request.created` 等历史审计事件保留。当前源码另有 `20260828_0033`，去掉调度器、规划与预设表，以及 `allocation_requests.profile_id`；本文件不把它记为已在运行库上执行。升级迁移不删除已有 token 表、退役服务器、占卡租约或 lease resource。

占卡链路没有校验摘要、自动重试、退避器、第二套定时器、自动抢占或整机占卡状态机；唯一的身份证明是固定 helper 对自身 v3 state 的只读检查，并且必须与已有 collector 观测一致，不能用于收养任意进程。

项目明确要求的资源正确性边界仍保留：过期采集不能被当成可用 GPU，Agent 只能使用实际返回的 lease 资源。这两项来自当前项目合同，不新增状态机。

Agent 合同现已明确限定作用域：ServerPilot 只协调 GPU，禁止绕过的对象是 GPU 发现、选卡、申请和释放；已获得当前授权 endpoint 的 Git 同步、文件维护与只读环境检查不需要 GPU lease。`workspace_path` 仍只是元数据，不提供远端 shell 或额外授权。`Transport closed` 与 `no_capacity` 分开处理，前者最多重试一次；同一任务内的 CUDA 初始化失败不会立即重试同一 server。

## 已完成验证

### 本次工作树（实机验证，2026-08-28 夜）

针对「申请不到卡 / 流程卡顿 / 申请很慢」的定位与修复，证据取自本机运行的控制面（6 endpoint / 26 GPU / 2 分组）。

语义变化：

- **占卡状态以采集新鲜度为第一条规则。** 看不见一张卡就无法确认它上面的占卡进程还在，`actual` 因此报 `ERROR` 而不是回放上一次记录值。状态页、endpoint 汇总和分配器现在共用 `_keepalive_gpu_status` 这一处实现（此前 snapshot 内另有一份手写拷贝 `derive_keepalive`，且已经对「有进程但无占卡记录」给出不同答案）。snapshot 因此少了一次 `KeepaliveCurrent` 预取，`session.get` 命中 identity map；只读副本实测 `snapshot()` p50 从 8.3ms 降到 7.5ms。
- **过期的占卡不再是可回收容量。** `_eligible_gpus` 此前算出 GPU state 却在 keepalive 分支丢弃它，于是失联主机上的卡进入 reclaim 候选；`_select_resources` 的 best-fit 在同为 8 卡时按 endpoint id 排序，恰好优先选中那台失联的机器，申请在 SSH 超时上耗尽预算后以 503 结束，且因为是 `raise` 而非 `return None`，插件兜底根本不执行。
- **一个 GPU UUID 只归属一个 endpoint。** 容器换转发端口重新登记会为同一张物理卡留下第二份记录。`ingest_observation` 现在按「黏性在位者 + 过期移交」收敛归属，失主的占卡租约在同一轮释放；分配器另有 `uuid_leased_elsewhere` 这条 fail-closed 过滤。不合并 endpoint，只裁定物理卡归属。
- **警告不再比它描述的对象活得久。** `idle_lease` 并入 lease 作用域告警类型，孤儿 endpoint / GPU 告警在 `_reconcile_leases` 每轮清理，删除服务器时即时关闭。
- **采集节奏与退避。** 循环改为固定间隙（此前 `max(0.25, interval - elapsed)` 在慢周期下退化为背靠背连续采集）；持续失败的 endpoint 退到 `DEGRADED_ENDPOINT_PROBE_SECONDS`（60s）重试，第一次成功即回到每轮。退避只影响探测节奏，不改变准入——它已经通过 stale telemetry fail closed；省下的是每轮一次失败写入与 revision 自增，那会让每个读者都以为有变化。间隔取得接近采集周期，好让刚被人修好的主机自己回来，不需要另开一个即时重探入口。扇出常数删除：一轮的时长是最慢的一台，不是总和。
- **阻塞调用离开事件循环。** 插件 `info` 的结论按文件内容缓存（实测单次调用 37ms → 0.11ms）；`observe` / `apply` / `release` 经 `asyncio.to_thread`；协程内的领域调用经 `BrokerService.in_domain`（单 worker，即 `/health/ready` 已公布的 `single_writer`）。`tests/test_package_layout.py` 有静态护栏禁止协程里裸调 `service.` 或插件动词。
- **申请预算由服务端决定。** 插件声明的 `apply_max_seconds` 成为强制超时，直连分组投影推导出的预算，客户端不再按 `gpu_count` 估算；读超时不再重放申请（请求已送达，重放只会把等待翻倍），只有确证未送达的连接失败才重试一次。`/api/v1/routine/claims` 与 `/api/v1/claims` 一样强制 idempotency key。

实机证据（同一台机器，改前 / 改后）：

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 事件循环延迟探针（不存在路由，45s 采样） | p99 458ms，max 1922ms，>1s 停顿 5 次 | p99 5.0ms，max 56ms，>1s 停顿 0 次 |
| `GET /api/v1/state` | 1.0–1.3s | 0.04s |
| `GET /api/v1/gpus` | 1.70s | 0.05s |
| `GET /api/v1/endpoints` | 1.52s | 0.05s |
| 活跃告警 | 23 条（21 条指向已不存在的资源） | 2 条，均为真实失联 endpoint |
| 失联主机 `server-lab-01-example-test-p4725` 的占卡投影 | `actual=ON`，8 张卡进入 reclaim 候选 | `actual=ERROR`，`available_count=0`，在 `busy_gpus` 中标为 `unreachable` |
| `gpu_apply(server_group_id="lab-group", gpu_count=8)` | 挑中失联主机后 503 | 约 17 秒成功，落在仍在应答的 `server-lab-02-example-test-p4580`，随后正常释放 |

Python 全量测试 `616 passed`，Ruff 通过。未改数据库 schema，无新迁移：所有判定都从既有列（`GPUDevice.present/last_seen_at`、`ProviderState.last_success_at/last_attempt_at`、`TelemetryCurrent.observed_at`）派生。

第二轮（契约与归因，同夜）：

- **两个服务器管理工具恢复可调用。** `_require_endpoint_admin_contract` 删除：它校验 `approval_ref` 非空后即丢弃，服务端从不消费。`gpu_add_server` 必填参数从 6 个降到 `[project_id, host, workspace_path]`，`gpu_update_server` 从 4 个降到 `[server_id]`，与 instructions 首次一致（`approval_ref` / `idempotency_key` 正是 `tests/test_agent_policy.py` 断言不得出现在 instructions 里的概念）。replay key 改由工具自己生成。保留必填 `project_id`：owner 为空会让 `has_unowned_endpoints` 在下次 `initialize` 触发 `_upsert_inventory`，用 YAML 回滚用户在 App 上改过的字段。重复登记由 `endpoint_exists` / `endpoint_address_exists` 两条 409 挡住——那从来不是 idempotency key 的职责。
- **两个 broker client 收敛到 `parse_broker_response`。** 此前 `error.details` 只有异步侧携带，同步侧静默丢弃，于是 `group_selection_required` 经 MCP 到达时带着候选组、经 CLI 到达时被剥空。
- **租约遥测按持有期夹紧。** snapshot 为 workload 租约的卡新增 `telemetry.lease_recent_average`，样本下界取 `lease.issued_at`；MCP 的 `leased_gpus[].recent_average` 与 `lease.telemetry_window` 改读它。未夹紧的 `recent_average` 不动——它描述这张卡本身，是 App 消费的另一个事实。样本不足为 `null`，`current` 仍可读。
- **lease 投影新增只读 `last_process_observed_at`**：这些卡上最后一次运行计算进程的时间，**下界取 `process_started_at >= lease.issued_at`**——只有在你拿到卡之后启动的进程才可能是你的。用 `last_seen_at` 做下界挡不住一个跨越分配时刻仍在被观测的旧进程。它不参与任何放行判断。

**服务端门槛：先前否决的是判据，不是门槛本身。** release-empty 最初提出的门槛建立在 `idle_since` 上，被否决的理由成立且仍然成立：`_reconcile_idle_lease` 只在 `kind != "keepalive"` 且 state ∈ {HELD, ACTIVE} 时更新它，故 keepalive 与 CONFLICT 租约的该值恒为 `None` 或冻结，任何基于它的阈值都会让这条人工恢复路径在自己的目标场景上永久 409。p8908 事故（`docs/teamwork/records/keepalive-workload-collision-p8908.md`）随后给出了相反方向的证据：没有门槛时，一个健康的 8 卡租约在两批 shard 之间被人点掉，8 秒后被占卡程序占满显存。现在的门槛换的是判据而不是阈值——用「租约存活年龄」（该租约各 GPU 上最后一次进程观察与 `lease.last_heartbeat_at` 二者取晚，再与 `now` 相减），它对 CONFLICT 租约同样有定义，且 keepalive 租约与已失联端点两种情形豁免这道**存活窗口**，因此原否决意见指出的永久 409 不会发生。豁免的只有存活窗口：keepalive 租约仍然要求它的每张卡当前没有进程，否则以 `conflict_process_present` 拒绝——清掉一个 worker 仍在跑的占卡租约，会把占了 80% 显存的 worker 留在卡上而没有任何租约指向它，停卡计划也再找不到它。已失联端点是唯一连这层证明也给不出的情形（机器已经不答，无法证明卡是空的），沿用原有的整体豁免。判据只有一份：`_manual_release_verdict` 同时供 release-empty 放行与 snapshot 的 `manual_release` 投影使用，`blocked_reason` 与该路径会抛的错误码同名，因此 App 里灰掉的按钮与点下去会得到的 409 是同一个词。App 不再自己按 GPU 状态推断可释放性：`LeaseRecord.manualRelease` 缺字段即视为不可释放。

**桌面端**：`confirmEmptyLeaseCleanup` 的确认框展示 `last_process_observed_at`（相对时间，或「从未观测到」）。刻意不设「多久以内算危险」的阈值——那等于在客户端重建一套放行标准，而这条路径存在的意义正是救那些没有标准可依的租约。点击层数不变，紧急恢复不因此变难。`desktop/Fixtures/maintenance.json`（8 秒前，复刻事故形态）与 `conflict.json`（两小时前）覆盖另外两个文案分支。`zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 均通过。（当时 Swift 单元测试因缺 Xcode 的 `XCTest` 无法在本机运行；见下文第三轮，已迁到 swift-testing 并在本机跑通。）

**已知缺陷（未修，本轮后单独做）**：`reassign_lease_gpus` 中途换卡不更新 `lease.issued_at`，`LeaseResource` 也没有"这张卡何时加入本租约"的列，故换卡后 `lease_recent_average` 的夹紧退化为无效。修它需要新增 `LeaseResource.assigned_at` 列与 migration，届时 `last_process_observed_at` 的下界一并改为 `max(issued_at, assigned_at)`。只在人工换卡时发生。

第二轮实机复验：`gpu_apply` 3.6s；申请后 `recent_average: null`、`current` 可读、`telemetry_window` 不发布假窗口；`open_leases` 含自己；两个管理工具 required 正确；`group_selection_required` 携带候选组。Python 全量测试 `616 passed`。


第三轮（失联主机的死锁，同夜）：

- **`_endpoint_is_unreachable`（`service.py`）是新增的唯一判据。** 取 `ProviderState(provider="raw-ssh", endpoint_id)`：无记录或 `last_error is None` 即为可达；`last_success_at` 为空视为失联；否则 `now - last_success_at > collector.stale_after_seconds`。判据客观、只读既有列，无新迁移。
- **`release_empty_conflicted_lease` 补全了它缺的另一半语义。** 它原本只实现「证明卡是空的」，撞上「已经无法证明任何事」就永久 503 `conflict_observation_failed`：采集失败 → 拿不到新鲜度证明 → 租约释放不掉 → `delete_endpoint` 撞 409 `endpoint_has_active_leases` → 服务器删不掉 → 卡永久锁死在账本里。每条恢复路径都向那台已经不存在的机器要证据。失联时跳过新鲜度证明与逐卡进程检查（`for gpu in [] if unreachable else gpus`），`release_reason` 记为 `server unreachable; operator settled the ledger`。
- **`delete_endpoint` 同理：失联时 409 让位，并调 `_settle_leases_for_deleted_endpoint` 结算残留租约**（`release_reason = "server deleted while unreachable"`），审计 summary 带 `unreachable` 与 `settled_lease_ids`，不留指向已删 endpoint 的悬空租约。该查询覆盖全部情形：`LeaseEndpointCommitment` 只与同一批 GPU 的 `LeaseResource` 同时写入，不存在只有 commitment 没有卡行的活跃租约。

**没有加 override 参数或 `force` 开关。** 这正是第二轮评审否掉的「加门槛再加旁路」：判据由客观事实（采集确实失败且已过期）决定，动作本来就只能由人从 App/REST 发起。三档语义因此是完整的一张表，不是两条路径：采集成功且卡上有进程 → 拒绝（fail closed，不变）；采集成功且卡上无进程 → 释放（不变）；采集失败且已失联 → 允许人结算账本。

**fail closed 在这里保护不了任何东西。** ServerPilot 从不停用户 workload，更停不了一个够不着的；一条过期的账本记录也拦不住别人使用那张卡。它唯一的效果是把卡锁死在自己账本里。

测试钉住两个方向（`tests/test_service.py`）：主机**仍在应答**时照旧 `conflict_observation_stale` / `endpoint_has_active_leases`（一次网络抖动清不掉活主机上的租约），失联后才放行并写入对应 `release_reason`。

**桌面单元测试改用 swift-testing，本机不再需要 Xcode。** 62 个用例从 XCTest 迁到 `@Test`/`#expect`/`#require`，`desktop/build-macos-app.sh test` 从 `xcode-select -p` 推导 `-F .../Library/Developer/Frameworks` 与 rpath，CommandLineTools 环境下即可运行（装了 Xcode 的机器同样适用）。这条取代了上文「本机无法运行 Swift 单元测试」的说明——`testLeaseRecordCarriesWhenItLastRanSomething` 已在本机跑通。迁移中暴露一个真实问题：XCTest 串行执行同一个 class，swift-testing 默认并行，共享 `StateRouteURLProtocol` 静态状态的用例会互相踩；两个 suite 因此显式标注 `@Suite(.serialized)`，而不是依赖隐式执行顺序。

第三轮实机复验（现网控制面）：p4725 的 8 笔遗留占卡租约全部结算、endpoint 删除，账本从 26 张卡收敛到真实存在的 18 张，指向 p4725 的残留 GPU 行为 0，孤儿告警随之消失。Python 全量测试 `618 passed`，Ruff 通过；`zsh desktop/build-macos-app.sh test` 报告 `62 tests in 2 suites passed`。未改数据库 schema，无新迁移。


第四轮（登记一台服务器为什么造不出能用的机器）：

起因是一条现网记录：另一个 Agent 线程登记的 `server-lab-01-example-test-p7770` 在 App 里是红的，`observation_profile=server-script-v1`、`server_group_id=null`、`keepalive_policy=disabled`，`ProviderState.last_success_at` 为 `null`（`Host key verification failed`，`known_hosts` 里没有 `[lab-01.example.test]:7770`）。对照 `EndpointCreate` 与 MCP 工具签名后确认：**四个字段里有三个不是调用方选的。**

- **`server_group_id` 在 MCP 上根本不存在。** `EndpointCreate` 与 `EndpointUpdate` 一直带这个字段，`gpu_add_server` / `gpu_update_server` 两个工具都不暴露它。分组决定这台机器能否被 `gpu_apply(server_group_id=...)` 选中，所以经 MCP 登记的服务器**必然**是不可分配的；补齐前 Agent 无论怎么调都造不出一台能用的机器。两个工具现在都接受它。
- **`observation_profile` 的默认值本身是错的，而且有两个。** `config.py:EndpointConfig` 默认 `linux-nvidia`，`schemas.py:EndpointCreate` 默认 `server-script-v1` —— 同一字段两个默认值，取决于从 YAML 还是 REST/MCP 进来。后者指向「远端自带采集脚本」这个最特殊的契约，用在裸机上连得上却读不到卡。统一为 `linux-nvidia`；profile 的选择说明从平铺枚举改为「哪种机器选哪个」，放在参数 `Field(description=...)` 上而不是全局 instructions（后者每轮都要付费，且 2400 字符上限未动）。
- **`keepalive_policy` 刻意不暴露。** 它必须与 `keepalive_adapter_id` 成对设置（现网四台 keepalive 主机是 `policy=idle_keepalive` + `adapter=server-script-v1`，而它们的 `observation_profile` 是 `linux-nvidia`，两者不可互相推导）。把两个耦合字段交给 Agent 正是本轮缺陷的形状；keepalive 仍是人在 App 上设的策略，不影响 apply/release。

**登记即验证。** `POST /api/v1/endpoints` 改为 `async`，创建后对该 endpoint 做一次采集，再返回新增的只读 `observation`（`observed` / `observed_at` / `gpu_count` / `error`，由 `service.endpoint_reachability` 从既有 `ProviderState` 与 `GPUDevice` 派生，无新列、无新迁移）。放在 REST 而不是 MCP 工具里，App、CLI、MCP 因此共用同一条路径。

**失败不回滚。** endpoint 保留：它是操作者「我要用这台机器」的记录，host key 或端口是人自己修的，修好后下一轮采集自己接上，不需要第二次登记；回滚还会与 idempotency 重放相互作用。

**两个客户端的等待时间随之改为按服务端预算。** 登记现在要等一次采集（插件 `PLUGIN_OBSERVE_TIMEOUT_SECONDS=45`，直连一个 SSH connect timeout），超过 `CONTROL_PLANE_READ_TIMEOUT_SECONDS=20`。`client.py` 的 `_CONTROL_PLANE_CLAIM_PATHS` 集合改为 `_CONTROL_PLANE_PATH_TIMEOUTS` 表（claim 200s / 登记 60s），同类判据收进一处。桌面端同形：`BrokerStore.mutationTimeout(forPath:)` 给 `api/v1/endpoints` 60s、其余 mutation 保持 10s —— 此前所有 mutation 固定 10s，登记必然在控制面还在连的时候超时。

**桌面端把这个结果说出来。** `addEndpoint` 改走 `performMutationWithPayload`，`registrationOutcome` 把 `observation` 变成三句人话：已连上并发现 N 张卡 / 已连上但没有发现 GPU（提示确认观测方式）/ 还没有连上（带原因）。未连上时同时写入 `errorMessage`，不再是「正在确认状态」加一分钟后一行红字。

第四轮验证：Python `622 passed`（新增 4 条：登记失败带原因并保留记录、登记成功报告卡数、登记即可归组、登记超时高于它等待的采集预算），Ruff 通过；`zsh desktop/build-macos-app.sh test` 报告 `66 tests in 3 suites passed`（新增 4 条 Swift 用例）；`build-macos-app.sh` 与 `verify-macos-app.sh` 通过。现网 `p7770` 在本轮期间已被外部修好（host key 补齐、profile 改为 `linux-nvidia`、归入 `lab-group`），现为 ONLINE / 4 卡，可作为该诊断的事后印证，但不是本轮改动的产物。

仍未做：`api.py` 里 32 个同步 `def` 路由未改为 `async def` + `in_domain`。它们由 FastAPI 自动丢线程池，本来就不在事件循环上，观测到的卡顿不由它们造成；改动会覆盖每个路由体，且把读也串行到单 worker，收益需要先有实测支撑。`hanhai22` 插件依赖一个 12 小时有效期的外部 ControlMaster（由 Storyboard 项目的 `tools/run/hanhai_session.py up --auto` 建立并复用同一个 `~/.ssh/cm/` socket）。socket 过期后该组静默退回上一次成功观测的数字，`largest_allocatable_block` 变 `null`；实测重开 master 后 151 秒内自行恢复（`largest_allocatable_block: 5`，40 张空闲卡分散在多节点）。MCP 目前不投影 `monitor.last_error`，所以 agent 拿到 `null` 时没有可执行的下一步。

### 2.0.0 候选（当前工作树）

以下结果针对当前工作树，在准备发布 `v2.0.0` 时复跑：

| 检查 | 结果 |
| --- | --- |
| Python 全量测试 | `556 passed`，`uv run --reinstall-package serverpilot pytest -q` 通过。新增覆盖插件 v3 `limits` 校验（未知键拒绝、`lease_ends` 与 `max_lease_seconds` 一致性）、两种 `allocation` 的组级 limits 推导、`largest_allocatable_block` 在单次上限未知时为 `null`、以 `server_group_id` 路由到插件 apply，以及旧 `count\|name` 容量值的向后兼容解码 |
| Ruff / `git diff --check` | 通过 |
| macOS App | `zsh desktop/build-macos-app.sh` 与 `zsh desktop/verify-macos-app.sh` 通过；根目录唯一 `ServerPilot.app` |
| 插件契约 | 本机 `hanhai22` 与随包 `slurm-immediate` 的 `info` 均返回合法 `schema_version 3` 与 `limits`；升级前已保留 `hanhai22.v2.bak` |
| 实盘数据投影 | 以 sqlite backup API 制作运行库的**只读副本**，用当前源码生成快照：委托型集群作为正常分组投影，带组名与共享工作目录；`scheduler_servers` 不再出现；该登录节点不再落入 `cpu_only_servers`；两个分组分别推导出 `direct` / `on_release` 与 `delegated` / `hard_kill_at_time_limit` / `3600`。全程未写入运行库，未重启 daemon |

上述实盘投影使用只读副本，因此它证明的是**当前源码对真实数据的投影结果**，不代表正在运行的 daemon 已加载这份代码。

## 进程观察的单一判据

**一个进程是当前事实，直到这台服务器自己连续、不间断的完整采集把它落下满 `collector.process_absence_grace_seconds`（默认 60 秒）。** 此前一次不含它的完整采集就立刻把该行置 `active=False`。这不是「阈值太敏感」：`nvidia-smi --query-compute-apps` 是与 GPU 查询分开的探针，它返回空列表时同时代表「确实没有进程」和「这个 PID namespace 看不到进程」，而 `observation_complete` 只由 GPU 探针决定，于是容器化服务器上一次看不见进程的采集被当成了进程结束的完整证据。

- **判据是证据链，不是墙上时钟。** 窗口记在 `process_observations.absent_since`：完整采集里缺席则起表，被观测到则清零，**采集不完整或采集失败则清零**。断连期间没有证据产生，因此断连不计入缺席，而是让缺席从头开始。p8908 在 45 分钟内断连三次（最长 62 秒），任何一次都不会再退休一个仍在运行的进程。
- 因为断连不再计入，窗口不需要为断连时长留余量，取 60 秒即可：按预设采集周期这是十余次连续、健康、完整且都不含该进程的观测。它不再需要与 `stale_after_seconds` 排序，也不再从它派生——两者回答不同问题，各自独立。
- **「这张卡上现在有没有进程」只有一个判据、一个 owner。** 退休发生在证据到达的地方（`ingest_observation` → `_advance_process_absence`），所有读侧（`_current_processes`、`snapshot` 的批量查询、`bind_workload`）一律只问 `active`。读侧再加一层 wall-clock 过滤就是用第二个时钟回答同一个问题，而且会把「只是停止应答的服务器」上的进程悄悄退休——这正是本控制面不能有的 fail-open。
- `finalize_keepalive_stop` 的拒绝谓词随之修正为「目标卡上出现任何不是本卡 expected keepalive worker 的当前进程即拒绝」；worker 自身是否停掉仍由 `observation_not_before` barrier 判定。不这样分开，占卡 worker 自己的行会让每一次停卡与每一次 `reclaim_keepalive_for_claim` 都 409。
- **验证通过的停卡会直接退休该 worker 的进程行。** 年龄判据存在是因为「一次采集没列出它」有歧义，而这里没有歧义——ServerPilot 命令了这个 worker 停止，又看到停卡后的完整采集不再带它。
- 这不是被否决过的那条 release-empty 门槛：服务端没有新增任何「最近有进程就拒绝」的阈值，只是 `release_empty_conflicted_lease` 已经在读的那个「当前进程」集合本身变准了。
- **卡的周转代价已收敛。** 释放后卡仍会保持 `BUSY_UNMANAGED` 直到缺席窗口走满，但那是 60 秒的连续健康观测，不再叠加断连时长；`test_a_card_its_owner_released_comes_back_once_the_absence_closes` 钉住 release → re-apply 会收敛。
- schema 变更一列：`process_observations.absent_since`（migration `20260831_0034`）。

## 占卡起卡冷却期

**`inventory.keepalive_start_cooldown_seconds`（默认 600 秒）：一张 GPU 上最近一次 `kind='workload'` 的 `LeaseResource.released_at` 距今不足冷却期时，`list_keepalive_transitions` 把它判为 `ineligible` 并给出剩余秒数，而不是 `start`。** workload 租约的释放是控制面唯一的「主人干完了」信号，而分阶段任务两批之间的空窗与它长得一样；p8908 里释放后 8 秒占卡就占满了 65 GiB 显存。冷却期直接由既有列现查现算，不加列、不加迁移。只作用于**起卡**：`plan_keepalive_reclaim` / `reclaim_keepalive_for_claim`（Agent 申请时占卡让位）走 `_eligible_gpus(include_reclaimable_keepalive=True)` 这条独立路径，不受影响。

## 尚未完成的验证

Swift 单元测试已迁到 swift-testing，`zsh desktop/build-macos-app.sh test` 在本机（仅 CommandLineTools）可跑，62 个用例通过；XCUITest 仍需 Xcode，本机未运行。原生 APP 的实机按钮证据属于 `1.5.5`；`1.5.6` 在补齐根 App 构建与定向现场验收前，不能沿用该证据。尚未完成的桌面辅助体验检查包括 VoiceOver、键盘完整焦点顺序、缩放重排、色彩对比度测量与 Reduce Motion；不能仅凭截图宣称这些辅助功能完全合规。

以下运行环境能力仍未验证：

- host PID 的启动 ticks 尚未由 collector schema v2 公开；因此 worker 恢复使用 sealed namespace worker 证明、UUID 唯一 driver PID、boot ID 与新鲜观测交叉确认。若要把 host PID 重用窗口进一步收紧到启动 ticks，需单独演进 collector 协议并部署新入口，不能把 namespace ticks 当作它的替代品。**进程观察的宽限期把这个重用窗口从一个采集间隔放宽到宽限期本身**（`username` 为空的宿主 PID 分支没有 start-time 可比，身份复用只以「同 GPU、同 boot、行仍 active」为条件）：宽限期内同一 PID 被另一个同样不可见的 CUDA 进程复用时，新进程会被并入旧行。这只可能发生在原进程确实已退出的分支上——原进程还在跑时它的 PID 不可能被复用——后果是一张卡被继续记在已死的租约名下直到那个外来进程退出，方向上是扣住卡而不是发出去。尚未在现场观测到该重用。
- 完整工作日 shadow、2 小时内存 soak 和 24 小时数据库增长观察。
- 非 loopback 部署所需的 TLS 与访问控制。
- 其他 MCP 客户端的现场联调。
- 服务器分组、五工具 MCP、浏览器界面移除与统一集群模型已在源码落地，但尚未记录针对该合同的现场 daemon/MCP 联调：本轮只用运行库的只读副本验证投影，没有重启 daemon，也没有让升级后的插件真正跑过一次 `observe`。因此 `scheduler_capacity` 的新键（`largest_free_block`、`vram_mib`、`max_gpus_per_lease`、`cpu_cores_per_gpu`、`memory_mib_per_gpu`）尚无现场证据，实盘副本中它们仍来自升级前的旧编码而为空。已安装连接器描述是否已刷新，不在本文件宣称范围内。
- MCP 进程已经退出后，由调用方重新发起的新工具调用没有跨进程稳定 token，不能与旧调用判定为同一次传输重放；当前公开五工具合同不增加该 token。
