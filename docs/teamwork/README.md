# Project Teamwork Documents

这里的记录是项目过程文档，随仓库公开，写入前要脱敏：不放内网地址、账号、本机路径、
个人网络配置或工作负载细节。个人网络排障记录只留在本机，不进仓库（`.gitignore` 已排除）。

这是本项目自己的 Teamwork 读取侧入口：新会话依赖这个项目已经决定、得出结论
或试过的东西时，先读这里，再跟着索引打开真正相关的那几份。

## Project current state

- **4.0.0 已发布（10 月 31 日）**：见[计划](plans/v4-agent-and-app-redesign.md)。
  P0–P6 完成，本机运行该构建，直连链路实机通过。Slurm 真实流程经用户决定暂不验证。
  已发布为 4.0.1：MCP 收为三个工具。未验证项见 `docs/IMPLEMENTATION_STATUS_zh.md` 顶部条目。
- **通用分配模型与共用节点观测（9 月 8 日）**：用户已接受并要求执行，正式分配记录、连接维持、
  GUI/CLI/MCP 定位与迁移已实现并部署本机；显式接管已有连接、重启保持意图、18 节点和外部
  作业只读核对通过。真实申请／进入／归还和密码／TOTP 重认证仍待现场专用测试分配验证。
  后续按用户纠正改为普通／插件共用节点观测、协议 v5；节点实时用量与作业统计分开。
  用户已批准并登记计算节点主机密钥；严格校验通过，当前实际阻塞为计算节点账号认证拒绝（auth_failed）。
  决策见[插件计划补记](plans/server-plugin-mechanism.md)，交付见 [当前实现状态](../IMPLEMENTATION_STATUS_zh.md)。

- **瀚海“三卡”已定位为过期观测**：`IDX:N/A` 解析错误使采集停在 9 月 4 日，
  配置上限实际为 8。解析器已修并更新本机插件，源码已屏蔽过期容量；
  后台采集已恢复 ONLINE，观测到 57 张空卡、单节点最大 8 张；未提交作业，
  控制面修复已部署。节点列表继续沿用原表格，组内最多 4 行，超出纵向滚动；
  连接列使用 host:port，排序按实际列值且缺失值置后，实机验证已通过；有节点明细时不重复列登录入口行。详见[插件计划补记](plans/server-plugin-mechanism.md)
  和 `docs/IMPLEMENTATION_STATUS_zh.md` 对应条目。

- **「连接失败」已修复**：见[观测层重做与项目收口](plans/observation-layer-and-consolidation.md)。
  根因是一个超时数字同时当连接和整个观测的预算，且多个探测共享同一期限。传输层已改为
  daemon 持有的 SSH 复用连接。Windows GUI、排队申请、预约、维护窗口和人为停用轴已按该计划删除。
- **两条评审已 ACCEPT**：[server groups + 任务声明式 GPU 需求](records/server-groups-and-gpu-demand-review.md)
  与[范围缩减](records/scope-reduction-review.md)的候选 2 均被判 `ACCEPT`——前者关闭了
  Web 空组、workspace 预填、人类多主机误用三个失败点，同时保留 ungrouped/plugin/keepalive
  串行化/显式跨组 span 的兼容；后者关闭了已删外部调度器仍被文档描述为现有能力、
  浏览器 UI 打包、session setting、死类型/死投影与失效 `profile_id` 列这几个问题，
  受保护的运行路径未被打破。对应的选定方向记在
  [server groups 与 GPU 需求计划](plans/server-groups-and-gpu-demand.md)。
- **两份早期计划已被后续工作取代，只作历史参考**：
  [控制面收口](plans/serverpilot-control-plane-optimization.md)的立即语义已由其他工作达成，路由已统一到 `/api/v2`；
  [原生 macOS UI 下一阶段](plans/native-macos-ui-next-stage-execution-plan.md)的 inspector 与协调轨未采用，现行界面契约是 `docs/DESIGN_SYSTEM.md`。
- **keepalive 原语形状的讨论已被取代**：见[讨论](discussions/keepalive-primitive-shape-and-toggle-semantics.md)。
  现行做法是逐卡占卡，申请时自动让位，见 [per-GPU 占卡范围](records/per-gpu-keepalive-scope.md)。
- **开源整改与插件机制两条线各有一份计划在跟进**：
  [开源可维护性全面整改](plans/open-source-readiness-overhaul.md)与
  [服务器插件机制（瀚海 22 为首个插件）](plans/server-plugin-mechanism.md)，
  后者同时修正了本项目的 Teamwork 维护面本身。
- **端点 monitor 状态的两条正交轴已拆分并通过对抗性审查**：见
  [端点 monitor 状态的两条正交轴拆分](records/endpoint-monitor-status-axis-split.md)——
  连通性判据统一为 `last_success_at` 年龄（与 GPU 卡侧同一时钟），人为设定
  （DISABLED/DRAINING）改由消费者直接读 `enabled`/`lifecycle_state`，公开枚举值未改。
- **端点「无响应」的根因已定位并修完**：见[端点连通性状态的两条轴与单一判据](records/endpoint-reachability-axes-and-single-criterion.md)——不是阈值太敏感，而是 `last_error`（一次尝试的事件）被当成主机状态、且 monitor 枚举把连通轴与人为轴压成一个值。已收敛为单一年龄判据并删除零调用的重复副本；两条 keepalive/释放路径的同类判据仍未决。
- **p8908 占卡/任务同卡相撞的四条修法已落地**：见
  [占卡程序与 agent 工作负载相撞（p8908）](records/keepalive-workload-collision-p8908.md)——
  控制面新增唯一存活原语 `_lease_liveness_age_seconds`（现在查询任务 `gpu_status(task)` 即心跳），进程缺席改由 `process_observations.absent_since` 的证据链计时——
  完整采集缺席则起表，被观测到或采集失败则清零，断连不计入而是让缺席重来，
  窗口因此收到 60 秒；人工「释放空闲占用」与自动回收共用 `_manual_release_verdict`
  （App 只读服务端答案），占卡起卡新增 `keepalive_start_cooldown_seconds`。
- **agent 分不清「占卡」与「任务占用」的根因已定位并补上契约文本**：见
  [MCP 投影瘦身与 macOS GUI 优化](records/mcp-projection-and-macos-gui-optimization.md)——
  GUI 与 MCP 投影里早有完整状态词表，但 `MCP_INSTRUCTIONS` 之前从未定义
  `busy_gpus.status` 的四个值；已补上语义说明。**KEEPALIVE 这一条已于 9 月 9 日闭合**：
  `resource_nodes` 原样透传裸 `state`，而契约全文从未出现 KEEPALIVE 这个词，
  agent 因此把 65 GB 占用的占卡程序读成别人的任务并跳过可用卡；已在
  `agent_contract.py` 的 resource_nodes 规则里给这个 token 下定义并重新渲染两份生成物。
  `publicly_available` 仍是一个布尔，「开新字段区分挂着占卡与真的空卡」是未选用的备选。
- 其余 records 是此前各阶段已经跑完的结论——四项核心功能范围收敛、
  per-GPU keepalive 冻结评审、原生 UI 稳定候选评审、MCP 投影瘦身、
  空闲租约回收、gpu-broker→ServerPilot 迁移等——按需从下面的索引按主题打开，
  不在这里逐条复述。

## Document index

### discussions/

- [keepalive 对外原语形状与启停确认语义](discussions/keepalive-primitive-shape-and-toggle-semantics.md) — 可选、极轻量的 GPU keepalive 附加能力；对外原语形状与启停确认语义,协作中尚未被用户接受。

### experiments/

- [冒名 daemon 占住 8787](experiments/impostor-daemon-holds-8787.md) — 断言「8787 被冒名 daemon 占住，而非 daemon 未启动或远端 keepalive 孤儿占卡」,setup 为 ServerPilot 1.8.0 / macOS LaunchAgent,以 launchctl list、lsof、/health/ready 作判别。

### plans/

- [4.0 Agent 与 App 重设计](plans/v4-agent-and-app-redesign.md) — 已批准并合入 `master`；含决定表、P0 实测与分阶段计划。
- [Gpu Broker Keepalive Extension](plans/gpu-broker-keepalive-extension.md) — 把 keepalive 做成可选 endpoint capability + 显式 `gpu_set_keepalive(enabled)` 原语,已选方向。
- [原生 macOS UI 下一阶段可执行计划](plans/native-macos-ui-next-stage-execution-plan.md) — 已接受的原生 macOS UI 方向的下一阶段可执行计划,只描述设计,未执行 Swift/MCP/迁移/构建改动。
- [ServerPilot 开源可维护性全面整改](plans/open-source-readiness-overhaul.md) — ServerPilot 从「能用的个人项目」整改为可对外维护的开源项目。
- [ServerPilot project cleanup sweep](plans/project-cleanup-sweep.md) — ServerPilot project cleanup sweep plus delete-server reliability。
- [Server groups and task-declared GPU demand](plans/server-groups-and-gpu-demand.md) — ServerPilot 一等公民 server groups 与常规 GPU 需求选择;已被评审 ACCEPT。
- [ServerPilot 服务器插件机制](plans/server-plugin-mechanism.md) — ServerPilot 服务器插件机制（瀚海 22 为第一个插件）并修正项目 Teamwork 维护面。
- [观测层重做与项目收口](plans/observation-layer-and-consolidation.md) — 「连接失败（常常六台同时）」的根因与选定修法；一个超时数字当两个预算用、六个探测共享一条期限,传输层改 SSH 复用连接已实测验证,并借此收口观测面、删除死路径与三份客户端。
- [ServerPilot Control Plane Optimization](plans/serverpilot-control-plane-optimization.md) — 把 GUI/CLI/MCP 的正常资源路径收口为单一 loopback 控制面（立即成功或立即 `no_capacity`）,这一选定结果只描述设计,未执行代码改动。

### records/

- [占卡程序与 agent 工作负载相撞（p8908）](records/keepalive-workload-collision-p8908.md) — keepalive 把 80% 显存占到 agent 正在跑的卡上；根因是「此刻没观察到进程」被当成「活干完了」；四条修法已落地（唯一存活原语 + 心跳、进程缺席由 `absent_since` 证据链计时且断连重置、人工还卡与自动回收共用判据、起卡冷却期），周转代价已收敛到 60 秒连续健康观测。
- [端点 monitor 状态的两条正交轴拆分](records/endpoint-monitor-status-axis-split.md) — 端点 `monitor` 状态该由什么判据推导、消费者该读哪个字段;根因是连通性轴与人为设定轴被压进同一枚举、且连通性判据用错了时钟,已修法落地并通过对抗性审查。
- [端点 CPU/内存容量的权威来源](records/endpoint-capacity-authority.md) — 容量口径由 `_host_capacity` 单独拥有；准入面已从整机 cpu_count/memory_total 收口到同一份 cgroup 份额，算不出份额时 fail closed。
- [端点连通性状态的两条轴与单一判据](records/endpoint-reachability-axes-and-single-criterion.md) — 「网络波动导致可连接的服务器显示无响应」的根因与已执行修法；连通性只用 last_success 年龄一个判据，人为轴另走 enabled/lifecycle_state。
- [四项核心功能产品范围](records/four-core-product-scope.md) — ServerPilot 产品范围收敛为四项核心功能。
- [GPU Broker General Resource Control Plane](records/gpu-broker-general-resource-control-plane.md) — 统一操作数据路径（canonical state，现为 `GET /api/v2/state` + revision 一致性）,及其执行计划,均为持续目标。
- [per-GPU keepalive 冻结候选评审](records/gpu-broker-keepalive-per-gpu-review.md) — per-GPU keepalive 冻结候选（1.5.0）的严格评审判决。
- [Allocatable GPU Discrepancy](records/gpu-broker-native-ui-allocatable-gpu-discrepancy.md) — 「6 张 A800 可见但只有 4 张可分配」的原因定位与已验修复。
- [Packaging Failure Split](records/gpu-broker-native-ui-packaging-failure-split.md) — 桌面打包失败分叉的已验解法与残留不确定。
- [原生 UI 稳定候选评审](records/gpu-broker-native-ui-workflow-review.md) — 原生 UI 稳定候选（根目录唯一 `ServerPilot.app`）的评审判决。
- [原生 macOS GUI 公开 copy 与语言层级收束](records/gpu-broker-native-ui-workflow.md) — 原生 macOS GUI 公开 copy 与语言层级收束,持续目标。
- [本机 install/keepalive 集成迁到 ServerPilot](records/gpu-broker-to-serverpilot-install-migration.md) — 本机 install/keepalive 集成从旧 gpu-broker 路径迁到 ServerPilot 的整改。
- [空闲租约两阶段回收与占卡可见性](records/idle-lease-reclaim.md) — ServerPilot 空闲 GPU 回收（Agent 释放不及时 / 空闲清理 / 占卡保护）。
- [keepalive 失控后如何拨回正轨](records/keepalive-recovery-not-lease-ttl.md) — 控制面不可用时，被 keepalive 占住的 GPU 如何回到可用状态。
- [macOS App 设计契约一致性与可访问性审计](records/macos-design-contract-audit.md) — macOS App 设计契约一致性与可访问性审计,持续目标。
- [MCP 投影瘦身与 macOS GUI 优化](records/mcp-projection-and-macos-gui-optimization.md) — ServerPilot MCP 响应投影优化 + macOS 桌面端设计契约/可访问性优化;根因是契约文本从未定义 `busy_gpus.status` 四值与 `state=KEEPALIVE`,两者都已在契约里补上词表;未开新字段。
- [ServerPilot 1.8.0 开源可维护性审查](records/open-source-readiness-review.md) — ServerPilot 1.8.0 + 未提交插件工作的开源可维护性审查。
- [per-GPU 占卡范围](records/per-gpu-keepalive-scope.md) — ServerPilot 空闲占卡从整机切换为逐 GPU。
- [清理扫除候选计划审查](records/project-cleanup-safety.md) — 清理扫除候选计划的审查（verdict REVISE）。
- [Scope reduction 审查判决](records/scope-reduction-review.md) — 范围缩减候选的审查判决;候选 2 已 ACCEPT。
- [Server groups and GPU demand 审查判决](records/server-groups-and-gpu-demand-review.md) — server group + 任务声明式 GPU 需求实现候选的审查判决;候选 2 已 ACCEPT。
- [开源整改会话产出的未提交改动集](records/uncommitted-change-set-review.md) — 开源整改会话产出的未提交改动集复查。

### guides/

目前没有文档。
