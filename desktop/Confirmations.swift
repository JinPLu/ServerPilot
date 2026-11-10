import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

enum DesktopError: LocalizedError {
    case projectRootMissing
    case brokerExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "应用内置运行资源不完整。请重新安装 ServerPilot.app，或为开发构建设置 SERVERPILOT_ROOT。"
        case .brokerExecutableMissing:
            return "未找到已安装的 ServerPilot CLI。请在仓库根目录执行 `uv tool install --force .` 安装或升级命令行工具后重新打开应用。开发或测试可设置 SERVERPILOT_CLI 指向可执行文件。"
        case .commandFailed(let details):
            return details
        }
    }
}

// MARK: - Native desktop shell

@MainActor

enum ServiceProbeResult {
    case compatible(ServiceInfo)
    case incompatible(String)
    case unavailable
}

@discardableResult

private func processLines(_ processes: [GPUProcessRecord]) -> String {
    let listed = processes.prefix(6).map { process in
        let memory = process.memoryMiB.map { " · \(memoryMiBLabel($0))" } ?? ""
        return "PID \(process.pid) · \(process.executable) · \(process.username ?? "未知账号")\(memory)"
    }
    return listed.joined(separator: "\n") + (processes.count > 6 ? "\n另有 \(processes.count - 6) 个" : "")
}

/// Returning a task ends it: name the processes that will be stopped, and how,
/// apart from the ones it will leave running.
func confirmLeaseRelease(
    _ lease: LeaseRecord,
    processes: (ending: [GPUProcessRecord], leaving: [GPUProcessRecord])
) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    let leaving = processes.leaving.isEmpty
        ? ""
        : "\n\n以下进程属于其他账号或集群节点，不会被结束，会继续占着卡：\n" + processLines(processes.leaving)
    if lease.allocations.contains(where: { $0.releaseGranularity == "allocation" }) {
        alert.messageText = "归还 \(lease.gpuCount) 张 GPU？"
        alert.informativeText = "将归还整个资源分配，提供方可能终止其中仍在运行的程序。请先确认任务已结束。"
        alert.addButton(withTitle: "归还")
    } else if processes.ending.isEmpty {
        alert.messageText = "归还 \(lease.gpuCount) 张 GPU？"
        alert.informativeText = processes.leaving.isEmpty
            ? "这个任务的卡上没有看到运行中的进程。"
            : "ServerPilot 不会结束任何进程。" + leaving
        alert.addButton(withTitle: "归还")
    } else {
        alert.messageText = "归还 \(lease.gpuCount) 张 GPU，并结束 \(processes.ending.count) 个进程？"
        alert.informativeText = processLines(processes.ending) + "\n\n"
            + "ServerPilot 会先在服务器上核对每个进程仍是这个任务的、属于登录账号，"
            + "再发送 SIGTERM；10 秒后仍在运行的发送 SIGKILL。" + leaving
        alert.addButton(withTitle: "归还并结束进程")
        alert.buttons.first?.hasDestructiveAction = true
    }
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

/// Confirm the endpoint-wide occupancy switch, and say that it is one.
///
/// Occupancy itself is per GPU, but this control is the server's policy, and
/// the two were easy to confuse: to free one card for an agent, a person turned
/// the whole server off, took every worker down with it, and turned it back on
/// -- which reoccupied every idle card at once, on top of a job that was still
/// running. Nothing had to be turned off at all: a claim reclaims the cards it
/// needs on its own. So the count is named, and so is the fact that it all
/// comes back together.
@discardableResult

func confirmKeepaliveEnd(activeGPUCount: Int) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "结束这台服务器的占卡？"
    alert.informativeText = "这是整台服务器的占卡开关，不是单张卡的操作：这台服务器上正在占卡的 \(activeGPUCount) 张 GPU 会一起停止，重新开启时也会一起重新占卡。正在运行的任务不会被停止。只是想腾出几张卡给 Agent 的话不用关——Agent 申请 GPU 时，ServerPilot 会自动让出它需要的那几张。"
    alert.addButton(withTitle: "结束占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

/// Describe when this lease last ran something, for the person about to clear it.
///
/// "No process right now" is what the release already proves, and it is not the
/// same as "this lease is finished": a job between two batches looks exactly
/// like one that ended. Clearing the first wedges its cards, because the work
/// comes back to a card that no longer belongs to anyone. Deliberately no
/// "recent = dangerous" threshold — that would rebuild a release standard in
/// the client, and this path exists to rescue exactly the leases no standard
/// can judge. The elapsed time is the fact; the judgement stays with the person.

private func processObservationSummary(_ isoTimestamp: String?) -> String {
    guard let isoTimestamp, let observedAt = endpointTelemetryHistoryDate(isoTimestamp) else {
        return "自这笔租约生效以来，ServerPilot 从未在这些 GPU 上观测到计算进程。"
    }
    let elapsed = Date().timeIntervalSince(observedAt)
    guard elapsed >= 0 else {
        return "最近一次在这些 GPU 上观测到计算进程：\(historyDateTime(observedAt))。"
    }
    return "最近一次在这些 GPU 上观测到计算进程：\(historyElapsedDescription(elapsed))前。"
}

@discardableResult

func confirmEmptyLeaseCleanup(_ lease: LeaseRecord, conflict: Bool, unreachable: Bool) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    // A server that stopped answering cannot prove its cards empty, and the
    // control plane waives that proof; saying it will check would be untrue.
    let how = unreachable
        ? "这台服务器已停止应答，ServerPilot 无法确认这些 GPU 上是否还有进程，会直接结清这条记录。如果任务其实还在运行，它的卡会被当作空闲重新分配。此操作不会停止远端任务。"
        : "ServerPilot 会先重新采集这台服务器；只有确认这笔租约覆盖的 GPU 都没有运行中的进程时才会归还。此操作不会停止远端任务。"
    alert.messageText = conflict
        ? "确认任务已结束后清理记录？"
        : (unreachable ? "结清失联服务器上的这条记录？" : "归还这笔空闲占用？")
    alert.informativeText = """
    \(how)

    \(processObservationSummary(lease.lastProcessObservedAt))
    """
    alert.addButton(withTitle: conflict ? "确认任务已结束后清理" : (unreachable ? "结清记录" : "归还空闲占用"))
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult

func confirmEmptyKeepaliveCleanup(gpuCount: Int) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "归还遗留占卡？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认 \(gpuCount) 张占卡 GPU 都没有运行中的进程时才会归还。"
    alert.addButton(withTitle: "归还遗留占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult

func confirmEndpointsDelete(_ endpoints: [EndpointRecord]) -> Bool {
    let listLimit = 12
    var listed = endpoints.prefix(listLimit).map(\.sshCommand)
    if endpoints.count > listLimit {
        listed.append("…以及另外 \(endpoints.count - listLimit) 台")
    }
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = endpoints.count == 1
        ? "从 ServerPilot 移除这台服务器？"
        : "从 ServerPilot 移除 \(endpoints.count) 台服务器？"
    // Deleting a server that stopped answering also settles its tasks: that
    // is how a person says the machine is gone, and the agents lose the cards.
    let settles = endpoints.contains { $0.monitorStatus != "ONLINE" }
        ? "\n\n已停止应答的服务器上如果还有进行中的任务，会一并结清，Agent 会失去这些卡。"
        : ""
    alert.informativeText = """
    \(listed.joined(separator: "\n"))

    ServerPilot 将不再监控和协调这台服务器，并删除它在本机的记录；不会停止远端进程。\(settles)
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: endpoints.count == 1 ? "从 ServerPilot 移除" : "移除 \(endpoints.count) 台")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}


func confirmServerGroupDelete(_ group: ServerGroupRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "删除服务器组「\(group.displayName)」？"
    alert.informativeText = """
    只会删除分组，不会停止远端进程。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "删除服务器组")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}

// MARK: - Apple Home inspired native interface

