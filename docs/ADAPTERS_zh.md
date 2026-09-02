# Adapter 能力与边界

adapter 只实现已注册的观测或占卡协议；资源身份、准入、租约和审计始终属于
ServerPilot。它不是第二个控制面，也不是远程命令入口。

密封的是调用契约（动词固定、参数形状固定、输出严格校验），不再是实现来源。
配置只能选择当前可发现的 observation profile：三个内置值
内置的 `linux`，或本机已发现的插件 ID。
不能携带 shell、argv、SSH 参数、机密信息或 Agent 自定义 target。

本机插件以当前用户权限运行，放进插件目录的脚本由用户自己负责。
Slurm 一类集群通过同一套 observation profile / 插件机制接入，和普通主机一样登记为
endpoint，而不是单独的调度器对象。采集协议见
[COLLECTOR_SCRIPT_zh.md](COLLECTOR_SCRIPT_zh.md)，插件契约见 [PLUGINS_zh.md](PLUGINS_zh.md)。

| Adapter | 能力 | 允许的工作 | 不允许 |
| --- | --- | --- | --- |
| `raw-ssh` | observation | 固定的主机 / GPU 遥测和已观测 PID 的进程详情 | 任意 shell、读取私钥、写入租约 / 申请 |
| `server-script-v1` | endpoint_keepalive | 先预检 `serverpilot-keepalive --protocol-info`，再执行固定的 `serverpilot-keepalive --schema-version 3` 做空闲 GPU 占卡；身份恢复只读执行固定的 `--inspect --schema-version 3` | 项目任务启停、调用方指定 PID/GPU、路径或环境 |

未知 adapter、未知 capability、过期或冲突的观测、不确定的远端结果，一律 fail closed。

## 空闲占卡

endpoint 不要求用户手工填写 adapter。用户明确点击「开始占卡」时，ServerPilot 自动挂载
代码内置的 `server-script-v1` helper；公开接口仍然只有 `enabled: true|false`。开启时只对已验证的
空闲 GPU 做 reconcile；忙碌、未托管、冲突或状态过期的 GPU 一律不动。
ServerPilot 传入精确的物理 UUID，helper 只能管理自己的 worker。

每张 GPU 独立判断：一张 GPU 冲突不会阻断同一服务器上其他空闲 GPU 的占卡启动。

adapter 在做任何启停变更前，先执行只读的 `--protocol-info` 预检。预检真正校验的是
`kind=serverpilot-keepalive`、`schema_version=3`（`KEEPALIVE_SCHEMA_VERSION`），以及这四个能力：
`per_gpu_keepalive`、`pidfd_identity`、`pci_bus_id`、`worker_attestation`。
helper 还会报告 `implementation_version`，该值等于构建它的 ServerPilot 包装版本
（`KEEPALIVE_IMPLEMENTATION_VERSION` 直接取自 `serverpilot.__version__`，当前为 `2.0.0`）；
adapter **不**按这个数字做兼容判断。预检失败返回 `keepalive_helper_incompatible`，不发送任何变更
payload。旧 v2 wire/state 版本直接拒绝。
恢复时只接受 helper 自己的 v3 状态里仍然存活、且带固定 marker 的 worker：helper 对指定物理 UUID
做固定的 NVIDIA compute 查询，必须恰好得到一个 driver-visible PID，ServerPilot 再用这个 PID 和
Linux boot ID 与最新一次采集观测对照。helper PID namespace 内的 `start_time_ticks` 只用于本地
pidfd 停止校验，不能当作 host PID 的启动时钟；当前 collector schema v2 没有提供可做这一端到端
比对的 ticks。历史工作负载租约如果遗留为「归属待确认」，人可以在监控端执行「清理遗留归属」；
ServerPilot 会先重新采集，确认相关 GPU 上都没有进程后才释放，只看 0% 利用率不会直接释放。

每张目标 GPU 都有独立的内部租约、worker 和健康状态。worker 按该卡 CUDA 可见总显存占用约 80%、
GPU 利用率约 80%，只用单个 PyTorch CPU 线程，稳态下无磁盘 / 网络 I/O；实际的 CPU、RSS、
GPU 干扰和停止响应，仍须在获授权的目标主机上验证。

即时受管的申请，只有在普通分配失败、且服务已规划出完整并验证过的逐卡回收方案时，才会停止这些
worker、重新采集确认为空，然后重试原申请。它不影响同机其他卡、未托管进程和直接 SSH 任务；
要在这台机器上直接 SSH 干活前，管理员应显式关闭该 endpoint 的占卡策略。

## 控制面不可用时的人工恢复

占卡 worker 以独立会话启动，与控制面分离；控制面停止后它们会继续占卡，没有自动释放路径。三条显式命令覆盖这种情况，都不要求 daemon 在线：

| 命令 | 作用 |
| --- | --- |
| `serverpilot keepalive inspect --endpoint <id>` | 只读，经固定 `--inspect --schema-version 3` 报告远端仍在跑的 worker 及其 PID |
| `serverpilot keepalive stop --endpoint <id>` | 对该 endpoint 当前观测到的全部 GPU 执行固定停止命令 |
| `serverpilot daemon reclaim` | 端口被非 launchd 托管的 ServerPilot 占住时停止它并交还给 LaunchAgent |

它们只从控制面数据库读取 endpoint 的连接信息，GPU UUID 来自一次现场只读采集，其余全部走密封 adapter；不写库，也不复制领域规则。endpoint 被暂停时这些命令仍然可用——这正是需要它们的场景。三条命令只能由人或 Agent 显式调用，不是后台策略：控制面恢复后，reconcile 会按既有策略重新决定占卡。

`daemon reclaim` 只处理「有 ServerPilot 在应答且实例标识匹配、但不归 launchd 管」这一种情况。daemon 归属正常时它不动任何进程；SIGTERM 后进程仍在则报错交给人处理，不自行升级到 SIGKILL。

## 不变边界

- adapter 不接触 `BrokerService`、数据库，不做租约 / 申请写入；
- 不新增通用 `execute()`、第二套认证或非 loopback listener；
- GPU 身份始终是 `endpoint_id:gpu_uuid`，adapter ID 只作诊断来源。
