---
status: active
superseded-by:
created: 2026-08-26
updated: 2026-08-26
---

# Experiment: 冒名 daemon 占住 8787，App 拒绝启动且 18 张卡显示不可用

## Identity and boundaries

- Subject identity: 断言「8787 被冒名 daemon 占住，而非 daemon 未启动或远端 keepalive 孤儿占卡」，setup 为 ServerPilot 1.8.0 / macOS LaunchAgent `local.serverpilot.daemon`，以 `launchctl list`、`lsof`、`/health/ready` 作判别
- Failure signature: macOS App 弹出「无法启动 ServerPilot — 启动本机后台服务失败：http://127.0.0.1:8787 is not served by gui/501/local.serverpilot.daemon」并退出；同时一个使用 MCP 的 Agent 报告「daemon 没起」，8 张 A800 各被 `serverpilot.server_keepalive` 占 65 GB，剩余约 16 GB 不足以跑需要 27.5 GB/卡的训练。环境：ServerPilot 1.8.0，macOS，LaunchAgent `local.serverpilot.daemon`
- Authoritative owner: Root agent
- Failure boundary: 期望 8787 由 launchd 托管的 daemon 持有；实测 8787 被一个非 launchd 进程持有，而 `launchctl list` 显示该作业未运行、上次退出码 3
- Success signal: `launchctl list` 中 `local.serverpilot.daemon` 有真实 PID，且该 PID 就是 8787 的持有者，`/health/ready` 返回 ready，快照中 18 张 GPU `available`，App 正常打开

## Current diagnosis

已确认。原因不是 daemon 未启动，也不是 keepalive 孤儿。是一个从仓库 checkout 手工启动的 `uv run serverpilot serve` 进程顶着 LaunchAgent 的 `--daemon-instance-id` 占住了 8787，导致真正的 LaunchAgent 绑不上端口而反复退出，App 的归属校验随之失败。

## Hypotheses

| Hypothesis | Standing | Discriminator | Evidence |
| --- | --- | --- | --- |
| daemon 未运行（Agent 的原始诊断） | 已否定 | `/health/ready` 是否响应 | 首次检查即返回 `{"status":"ready",...,"process_id":6402}`，8787 有 LISTEN |
| 远端 keepalive 孤儿把卡占死 | 已否定 | 快照中 GPU 是否 `available` | `available_gpus: 18`、`busy_gpus: 0`，全部 `KEEPALIVE` 待命态，正是 `gpu_apply` 可让位的状态 |
| launchd 作业本身崩溃 | 已弱化 | 退出码与端口归属的先后 | 退出码 3 是绑定失败的结果而非原因 |
| 非 launchd 进程占住端口 | 已确认 | 8787 持有者的 PPID 与命令行 | PID 6401 为 `uv run serverpilot serve ... --daemon-instance-id macos-launchagent-v1\|local.serverpilot.daemon\|...`，21:41:52 启动自 `.venv/` |

## Observations

### 端口持有者身份

- Expected discriminating branches: 若持有者是 launchd 作业，则 `launchctl list` 应显示同一 PID；若是别的进程，两者不一致
- Raw result: `launchctl list` 显示 `-	3	local.serverpilot.daemon`（未运行，上次退出 3）；`lsof` 显示 8787 由 PID 6402 持有，其父进程 6401 命令行为 `uv run serverpilot serve ... --daemon-instance-id macos-launchagent-v1|local.serverpilot.daemon|...`
- Owner interpretation: 该进程复制了 LaunchAgent 的实例标识，因此 `/health/ready` 的 `daemon_instance_id` 看上去合法，但它不受 launchd 管辖。App 校验的是 launchd 归属而非标识字符串，故拒绝。

### 修复后的稳态

- Expected discriminating branches: 若原因判断正确，释放端口后 launchd 应能绑定并进入稳态；若还有其它占用者，端口会再次被抢
- Raw result: 终止 6401/6402 后 8787 free；`launchctl kickstart -k` 后 `launchctl list` 显示 PID 81042 且 `lsof` 同为 81042；采集恢复后 `data_age 6.2s`、`online 5/5`、`available 18/18`、`abnormal 0`；App 打开后不再报错，LaunchAgent 经其 `daemon ensure` 重启为 82080 仍保持归属一致
- Owner interpretation: 因果链闭合。

## Cause and owned boundary

一个从仓库 checkout 手工启动的 `serverpilot serve` 进程被赋予了 LaunchAgent 的 `--daemon-instance-id` 并占住 8787。第一个坏的自有边界是：`--daemon-instance-id` 可由任意本地调用方指定，系统没有任何机制阻止非 launchd 进程冒用该标识。`/health/ready` 因此报告了一个真实但误导的身份。

派生问题（本次未修，已进入整改计划）：App 的错误文案「is not served by gui/501/local.serverpilot.daemon」准确但不可操作——它没有告诉用户谁占着端口、也没有给出恢复动作，用户只能选择「退出」。

## Authorized repair and verification

- Authorization: 用户明确授权解阻塞（选择"先切到执行模式把 daemon 起回来并确认 8 张卡可用"）
- Repair: 终止冒名 daemon 进程 6401/6402 及残留的 `daemon ensure` / `launchctl kickstart` 辅助进程，随后 `launchctl kickstart -k gui/501/local.serverpilot.daemon`。未修改任何代码。
- Same-path verification: `launchctl list` 与 `lsof` 指向同一 PID；`/health/ready` ready；快照 18/18 可用、异常 0；MCP stdio 探针 2.9 秒返回 `gpu_status isError: False`，三工具齐全；App 正常打开不再弹错
- Probe cleanup: 临时 MCP 探针脚本位于 `/tmp/sp_mcp_probe.py`，未进入仓库。该探针的正式版本已作为 CI 护栏纳入整改计划（`tests/test_mcp_protocol.py`）。

## History

`Append only. Never rewrite or remove; a correction is a new dated entry.`

### 2026-08-26 — 诊断与修复落盘

Agent 的原始报告（"daemon 死着导致 keepalive 不让位"）在两个环节上判断错误：daemon 在运行，卡也处于可分配的 KEEPALIVE 待命态。真实故障在 App 侧的端口归属校验。此次现场同时印证了复查中的一条 MCP 缺陷：`initialize` 上报 `serverInfo.version = '1.29.1'`（MCP SDK 版本）而非 ServerPilot 1.8.0。
