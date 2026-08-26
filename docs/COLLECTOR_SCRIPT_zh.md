# 服务器采集脚本协议（schema v2）

`server-script-v1` 是面向单台服务器的固定只读采集协议。ServerPilot 对这个 profile 只会通过 SSH
执行下面这一条只读入口：

```text
serverpilot-collect --schema-version 2
```

入口名、参数和 schema version 都由代码固定；endpoint 配置、REST/MCP 输入和 Agent 都不能传入
命令、路径、argv、Docker 参数、环境变量或 SSH option。采集不是远程执行授权。

## 在目标服务器安装

在每台被观测的服务器上安装 ServerPilot 发布包（例如组织提供的 wheel），其中要包含同版本的
`serverpilot-collect` 命令行入口：

```text
python -m pip install /受控发布目录/serverpilot-<version>-py3-none-any.whl
serverpilot-collect --schema-version 2
```

第二条命令必须只在 stdout 输出一行 JSON。ServerPilot 走非交互式 SSH，入口必须在该账号的
非交互 `PATH` 里，推荐安装到 `/usr/local/bin/serverpilot-collect`。诊断信息、banner 和日志
都不要写进 stdout。

如果服务器需要 Docker、厂商 runtime 或自定义工具前缀，可以在**这台服务器上**自行维护一个
同名短脚本（包装器）：它仍然只接受上面的固定参数，输出完全相同的 JSON。这类本地实现由服务器
管理员负责部署；不要把 Docker 命令、路径或额外参数填进 ServerPilot 的 endpoint 配置。

### 从源码构建受控 wheel

在受控构建机的同一版本源码根目录执行：

```bash
uv build
```

把 `dist/serverpilot-<version>-py3-none-any.whl` 交给目标服务器的管理员，按他们受控的软件分发方式安装，并确保 `serverpilot-collect` 在该 SSH 用户的非交互 `PATH` 里。安装后先在目标服务器上手工跑一遍固定入口，再在 App 里登记 endpoint；不要把 wheel 路径、pip 命令或运行时环境填进 endpoint 配置。

## JSON 合同

版本 2 的顶层对象必须且只能包含：

```json
{
  "schema_version": 2,
  "identity": {"hostname": "node-a", "boot_id": "..."},
  "host": {
    "cpu_count": 64,
    "load_1m": 1.25,
    "cpu_total_ticks": 1000,
    "cpu_idle_ticks": 750,
    "memory_total_mib": 262144,
    "memory_available_mib": 196608
  },
  "gpu_probe_available": true,
  "gpus": [{"gpu_index": 7, "cuda_ordinal": 0, "gpu_uuid": "GPU-...", "name": "...", "total_vram_mib": 81920, "memory_used_mib": 0, "memory_free_mib": 81920, "gpu_utilization_pct": 0, "memory_utilization_pct": 0, "temperature_c": 35, "power_watts": 100.0, "pstate": "P0", "health": "OK"}],
  "processes": [{"gpu_uuid": "GPU-...", "pid": 123, "used_memory_mib": 1024, "executable": "python", "username": "gpu", "process_started_at": "2026-08-10T00:00:00+00:00"}]
}
```

共享调度器集群（Slurm / LSF 等）可额外带一个顶层 `scheduler` 对象，只表达「这个 endpoint 能按需申请、现在大约有多少空闲」，不进入 GPU 账本：

```json
{
  "scheduler": {"free_gpu_count": 30, "gpu_name": "NVIDIA A100-SXM4-80GB", "note": "按需申请，不排队"}
}
```

`free_gpu_count` 和 `gpu_name` 必填，`note` 可选，只允许这三个字段。Agent 在 `gpu_status` 的
`scheduler_servers` 里看到它，申请时才真正入账。普通单台服务器不返回这个字段。

`gpu_index` 保留 `nvidia-smi index` 供界面识别；`cuda_ordinal` 固定表示设置 `CUDA_DEVICE_ORDER=PCI_BUS_ID` 之后的执行 selector，由采集脚本按 `pci.bus_id` 排序算出。两者不得混用。`identity`、`host`、每个 GPU 和每个 process 的字段集合同样是固定的，不能扩展；顶层除固定字段外，只额外接受上文的可选 `scheduler`。数值必须是 JSON
number（不能用字符串、NaN 或 Infinity），字符串不能含控制字符，`process_started_at` 必须带
时区。GPU UUID、index 和 CUDA ordinal 各自不得重复；process 的 `(gpu_uuid, pid)` 不得重复，且 process 必须
指向本快照里的 GPU。

没有 NVIDIA runtime 或没有 GPU 的服务器，应返回 `gpu_probe_available: false` 和空的 `gpus`、
`processes`。这样 CPU/内存观测仍然可用，同时这一轮不算完整观测，ServerPilot 不会拿这份空列表
去判定已经在册的 GPU 消失了。

ServerPilot 把 stdout 限制在 1 MiB、stderr 限制在 16 KiB，拒绝截断、非 UTF-8、非单个 JSON 对象、
重复 JSON key、未知字段、超限集合和内部不一致的数据。任何拒绝都会让该 endpoint 的本轮观测
失败，保持 fail closed，不会降级成任意 SSH 命令。

中央采集器按持久化的 5 / 10 / 30 秒间隔采集。App 的「刷新」只是重读控制面状态，不会绕过
这个间隔另做一次观测。到期仍没有新观测时，ServerPilot 将其视为连接或采集问题，停止分配对应
资源，不会拿旧数据冒充当前状态。

## 迁移

远端只接受当前 schema v2，不读取、不降级到 schema v1。安装当前脚本后，先在目标服务器上手工运行固定入口并验证 JSON，再启用 `server-script-v1` observation profile。
