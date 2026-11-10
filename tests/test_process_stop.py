"""Returning a task in the App ends its processes, and only provably its own."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from serverpilot.adapters import RawSSHResult
from serverpilot.config import EndpointConfig
from serverpilot.process_stop import _SECTION, Card, StopPlan, StopTarget, stop_processes

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
        #: The container's own processes with a GPU open, by their local PIDs.
        self.container: dict[int, dict] = {}

    async def run(self, endpoint: EndpointConfig, command: str) -> RawSSHResult:
        if self.fail:
            return RawSSHResult(returncode=255, stdout="", stderr="ssh: connect to host gpu.example: timed out")
        if command.startswith("kill -"):
            _, signal, *pids = command.split(" 2>")[0].split()
            pids = [int(pid) for pid in pids]
            self.signals.append((signal[1:], pids))
            for pid in pids:
                process = self.processes.get(pid) or self.container.get(pid)
                if process and (signal == "-KILL" or not process.get("ignores_term")):
                    process["alive"] = False
            return RawSSHResult(returncode=0, stdout="", stderr="")
        if command.startswith("id -u;"):
            rows = "".join(
                f"{pid} {item['uid']} {item['ticks']} {item['elapsed']} {item['cvd']}|{item['order']}|{item['comm']}\n"
                for pid, item in self.container.items() if item.get("alive", True)
            )
            return RawSSHResult(returncode=0, stdout=f"{self.login_uid}\n{_SECTION}\n{rows}", stderr="")
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


def stop(server: FakeServer, targets: list[StopTarget], **plan) -> dict[int, tuple[str, str | None]]:
    outcomes = asyncio.run(stop_processes(ENDPOINT, StopPlan(seen=targets, **plan), run=server.run, sleep=_no_wait))
    return {item["pid"]: (item["result"], item["detail"]) for item in outcomes}


def worker(cvd: str, *, order: str = "PCI_BUS_ID", uid: int = 1000, elapsed: int = 600,
           ignores_term: bool = False) -> dict:
    return {"uid": uid, "ticks": 4_330_117_992, "elapsed": elapsed, "cvd": cvd, "order": order,
            "comm": "python", "alive": True, "ignores_term": ignores_term}


# The driver's PIDs for this task's two cards, which the container cannot see.
UNSEEN = [(3_467_697, "[Not Found]"), (3_458_764, "[Not Found]")]
CARDS = [Card(0, "GPU-aaaa-0"), Card(1, "GPU-bbbb-1")]


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


def test_a_container_task_is_found_by_its_cards_and_nothing_else_is_touched() -> None:
    server = FakeServer({})
    server.container = {
        101: worker("0"),
        102: worker("GPU-bbbb"),
        103: worker("2"),                   # another task's card
        104: worker(""),                     # nobody can say whose
        105: worker("1", order=""),          # an index without the PCI order
        106: worker("0", elapsed=90_000),    # older than the task
        107: worker("0", uid=0),             # another account
    }

    outcomes = stop(server, [], unseen=UNSEEN, cards=CARDS, task_age_seconds=3_600)

    assert outcomes[101] == ("stopped", "容器内进程，CUDA_VISIBLE_DEVICES=0")
    assert outcomes[102] == ("stopped", "容器内进程，CUDA_VISIBLE_DEVICES=GPU-bbbb")
    assert outcomes[104][0] == outcomes[105][0] == outcomes[106][0] == "skipped"
    assert 103 not in outcomes and 107 not in outcomes
    assert server.signals == [("TERM", [101, 102])]
    assert all(server.container[pid]["alive"] for pid in (103, 104, 105, 106, 107))


def test_an_unseen_process_is_never_reported_gone() -> None:
    server = FakeServer({})
    server.container = {103: worker("2")}

    outcomes = stop(server, [], unseen=UNSEEN, cards=CARDS, task_age_seconds=3_600)

    assert {pid: result for pid, (result, _detail) in outcomes.items()} == {
        3_467_697: "skipped", 3_458_764: "skipped",
    }
    assert "看不到这个 PID" in (outcomes[3_467_697][1] or "")
    assert server.signals == []


def test_a_container_process_that_ignores_sigterm_gets_sigkill() -> None:
    server = FakeServer({})
    server.container = {201: worker("0", ignores_term=True), 202: worker("1")}

    outcomes = stop(server, [], unseen=UNSEEN, cards=CARDS, task_age_seconds=3_600)

    assert outcomes[201][0] == "killed" and outcomes[202][0] == "stopped"
    assert server.signals == [("TERM", [201, 202]), ("KILL", [201])]
