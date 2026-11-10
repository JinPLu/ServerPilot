"""Stopping a returned task's processes on one server, and nothing else.

A person who returns a task in the App is ending its work, not only asking the
ledger to forget it: returning used to leave the job running, and the cards
then read as held by a foreign process. This is the one place ServerPilot sends
a signal to a process it did not start, so it stays narrow on purpose.

A process the collector could name (``ps`` saw its PID) is stopped by that PID,
and only while it is still that process: the server has not rebooted, the PID
still has the start time it was observed with (a reused PID is somebody
else's), and it belongs to the account ServerPilot logs in as, compared by uid
because ``ps`` truncates long user names.

A process the collector could not name is the usual case on a container: the
driver reports the host's PID, which the container's ``ps`` cannot see, so that
PID cannot be signalled from here. The container's own processes are found
instead, and one is stopped only when all of this holds:

* it is the login account's and has a GPU device open;
* its ``CUDA_VISIBLE_DEVICES`` names only this task's cards, by UUID or, under
  ``CUDA_DEVICE_ORDER=PCI_BUS_ID``, by the index nvidia-smi prints, which is
  the environment gpu_apply hands an agent (a process can open every card's
  device file, so the device files alone cannot say whose it is);
* it has not been running longer than the task has existed.

Each is checked again by its kernel start time before every signal. SIGTERM
first, so the program can save and exit, and SIGKILL only for what is still
there ``TERM_GRACE_SECONDS`` later. Every target is answered by name, and a
process that could not be found is never reported as gone. Agents never reach
this: they return their own tasks when the work has already ended.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from serverpilot.adapters import RawSSHResult, run_sealed_ssh
from serverpilot.collector import parse_ps_output
from serverpilot.config import SSH_BUDGETS, EndpointConfig

TERM_GRACE_SECONDS = 10
#: How much longer than the task a container process may seem to have run:
#: the two clocks behind that comparison are a second apart at worst, and an
#: agent starts its job after the claim returns.
START_SLACK_SECONDS = 120
_SECTION = "__SERVERPILOT_STOP__"

Runner = Callable[[EndpointConfig, str], Awaitable[RawSSHResult]]


@dataclass(frozen=True)
class StopTarget:
    """A process the collector could name, by the PID ``ps`` saw."""

    pid: int
    boot_id: str
    started_at: datetime
    executable: str


@dataclass(frozen=True)
class Card:
    index: int
    uuid: str


@dataclass(frozen=True)
class StopPlan:
    """Everything to stop on one server for one task."""

    seen: list[StopTarget] = field(default_factory=list)
    #: Driver PIDs the collector could not name, and what nvidia-smi called them.
    unseen: list[tuple[int, str]] = field(default_factory=list)
    #: The task's cards on this server, for finding unseen work by its devices.
    cards: list[Card] = field(default_factory=list)
    #: How long ago the task was issued, measured on this machine.
    task_age_seconds: float = 0.0


class ProcessStopError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Seen:
    boot_id: str
    login_uid: int
    #: PID to (uid, start time) for the targets still running.
    alive: dict[int, tuple[int, datetime]]


@dataclass(frozen=True)
class _GpuProcess:
    """A process of the server's own namespace with a GPU device open."""

    pid: int
    uid: int
    #: Kernel start time in clock ticks since boot: the same process while it
    #: matches, whatever its PID is reused for later.
    start_ticks: int
    elapsed_seconds: int
    visible_devices: str
    device_order: str
    comm: str


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


# One line per process with a GPU device open: pid, uid, start ticks, elapsed
# seconds, then CUDA_VISIBLE_DEVICES|CUDA_DEVICE_ORDER|comm.  Fixed text: nothing
# from the task reaches it.
_GPU_PROCESSES_COMMAND = (
    "id -u; "
    f"printf '%s\\n' {_SECTION}; "
    "for d in /proc/[0-9]*; do "
    "ls -l \"$d/fd\" 2>/dev/null | grep -q '/dev/nvidia[0-9]' || continue; "
    "p=${d#/proc/}; "
    "env_of() { tr '\\0' '\\n' < \"$d/environ\" 2>/dev/null | sed -n \"s/^$1=//p\" | head -1; }; "
    "printf '%s %s %s %s %s|%s|%s\\n' \"$p\" \"$(stat -c %u \"$d\" 2>/dev/null)\" "
    "\"$(sed 's/^.*) //' \"$d/stat\" 2>/dev/null | cut -d' ' -f20)\" "
    "\"$(ps -o etimes= -p \"$p\" 2>/dev/null | tr -d ' ')\" "
    "\"$(env_of CUDA_VISIBLE_DEVICES)\" \"$(env_of CUDA_DEVICE_ORDER)\" \"$(cat \"$d/comm\" 2>/dev/null)\"; "
    "done; true"
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


def _parse_gpu_processes(stdout: str) -> tuple[int, list[_GpuProcess]]:
    parts = stdout.split(f"{_SECTION}\n")
    if len(parts) != 2:
        raise ProcessStopError("服务器没有按预期回答显卡进程查询")
    try:
        login_uid = int(parts[0].strip())
    except ValueError as exc:
        raise ProcessStopError("服务器没有回答登录账号的 uid") from exc
    found: list[_GpuProcess] = []
    for line in parts[1].splitlines():
        head, _, tail = line.partition("|")
        fields = head.split(" ")
        if len(fields) != 5 or not all(field.isdigit() for field in fields[:4]):
            continue
        order, _, comm = tail.partition("|")
        found.append(_GpuProcess(
            pid=int(fields[0]), uid=int(fields[1]), start_ticks=int(fields[2]),
            elapsed_seconds=int(fields[3]), visible_devices=fields[4], device_order=order, comm=comm,
        ))
    return login_uid, found


def _whose(process: _GpuProcess, cards: list[Card]) -> tuple[str, str | None]:
    """("ours", None) when the process may use only this task's cards;
    ("other", None) when it names a card of somebody else's; ("unknown", why)
    when its environment cannot say."""

    names = [name.strip() for name in process.visible_devices.split(",") if name.strip()]
    if not names:
        return "unknown", "没有设置 CUDA_VISIBLE_DEVICES，看不出它属于哪个任务"
    indexes = {str(card.index) for card in cards}
    for name in names:
        if name.isdigit():
            if process.device_order != "PCI_BUS_ID":
                return "unknown", "CUDA_VISIBLE_DEVICES 用编号却没有设 CUDA_DEVICE_ORDER=PCI_BUS_ID，编号可能对不上"
            if name not in indexes:
                return "other", None
        elif not (name.startswith("GPU-") and any(card.uuid.startswith(name) for card in cards)):
            return "other", None
    return "ours", None


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


async def _stop_seen(
    endpoint: EndpointConfig, targets: list[StopTarget], run: Runner, sleep: Callable[[float], Awaitable[Any]],
) -> dict[int, tuple[str, str | None]]:
    outcomes: dict[int, tuple[str, str | None]] = {}
    seen = _parse(await _ask(run, endpoint, _identity_command([t.pid for t in targets])))
    verified: list[StopTarget] = []
    for target in targets:
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
    if not verified:
        return outcomes
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
            outcomes[target.pid] = ("failed", "收到 SIGKILL 后仍在运行") if target in left else ("killed", None)
    return outcomes


async def _stop_unseen(
    endpoint: EndpointConfig, plan: StopPlan, run: Runner, sleep: Callable[[float], Awaitable[Any]],
) -> list[dict[str, Any]]:
    """Find, by its cards, the work the driver PIDs stand for, and stop that."""

    login_uid, found = _parse_gpu_processes(await _ask(run, endpoint, _GPU_PROCESSES_COMMAND))
    chosen: list[_GpuProcess] = []
    passed_over: list[dict[str, Any]] = []
    for process in found:
        if process.uid != login_uid:
            continue
        verdict, why = _whose(process, plan.cards)
        if verdict == "other":
            continue
        if verdict == "unknown":
            # It might have been this task's, so it is named, and left alone.
            passed_over.append({"pid": process.pid, "executable": process.comm,
                                "result": "skipped", "detail": f"容器内进程：{why}"})
            continue
        if process.elapsed_seconds > plan.task_age_seconds + START_SLACK_SECONDS:
            passed_over.append({"pid": process.pid, "executable": process.comm, "result": "skipped",
                                "detail": "容器内进程：在这个任务开始之前就已运行"})
            continue
        chosen.append(process)
    if not chosen:
        return passed_over + [
            {"pid": pid, "executable": executable, "result": "skipped",
             "detail": "服务器上看不到这个 PID（容器里 nvidia-smi 报的是宿主机 PID），也没有找到用这些卡的本账号进程"}
            for pid, executable in plan.unseen
        ]

    def same(now: list[_GpuProcess], wanted: list[_GpuProcess]) -> list[_GpuProcess]:
        current = {(item.pid, item.start_ticks) for item in now}
        return [item for item in wanted if (item.pid, item.start_ticks) in current]

    outcomes: dict[int, tuple[str, str | None]] = {}
    await _ask(run, endpoint, _signal_command("TERM", [item.pid for item in chosen]))
    await sleep(TERM_GRACE_SECONDS)
    stubborn = same(_parse_gpu_processes(await _ask(run, endpoint, _GPU_PROCESSES_COMMAND))[1], chosen)
    for item in chosen:
        if item not in stubborn:
            outcomes[item.pid] = ("stopped", None)
    if stubborn:
        await _ask(run, endpoint, _signal_command("KILL", [item.pid for item in stubborn]))
        await sleep(1)
        left = same(_parse_gpu_processes(await _ask(run, endpoint, _GPU_PROCESSES_COMMAND))[1], stubborn)
        for item in stubborn:
            outcomes[item.pid] = ("failed", "收到 SIGKILL 后仍在运行") if item in left else ("killed", None)
    return passed_over + [
        {"pid": item.pid, "executable": item.comm, "result": outcomes[item.pid][0],
         "detail": outcomes[item.pid][1] or f"容器内进程，CUDA_VISIBLE_DEVICES={item.visible_devices}"}
        for item in chosen
    ]


async def stop_processes(
    endpoint: EndpointConfig,
    plan: StopPlan,
    *,
    run: Runner,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> list[dict[str, Any]]:
    """Stop what this task runs on one server, and say what happened to each."""

    seen = list({target.pid: target for target in plan.seen}.values())
    report: list[dict[str, Any]] = []
    try:
        if seen:
            outcomes = await _stop_seen(endpoint, seen, run, sleep)
            report += [
                {"pid": t.pid, "executable": t.executable, "result": outcomes[t.pid][0], "detail": outcomes[t.pid][1]}
                for t in seen
            ]
        if plan.unseen:
            report += await _stop_unseen(endpoint, plan, run, sleep)
    except (OSError, TimeoutError, ProcessStopError) as exc:
        answered = {item["pid"] for item in report}
        report += [
            {"pid": pid, "executable": executable, "result": "failed", "detail": f"未能在服务器上执行：{exc}"}
            for pid, executable in [(t.pid, t.executable) for t in seen] + list(plan.unseen)
            if pid not in answered
        ]
    return report
