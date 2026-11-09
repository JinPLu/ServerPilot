"""Returning a task in the App ends its processes, and only provably its own."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from serverpilot.adapters import RawSSHResult
from serverpilot.config import EndpointConfig
from serverpilot.process_stop import _SECTION, StopTarget, stop_processes

STARTED = datetime(2026, 11, 10, 1, 2, 3, tzinfo=UTC)
LATER = datetime(2026, 11, 10, 5, 6, 7, tzinfo=UTC)
ENDPOINT = EndpointConfig(id="server-a", host="gpu.example", ssh_user="alice", port=22, workspace_path="/srv/work")


class FakeServer:
    """Answers the three commands process_stop sends, like a Linux host would."""

    def __init__(self, processes: dict[int, dict], *, boot: str = "boot-1", login_uid: int = 1000) -> None:
        self.processes = processes
        self.boot = boot
        self.login_uid = login_uid
        self.signals: list[tuple[str, list[int]]] = []
        self.fail = False

    async def run(self, endpoint: EndpointConfig, command: str) -> RawSSHResult:
        if self.fail:
            return RawSSHResult(returncode=255, stdout="", stderr="ssh: connect to host gpu.example: timed out")
        if command.startswith("kill -"):
            _, signal, *pids = command.split(" 2>")[0].split()
            pids = [int(pid) for pid in pids]
            self.signals.append((signal[1:], pids))
            for pid in pids:
                process = self.processes.get(pid)
                if process and (signal == "-KILL" or not process.get("ignores_term")):
                    process["alive"] = False
            return RawSSHResult(returncode=0, stdout="", stderr="")
        asked = [int(pid) for pid in command.split("ps -o pid=,uid= -p ")[1].split(" ")[0].split(",")]
        live = {pid: self.processes[pid] for pid in asked if self.processes.get(pid, {}).get("alive", False)}
        uids = "".join(f"{pid} {item['uid']}\n" for pid, item in live.items())
        rows = "".join(
            f"{pid} {item['user']} {item['started'].strftime('%a %b %d %H:%M:%S %Y')} {item['comm']}\n"
            for pid, item in live.items()
        )
        stdout = f"{self.boot}\n{_SECTION}\n{self.login_uid}\n{_SECTION}\n{uids}{_SECTION}\n{rows}"
        return RawSSHResult(returncode=0, stdout=stdout, stderr="")


def process(*, uid: int = 1000, started: datetime = STARTED, ignores_term: bool = False) -> dict:
    return {"uid": uid, "user": "alice", "started": started, "comm": "python", "alive": True,
            "ignores_term": ignores_term}


def target(pid: int, *, boot: str = "boot-1") -> StopTarget:
    return StopTarget(pid=pid, boot_id=boot, started_at=STARTED, executable="python")


async def _no_wait(_seconds: float) -> None:
    return None


def stop(server: FakeServer, targets: list[StopTarget]) -> dict[int, tuple[str, str | None]]:
    outcomes = asyncio.run(stop_processes(ENDPOINT, targets, run=server.run, sleep=_no_wait))
    return {item["pid"]: (item["result"], item["detail"]) for item in outcomes}


def test_only_the_observed_process_owned_by_the_login_account_is_signalled() -> None:
    server = FakeServer({
        11: process(),
        12: process(started=LATER),
        13: process(uid=0),
    })

    outcomes = stop(server, [target(11), target(12), target(13), target(14)])

    assert outcomes[11] == ("stopped", None)
    assert outcomes[12] == ("skipped", "这个 PID 已属于另一个进程")
    assert outcomes[13] == ("skipped", "不属于 ServerPilot 登录的账号")
    assert outcomes[14] == ("already_gone", None)
    assert server.signals == [("TERM", [11])]
    assert server.processes[12]["alive"] and server.processes[13]["alive"]


def test_what_ignores_sigterm_gets_sigkill_after_the_grace_period() -> None:
    server = FakeServer({21: process(ignores_term=True), 22: process()})

    outcomes = stop(server, [target(21), target(22)])

    assert outcomes == {21: ("killed", None), 22: ("stopped", None)}
    assert server.signals == [("TERM", [21, 22]), ("KILL", [21])]


def test_a_rebooted_server_has_none_of_the_observed_processes() -> None:
    server = FakeServer({31: process()}, boot="boot-2")

    assert stop(server, [target(31)]) == {31: ("skipped", "服务器重启过，这个 PID 已不是原来的进程")}
    assert server.signals == []


def test_a_server_that_does_not_answer_is_reported_not_guessed() -> None:
    server = FakeServer({41: process()})
    server.fail = True

    result, detail = stop(server, [target(41)])[41]

    assert result == "failed"
    assert "ssh: connect to host" in (detail or "")
    assert server.signals == []
