---
status: active
superseded-by:
created: 2026-08-30
updated: 2026-08-30
---

# Record: 端点 CPU/内存容量的权威来源

- Subject identity: 一个 endpoint「有多少 CPU 和内存可用」由哪一处代码定义，展示面与准入面如何保持同一口径。

## 当前结论

**权威是 `BrokerService._host_capacity`（service.py）**，它把一次主机观测解析成这个 endpoint 真正拥有的份额：有 cgroup 配额时用配额（`cpu_scope/memory_scope = "container"`），否则退回整机读数（`"host"`）。它的 docstring 已经写明「Every surface reads capacity from here」。

这不是「展示赢还是准入赢」的取舍：容器化服务器上报的 `cpu_count` / `memory_total_mib` 描述的是**整台节点**，不是这个 endpoint 拥有的那一份，所以任何直接读原始字段的地方都在回答错的问题。展示面（`_host_telemetry_dict` 的 `capacity` 块 → GUI 与 MCP `cpu_only_servers`）本来就读 resolver；**准入面没跟上**，这是唯一需要改的一侧。

**已改的四处消费者**（全部改为读 resolver）

- `_eligible_gpus`：`cpu_cores` / `memory_mib` 承诺检查原先对整机 `cpu_count` / `memory_total_mib` 比；`min_available_cpu_cores` 原先用 `cpu_count - load_1m`、`min_available_memory_mib` 用整机 `memory_available_mib`。
- `_resolve_reservation_gpu_ids`：同两条判据的第二份副本。
- `snapshot()` 的 `host_capacity` 卡片：`total_cpu_cores` / `observed_available_cpu_cores` / `total_memory_mib` / `observed_available_memory_mib` 与 `admission_state` 的算式。
- `resource_projection.used.cpu_cores`：原先累加 `load_1m`（整机 1 分钟负载），与容器口径的 capacity 相加会算出 used > capacity。改为 `total - observed_available`。

**「算不出来」一律 fail closed。** 容器 scope 下若尚无 `cpu_utilization_pct` 样本，resolver 的 `cpu_available_cores` 为 `None`——`load_1m` 是机器级的，不能替代。此时：`_eligible_gpus` 与预留解析把该端点排除（计入 `host_telemetry`），`host_capacity` 卡片给 `admission_state = blocked`，理由「this endpoint's own CPU share is not observable yet」。一个采集周期后自愈。

**已观测**：`test_admission_sizes_a_container_endpoint_by_its_cgroup_budget` 钉住 64 核/1 TiB 节点上 60 核/480 GiB 的 endpoint——申请 40 空闲核或 400 GiB 得 `no_capacity`，申请 25 核 / 300 GiB 成功。改动前旧算式会给出 60 核（`64 - load_1m 4.0`）与 819 GiB 而放行两者，所以该用例确实有判别力。全量 `uv run pytest tests/` 627 passed；ruff check 通过（`ruff format --check` 报的重排全部落在本次未触碰的既有代码行）。

**残留 unknown**

- `_host_telemetry_dict` 与 endpoint history 的点位仍并列输出原始 `cpu_count` / `load_1m` / `memory_available_mib`。它们标注为原始观测、与 `capacity` 块并存，未视为第四种定义；但没有任何东西阻止未来的消费者再次误读原始字段。
- `tests/test_agent_policy.py::test_tracked_client_rules_use_only_the_exact_harness_neutral_routine_contract` 失败，与本主题无关：它要求 `CLAUDE.md` 内容严格等于 `@AGENTS.md`，而工作区里的 `CLAUDE.md` 已被 Teamwork bridge 块修改。未处理。

## History

### 2026-08-30 — 准入侧收口到 resolver，容器端点不再按整机核数放行

一次 opus 评审在无关改动中发现：未提交的容器配额工作让展示面改用 cgroup 份额，准入面仍按整机放行——endpoint 对外说 60 核，分配器按 128 核受理。同一份评审另报出一处：同一份快照里存在两种「可用 CPU」定义（resolver 用 CPU 利用率、`host_capacity` 卡片用 1 分钟负载）。两者根因相同：resolver 已经存在，但不是所有消费者都读它。

判定权威为 resolver 而非某一「面」，四处消费者改为读它；`used.cpu_cores` 的 `load_1m` 累加同时改掉，因为它与容器口径的 capacity 不可相加。未新增配置项、开关或兼容分支。
