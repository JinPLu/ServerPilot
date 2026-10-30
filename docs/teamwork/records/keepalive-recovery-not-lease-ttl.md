---
status: active
superseded-by:
created: 2026-08-26
updated: 2026-08-27
---

# Record: keepalive 失控后如何拨回正轨

## Identity and final goal

- Subject identity: 控制面不可用时，被 keepalive 占住的 GPU 如何回到可用状态
- Final goal: 人或 Agent 能在控制面异常时把资源状态拨回正轨，且不引入未经批准的自动机制
- Authoritative owner: 用户（方向决策）；Root agent（取证与实现）

## Decision map

问题源于一个结构事实：keepalive 是"远端持有资源、释放机制只存在于本机"。由此分出两类互斥的解法——让远端自己具备退出条件（自动），或让人/Agent 具备显式的干预动作（手动）。两者可以叠加，但方向选择决定了要不要改 keepalive 线协议。

## Current stage

已定案。取证阶段确认：`server_keepalive.py:209` 以 `start_new_session=True` 启动 worker，主循环 `880-889` 只在收到 SIGTERM/SIGINT 时退出，无截止时间、无心跳、无父进程死亡检测；`api.py:1104-1111` 的 lifespan 只取消本地采集任务，从不通知远端；`cli.py` 中不存在任何 keepalive 子命令，因此本地没有一条不经过 daemon 的应急出口。

## Current question batch

keepalive 失控自释放怎么做？（当前：控制面一死，远端占卡永不退出）

## Options and recommendation

- 协议升到 v4 加租期：worker 自带截止时间，控制面采集循环顺带续约，超过阈值无续约则自行退出放卡。Root 当时的推荐项，理由是它能覆盖 `kill -9`、断电、崩溃三种 shutdown 钩子挡不住的情况。
- 只在 lifespan shutdown 时发 `enabled: false`：实现最小，但挡不住非正常退出。
- 不改机制，只加一个不经过 daemon 的本地应急命令。
- 前两者叠加。

用户否决了加租期的方向。

## Decisions and open points

- 已定：不加租期/自动自释放机制。改为提供人或 Agent 可显式调用、把状态拨回正轨的操作。
- 已定：该恢复入口不得依赖本机 daemon 可用——今天的故障正是 daemon 侧不可用（端口被冒名进程占住，见 `docs/teamwork/experiments/impostor-daemon-holds-8787.md`）。
- 已定：入口形态为三条显式 CLI 命令 —— `serverpilot daemon reclaim` 交还被非 launchd 进程占住的端口，`serverpilot keepalive inspect` 只读报告远端在跑的占卡 worker，`serverpilot keepalive stop` 停止它们。端口归属校验失败的提示同时给出占用进程的 PID 与命令行。
- 已定：这三条命令只读控制面数据库取端点连接事实，其余经密封 adapter 与只读采集；不写库，也不复制领域规则。该边界写入 `AGENTS.md`。
- 下一步授权动作：无。恢复入口已实现并在真实环境验证。

## Experiment slot

unused.

## User quotes

> 不用加租期，加agent或者人类能够调整、拨回正轨的操作即可

> 这是什么情况，为什么需要依赖这个东西其他agent才能使用，是冗杂设计吗

## Working understanding

用户的取舍与其一贯的工程原则一致：不为假想故障加自动机制，而是保证人在回路时有明确、可用的干预手段。这同时约束了实现——恢复入口是"操作"，不是后台策略，因此不应引入新的后台循环、续约协议或状态机。

需要与用户区分开的一点：keepalive 让一个 README 中标注为「可选」的便利功能，变成了整台机器的硬依赖——daemon 不可用时，连从未安装 ServerPilot 的人和 Agent 也被挡在外面。用户的问题"是冗杂设计吗"指向的正是这个耦合。Root 的判断是：协调层本身不冗杂，错的是降级方向——daemon 挂掉的正确降级应是"没有协调"，当前是"资源不可用"。用户选择用显式恢复操作而非自动租期来处理这个降级，这是对成本与可控性的取舍，不是对该判断的否定。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-27 — 入口形态定案

形态从「未定」收敛为三条显式 CLI 命令并实现。实现过程中改了两次取数来源：先设计为读 `inventory.yaml`，发现 daemon 的 inventory 里 `endpoints: []`（端点的权威来源是数据库，见 `collector.py` 的 "DB inventory is the mutable owner after bootstrap" 注释）；随后一次复查指出用 `collector_endpoints()` 会因 `lifecycle_state` 过滤而在端点暂停时失效，改用既有的 `collector_endpoint()` 单端点入口——该入口的 docstring 本就写明「停止验证必须在端点暂停后仍然可用」。因此 `AGENTS.md` 里最初写的「不读写数据库」是错的，已改为「只读数据库取连接事实、不写库」。

### 2026-08-26 — 方向定案

由一次真实阻塞触发：Agent 报告 8 张 A800 各被 keepalive 占 65 GB 无法训练。事后查明该次故障的真因在 App 端口归属校验而非 keepalive 孤儿，但取证过程确认了 keepalive 确实缺少任何自释放与应急出口。用户据此定案：不加租期，加显式恢复操作。
