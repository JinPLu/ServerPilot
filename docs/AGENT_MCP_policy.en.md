# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP.

1. `gpu_status(server_id?, lease_id?)` returns grouped allocatable capacity per server — `name`, `vram_mib`, `total_count`, `available_count`, no telemetry — under `server_groups[]` or `ungrouped_servers[]`, plus compact `busy_gpus` naming each busy card's human-readable task; `server_id` narrows scope. Assess group workspace/environment/data-weight notes and capacity first. Telemetry is readable only on cards you hold: `gpu_status(lease_id=…)` adds `leased_gpus` with `recent_average` and a lease summary (`min_memory_free_mib`, `slowest_gpu`) for tuning batch size and parallelism. Load on an unclaimed card is ServerPilot's own hold, stopped before allocation, never evidence the card is taken.
2. `gpu_apply(server_group_id?, server_id?, gpu_count=1, task?)` allocates GPUs; pass `server_group_id` for direct grouped hosts so the broker best-fits within the group. `server_id` is for scheduler/plugin or explicit ungrouped compatibility and must not pin a grouped direct host. Set `gpu_count` from launch script/config (`devices`, `--nproc_per_node`, `num_processes`, `--gres`), never from server or free capacity. Never use a UI title or GPU ID.
3. `gpu_release(lease_id)` releases one allocation, echoing its settled `state`.

Connection and workspace live per server, not per GPU: `ssh` connects; `workspace.path` (`workspace_path`) is remote cwd, never a repository path. Allocation `gpus[]` keep CUDA rows. UUIDs identify, never select; `cd` to the workspace. Set `CUDA_DEVICE_ORDER=PCI_BUS_ID`; `cuda_visible_devices` is that server's ordinal set; each `gpus[]` row has its own `gpu_cuda_visible_devices`. CUDA failure requires release and avoiding that server.

`no_capacity` and `group_selection_required` mean no allocation or queue; `busy_gpus` names holders. Refresh once, then wait. Retry `Transport closed` once. Leases persist until released, App correction, or idle reclaim; confirm each `released`, and claim only GPUs you will use because an idle one is returned on its own.

ServerPilot provides GPU coordination only. Direct SSH is normal; never bypass discovery, selection, allocation, or release via SSH, SQLite, inventory, or `nvidia-smi`. Non-GPU remote work, including Git synchronization, does not require a ServerPilot lease. Advanced compatibility tools are outside the routine path.
