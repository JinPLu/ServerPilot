# Security

## Reporting a vulnerability

Report privately through GitHub Security Advisories for this repository:

https://github.com/JinPLu/ServerPilot/security/advisories/new

Do not open a public issue. Include a minimal reproduction, the affected version or commit, and the impact. Do not include secrets, private keys, or production inventory.

## Supported versions

Only the latest GitHub Release is supported for security fixes. Older releases do not receive backports.

## Threat model

ServerPilot is a loopback control plane for a single local user account. Local processes running as that user are not mutually isolated; they are all effectively operators.

- The service binds to loopback by default (`127.0.0.1` / `::1` / `localhost`). `serverpilot serve` rejects other bind hosts; the macOS daemon requires a loopback `SERVERPILOT_URL`; the Windows launcher binds `127.0.0.1`.
- There is no authentication. `X-ServerPilot-Actor` is an audit label. Any syntactically valid name becomes an actor with role `allocator` and membership in every project. `allocator` is a mutating role, so any local process can send that header, create endpoints, change keepalive policy, claim GPUs, or present another actor's id and release that actor's leases. Desktop operator routes additionally trust an `X-ServerPilot-Client: desktop-app` header, which is also not a credential.
- The SSH user and what the remote host reports are trusted. Admission is fail-closed on *reported* facts: stale telemetry, connection errors, unknown processes, and resource conflicts refuse allocation. A host whose `nvidia-smi` omits compute processes yields `AVAILABLE`, so the account ServerPilot connects as must be one you would trust with that answer.
- ServerPilot starts and stops its own per-GPU keepalive CUDA processes (about 80% of card VRAM) and, for plugins that declare `apply` / `release`, the plugin's allocation. It does not start, stop, migrate, or preempt the user's own workloads.
- Keepalive workers are launched with `start_new_session=True` and survive the control plane. If the daemon stops, occupied GPUs stay occupied until the daemon returns and reconciles, or until someone stops the workers on the server. There is no automatic release path for that case.

Do not expose the service outside loopback. Do not place credentials, private keys, host inventories with secrets, or production telemetry in issues or pull requests. Non-loopback deployment, authentication, remote lifecycle control of user workloads, and automatic allocation require a separate security review and the owner's explicit approval.
