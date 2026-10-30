---
status: active
superseded-by:
created: 2026-08-26
updated: 2026-08-27
---

# Plan: ServerPilot 服务器插件机制 + Teamwork 合规

## Intended result and selected direction

- Subject identity: ServerPilot 服务器插件机制（瀚海 22 为第一个插件）并修正项目 Teamwork 维护面
- Authoritative owner: Root agent
- Intended result: App 能从服务端列表选择采集方式；瀚海以插件出现且核数/内存与现场一致；项目 AGENTS.md 与扁平文档符合现行 Teamwork 全局策略
- Selected direction: 本机可执行文件 + 固定动词 `info/observe/apply/release`；脚本自己决定怎么够到服务器；复用 schema v2 校验。Teamwork：刷新托管块、提炼两份仍生效旧结论、修公开文档断链
- Scope and protected behavior: 不改 `ADAPTER_REGISTRY`；不改固定 ssh argv；routine MCP 保持恰好三工具；插件 `plugin_id` 限 40 字符故无 DDL；瀚海首版只 `observe`，master 不在则失败且不认证

## Current execution plan

1. L4 Teamwork 合规：init-project、两份 discussions、文档卫生、本计划落盘
2. L1 `plugins.py` 契约与发现 → 打开 `observation_profile` → collector 插件分支 → `hanhai22`
3. L2 `GET /api/v1/observation-profiles` → 桌面动态选择器
4. L3 apply/release 接入 lease；MCP 列表工具放 advanced
5. L5 `PLUGINS_zh.md` 与边界修订
6. 汇合验证：三条红线、macOS 构建、瀚海端到端

## Authoritative project records

- 宿主计划：`~/.cursor/plans/plugins_and_teamwork_compliance_fd791a63.plan.md`（吸收并取代 `serverpilot_server_plugins_ad84ff6e`）
- 插件契约：`docs/PLUGINS_zh.md`
- 采集协议：`docs/COLLECTOR_SCRIPT_zh.md`

## Dependencies and parallel tracks

L4 立刻可开。L1-1 解锁 L2 / L3 / L5。L1-4 依赖 live ControlMaster；master 不在则停在该步，其余线继续。

## Verification

插件发现与校验单测；密封 adapter 注册表、两个固定 ssh argv、routine 恰好三工具保持绿；`build-macos-app.sh` 与 `verify-macos-app.sh`；瀚海在 App 上核数/内存与 `nproc`/`free` 一致。

## Risks and replan conditions

插件改为 Python entry point（进程内、同权限）；或首版就让 `gpu_apply` 对排队式集群发作业——与 `no_capacity` 不排队冲突，需先定 lease 模型。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-27 — 公开身份改为泛化参考插件

开源整改（`plans/open-source-readiness-overhaul.md`）决定仓库只随包提供泛化的 Slurm 参考插件 `slurm-immediate`，集群参数读插件旁的 `slurm-immediate.json`，未配置即失败。瀚海 22 移到本机用户插件目录 `~/Library/Application Support/ServerPilot/plugins/`，`plugins/` 与 `marketing/` 加入 `.gitignore`。本计划的 intended result 中「瀚海以插件出现」仅在本机成立，不再是仓库交付物。插件机制本身（四动词、schema v2 复用、apply/release 接入 lease）不变。

### 2026-08-26 — 计划首次落盘

用户接受插件机制方向（文件夹脚本 + App/MCP 列表选择），并要求同时把 ServerPilot 的 Teamwork 维护拉回 `policy/teamwork-global.md`。旧 case 原件保留，只提炼仍生效结论。

### 2026-09-07 — 三卡容量失真定位与修复

用户原话：「本地 storyboard/ 项目有更详细的使用规则，现在在serverpilot申请都只能申请到3卡，但是实际上空很多空卡」。

读取 `另一本地项目的集群接入说明` 后，
确认该项目记录的规则是单节点 8 卡、QoS 无 GPU 张数上限；CPU 核与作业形状会约束调度。
读取本机控制面发现三卡为 9 月 4 日旧观测，采集因 `IDX:N/A` 解析异常持续失败。
修复与验证统一记录在 `docs/IMPLEMENTATION_STATUS_zh.md` 的「瀚海容量停在三卡」一节。
后续接入判断应以 Storyboard 现行规则与新鲜观测为依据，不把三卡当固定配额。

### 2026-09-07 — 节点列表与布局纠正

用户要求：「按照节点梳理、排列开瀚海22服务器所有节点，弄个列表（滑轮来），所有group都限定宽度，然后设置滑轮」。
随后明确范围：「GPU 分区全部节点（推荐）」。
对横向等宽卡片的实现，用户纠正：「你不需要更改布局，你仔细理解我们的布局设计，你现在搞乱了」。

该纠正排除了横向卡片/网格方案。实现遵循 `docs/DESIGN_SYSTEM.md`：单张表、纵向分组段头、
44 pt 行高和同一套列宽；节点明细放在原分组内，长列表只增加组内纵向滚动，
不创建独立节点 SSH endpoint，不代替 Slurm 选择节点或启动 workload。

用户随后补充：「每个group保持4行差不多就可以了，有多的就显现滚动条」。
实现以 4 行作为每组可见上限；短组按内容收缩，超过上限才出现滚动条。

用户继续要求：「瀚海22的“ssh连接”列要和其他保持一致，尽量用host、port」；
「现在表头点击之后的排序是错的，需要优化」。连接列统一显示 `host:port`，
节点在同一登录入口后附节点名；组内所有行共用列值排序，缺失值两方向置后，切换排序回组顶。

用户再次纠正：「为什么把节点展开了，还有个‘按需申请’的单独一行？」
有在线节点明细时只列节点，删除重复的登录入口行；「按需申请」保留在组标题中。

### 2026-09-08 — 连接失效与领卡归属的诊断、优化建议

用户原话：「瀚海22 怎么又“超时”了？另外这个服务器领卡之后我也不知道自己用的哪个，这很奇怪，怎么优化？」

本机 REST 在 11:57（Asia/Shanghai）的实测：最后成功观测为 10:28:35，
最近错误为 `plugin_error: no live ControlMaster for hanhai22`，对应 control socket 不存在。
这是人工 SSH 复用连接失效，不是此前的 `IDX:N/A` 解析错误；连接为何退出尚未查明。
当时运行的旧插件不主动认证。后续核实已有自动密码／TOTP 认证实现，因此“只能人工登录”不是当前结论。
当前 REST 活跃 leases 为空。旧观测记录了当前账号的 一个 Slurm 作业，
节点 gnode07、槽位 0，但数据已过期，不能宣称作业现在仍在或是 ServerPilot 管理的租约。

源码和本机插件都有同一信息断点：远端 apply 已返回 `node`，本机输出时丢弃；
`parse_apply_payload` 与 lease resources 也没有节点/调度作业的公开定位信息。
桌面节点行的「项目 / 任务」固定显示 `—`，没有消费租约关联。

建议（尚未实施、未视为用户选定方案）：

- 连接原因显示「登录连接已失效 · 需要重新登录」，同时展示最后成功时间和登录入口；
  登录恢复依靠用户完成认证，不能把延长超时当作修复。
- 将分配所得节点、Slurm job ID、GPU UUID 与作业内设备序号沿插件→租约→REST→桌面贯通；
  领卡成功立即展示定位结果，并在原节点行的项目/任务列关联任务。保留单表、每组 4 行。
- 节点详情区分 ServerPilot 租约和同账号在外部提交的 Slurm 作业；后者不能推断项目/任务，
  显示作业号与已观测占用位置。观测失效时只保留明确标注过期的历史定位，不承诺仍占有资源。
- 登录入口与执行节点是两个位置；使用指导应基于该 Slurm allocation，不把登录节点上的
  `CUDA_VISIBLE_DEVICES=0` 误当成在所分配计算节点上执行。

本轮只读本机 REST 与文件，没有建立远端连接、提交或取消作业，也未修改运行中的插件。


### 2026-09-08 — 已接受方向与本轮实现

用户明确要求「PLEASE IMPLEMENT THIS PLAN: ServerPilot 通用分配模型与插件能力升级」。
关键原话保留：

> 统一从 ServerPilot 领卡；外部作业只展示，不自动接管。
> 用户显式开启连接后自动维持；认证失败停止恢复，等待用户处理。
> 任务声明预计使用时长，明确展示资源提供方确认的截止时间。
> 对按整个分配归还的资源，只有任务失联且全部资源持续空闲时才自动回收。
> 保持单张服务器表、纵向分组、每组最多四行和组内滚动。
> ServerPilot 管理分配，不启动、停止、迁移用户程序；归还分配的影响必须明确。

本轮据此取代上文尚未选定的建议：通用正式分配记录、v4 能力协议、daemon 连接协调、
service 统一申请／归还、GUI/CLI/MCP 同一定位结果与迁移已实现并安装本机。
瀚海配置上限采用现有访问文档的 10 天；具体截止以 Slurm 确认为准。
凭据保留原路径，认证实现独立打包，未复制秘密或运行时引用其他项目。

现场没有创建测试分配，也没有进入、取消或接管正在运行的外部任务。显式连接复用了已有
会话，持久化意图跨 daemon 重启保留，节点容量和外部作业的只读核对通过。
此前 SSH 退出原因和 Slurm 进入参数的现场验证仍未知；执行入口 verified=false。
本轮具体交付与验证以 [当前实现状态](../../IMPLEMENTATION_STATUS_zh.md) 最新条目为准。


### 2026-09-08 — 用户指出已领资源使用情况不可见

用户原话：「现在没办法查看瀚海服务器的资源使用情况，即使是我们自己领取的」。
本轮确认上一实现只展示位置：Swift 没解码分配的 GPU 利用率／显存字段，节点压力值固定为空，
使用情况页没展开分配。已补逐卡数据、采样时间、已领卡汇总与新鲜度门槛；Swift 76 项通过，
根目录应用构建验证并重启。

现场 REST 当前无 ServerPilot 瀚海租约，只有同账号一个外部作业。它没有设备采样，
Slurm sstat 也无 GPU 指标；登录入口经 SSH 只读访问 gnode07 因缺受信任主机指纹拒绝，
未绕过校验或建立 Slurm 作业步骤。现有外部作业实时 GPU 观测仍待可信采集入口；
不能把本轮展示修复说成已取得该作业的实时 GPU 指标。


## 2026-09-08：改为共用节点观测

用户纠正原文：
> 所以你打算怎么做？怎么获取相关的数据，怎么保证和baige服务器之类的一般服务器的信息处理类似。我不希望瀚海22服务器这么“特殊”，尽量和其他保持一致，特别是必要信息的处理

用户随后明确要求实施「瀚海与普通服务器共用资源观测链路」：共用现有固定探针与必要信息
处理，插件只发现计算节点并解释 Slurm 分配；不创建 srun 步骤，不修改训练，不启动远端
常驻采集进程。现场使用现有本人作业；若已结束则换当前作业，不额外申请资源。

当前实现已替换独立的已领 GPU 汇总路径：普通和插件节点共享原始探针、解析、节点存储、
CPU 差分、GPU/显存当前汇总、进程和新鲜度投影，客户端只按通用能力补充分配入口。
插件协议升至 v5，观测 schema 3；旧版需要更新。GPU 空闲不能绕过 Slurm 准入。

现场阻塞仍是 gnode07 缺可信主机密钥。集群的登录方式是账号加动态码，用户没有独立的公钥来源。

已搜索官方文档，能确认两步认证登录和分配后计算节点 SSH 的用法，不能找到公开指纹表。
经已有认证登录入口读取的候选主机公钥未经独立验证，尚未登记，也未关闭严格校验。
交付、测试及现场状态统一更新在 [当前实现状态](../../IMPLEMENTATION_STATUS_zh.md)。


2026-09-08 14:54 更新：用户对指定 gnode07 公钥首次信任明确回复「允许」。已登记该键，
严格主机校验通过；随后非交互 SSH 被账号认证拒绝（publickey,password）。登录节点
~/.ssh 只存在 known_hosts，未读取私钥或新增账号认证密钥。后台已显示 auth_failed，
不再是 host_key_rejected。一个外部作业 仍在运行，用量仍未知。官方登录手册对 SSH
密钥登录有限制，计算节点支持的认证方式需要进一步确认；尚未改变用户的禁用 srun 约束。
