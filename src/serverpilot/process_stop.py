"""Stopping a returned task's processes on one server, and nothing else.

A person who returns a task in the App is ending its work, not only asking the
ledger to forget it: returning used to leave the job running, and the cards
then read as held by a foreign process. This is the one place ServerPilot sends
a signal to a process it did not start, so it stays narrow on purpose:

* only processes the collector last saw on the cards this task holds;
* only one that is still that process: the server has not rebooted, and the
  PID still has the start time it was observed with, since a reused PID is
  somebody else's;
* only one owned by the account ServerPilot logs in as, compared by uid
  because ``ps`` truncates long user names;
* SIGTERM first, so the program can save and exit, and SIGKILL only for what
  is still there ``TERM_GRACE_SECONDS`` later.

Every target is answered by name. Agents never reach this: they return their
own tasks when the work has already ended.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from serverpilot.adapters import RawSSHResult, run_sealed_ssh
from serverpilot.collector import parse_ps_output
from serverpilot.config import SSH_BUDGETS, EndpointConfig

TERM_GRACE_SECONDS = 10
_SECTION = "__SERVERPILOT_STOP__"

Runner = Callable[[EndpointConfig, str], Awaitable[RawSSHResult]]


@dataclass(frozen=True)
class StopTarget:
    pid: int
    boot_id: str
    started_at: datetime
    executable: str


class ProcessStopError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Seen:
    boot_id: str
    login_uid: int
    #: PID to (uid, start time) for the targets still running.
    alive: dict[int, tuple[int, datetime]]


def sealed_runner(control_dir: Path) -> Runner:
    async def run(endpoint: EndpointConfig, command: str) -> RawSSHResult:
        return await run_sealed_ssh(
            endpoint,
            remote_command=command,
            control_dir=control_dir,
            deadline_seconds=SSH_BUDGETS.probe_deadline_seconds,
            what="process stop",
        )

    return run


def _pid_list(pids: list[int]) -> list[str]:
    # Only integers ever reach the remote command.
    return [str(int(pid)) for pid in pids if int(pid) > 0]


def _identity_command(pids: list[int]) -> str:
    joined = ",".join(_pid_list(pids))
    return (
        "cat /proc/sys/kernel/random/boot_id; "
        f"printf '%s\\n' {_SECTION}; id -u; "
        f"printf '%s\\n' {_SECTION}; ps -o pid=,uid= -p {joined} 2>/dev/null; "
        f"printf '%s\\n' {_SECTION}; TZ=UTC LC_ALL=C ps -o pid=,user=,lstart=,comm= -p {joined} 2>/dev/null; "
        "true"
    )


def _signal_command(signal: str, pids: list[int]) -> str:
    assert signal in {"TERM", "KILL"}
    return f"kill -{signal} {' '.join(_pid_list(pids))} 2>/dev/null; true"


def _parse(stdout: str) -> _Seen:
    parts = stdout.split(f"{_SECTION}\n")
    if len(parts) != 4:
        raise ProcessStopError("服务器没有按预期回答进程查询")
    boot_id = parts[0].strip()
    try:
        login_uid = int(parts[1].strip())
    except ValueError as exc:
        raise ProcessStopError("服务器没有回答登录账号的 uid") from exc
    uids: dict[int, int] = {}
    for line in parts[2].splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            uids[int(fields[0])] = int(fields[1])
    starts = parse_ps_output(parts[3])
    alive = {pid: (uid, starts[pid][1]) for pid, uid in uids.items() if pid in starts}
    return _Seen(boot_id=boot_id, login_uid=login_uid, alive=alive)


async def _ask(run: Runner, endpoint: EndpointConfig, command: str) -> str:
    result = await run(endpoint, command)
    if result.returncode != 0 or result.stdout_truncated:
        detail = result.stderr.strip().replace("\n", " ")[:200]
        raise ProcessStopError(detail or f"SSH 返回 {result.returncode}")
    return result.stdout


def _still_running(seen: _Seen, targets: list[StopTarget]) -> list[StopTarget]:
    return [
        target
        for target in targets
        if seen.boot_id == target.boot_id and seen.alive.get(target.pid, (None, None))[1] == target.started_at
    ]


async def stop_processes(
    endpoint: EndpointConfig,
    targets: list[StopTarget],
    *,
    run: Runner,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> list[dict[str, Any]]:
    """Stop each target that is provably still the process that was observed."""

    unique = list({target.pid: target for target in targets}.values())
    if not unique:
        return []
    outcomes: dict[int, tuple[str, str | None]] = {}
    try:
        seen = _parse(await _ask(run, endpoint, _identity_command([t.pid for t in unique])))
        verified: list[StopTarget] = []
        for target in unique:
            if seen.boot_id != target.boot_id:
                outcomes[target.pid] = ("skipped", "服务器重启过，这个 PID 已不是原来的进程")
            elif target.pid not in seen.alive:
                outcomes[target.pid] = ("already_gone", None)
            elif seen.alive[target.pid][1] != target.started_at:
                outcomes[target.pid] = ("skipped", "这个 PID 已属于另一个进程")
            elif seen.alive[target.pid][0] != seen.login_uid:
                outcomes[target.pid] = ("skipped", "不属于 ServerPilot 登录的账号")
            else:
                verified.append(target)
        if verified:
            await _ask(run, endpoint, _signal_command("TERM", [t.pid for t in verified]))
            await sleep(TERM_GRACE_SECONDS)
            seen = _parse(await _ask(run, endpoint, _identity_command([t.pid for t in verified])))
            stubborn = _still_running(seen, verified)
            for target in verified:
                if target not in stubborn:
                    outcomes[target.pid] = ("stopped", None)
            if stubborn:
                await _ask(run, endpoint, _signal_command("KILL", [t.pid for t in stubborn]))
                await sleep(1)
                seen = _parse(await _ask(run, endpoint, _identity_command([t.pid for t in stubborn])))
                left = _still_running(seen, stubborn)
                for target in stubborn:
                    outcomes[target.pid] = (
                        ("failed", "收到 SIGKILL 后仍在运行") if target in left else ("killed", None)
                    )
    except (OSError, TimeoutError, ProcessStopError) as exc:
        for target in unique:
            outcomes.setdefault(target.pid, ("failed", f"未能在服务器上执行：{exc}"))
    return [
        {
            "pid": target.pid,
            "executable": target.executable,
            "result": outcomes[target.pid][0],
            "detail": outcomes[target.pid][1],
        }
        for target in unique
    ]
