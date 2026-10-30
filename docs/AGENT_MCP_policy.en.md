# ServerPilot — Agent MCP Rules

Use the local `serverpilot` MCP.

1. `gpu_status(task?, server_id?, telemetry?)`
2. `gpu_apply(task, gpu_count=1, server_group_id?, server_id?, duration_seconds?, change?, take_over?, project?)` — declares what a task needs (task=what this work is, stable across restarts)
3. `gpu_release(task|lease_id, gpu_ids?, outcome=completed)`

These three tools are the whole MCP surface; server deletion and other lifecycle work happen in the app or through REST.

Every reply has ok. ok=false carries error{code,message,retryable,retry_after_seconds?,next?,details?}: an answer to act on. Follow error.next when present.

gpu_apply gives cards now or says why; nothing queues. gpu_count comes from your launch config, never from free capacity. Choose server_group_id for grouped hosts; server_id must not pin a grouped host. To wait, repeat it after retry_after_seconds; eta=null means no honest estimate. Repeating it is always safe: it returns your entry after a restart and keeps idle cards you still want. change=true grows or shrinks the task, and a running process cannot see added cards. Sessions of one project share its tasks; take_over=true takes one from another project or from before sessions. duration_seconds is required on clusters, whose jobs end at that limit.

Enter each server in servers[]: ssh, cwd, then set env. cwd is a working directory, never a code repository. Allocation entries require entering the existing provider allocation first: entry.argv opens a terminal; without one, prefix commands with entry.exec_prefix. verified=false means probe before use. Never guess a selector.

gpu_release ends the task, with outcome, when it finishes or fails; gpu_ids returns only some cards. Zero running GPUs alone does not mean a task is finished. Idle cards beside working ones are reclaimed after 30 minutes, and a wholly idle task after an hour unless you call gpu_status(task); changes reports what happened.

gpu_status() lists free cards per group, largest_block (one apply's maximum), your tasks in mine, and recoverable tasks. Read group notes before first use. gpu_status(server_id) shows one server. Card status: free (includes ServerPilot's own keepalive hold, stopped before allocation), in_use, held_idle (held, nothing computing), foreign (a process ServerPilot does not own), conflict, unknown (stale data; fails closed), fault, provider.

ServerPilot only coordinates GPUs. Do not use SSH, SQLite, inventory or nvidia-smi to work around it. Non-GPU remote work such as syncing a repository needs no lease.
