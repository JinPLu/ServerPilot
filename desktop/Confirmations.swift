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

func confirmLeaseRelease(_ lease: LeaseRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放 \(lease.gpuIDs.count) 张 GPU？"
    alert.informativeText = "请先确认任务已结束。释放不会停止任务。"
    alert.addButton(withTitle: "释放")
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

func confirmEmptyLeaseCleanup(_ lease: LeaseRecord, conflict: Bool) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = conflict ? "确认任务已结束后清理记录？" : "释放这笔空闲占用？"
    alert.informativeText = """
    ServerPilot 会先重新采集这台服务器；只有确认这笔租约覆盖的 GPU 都没有运行中的进程时才会释放。此操作不会停止远端任务。

    \(processObservationSummary(lease.lastProcessObservedAt))
    """
    alert.addButton(withTitle: conflict ? "确认任务已结束后清理" : "释放空闲占用")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult

func confirmEmptyKeepaliveCleanup(gpuCount: Int) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放遗留占卡？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认 \(gpuCount) 张占卡 GPU 都没有运行中的进程时才会释放。"
    alert.addButton(withTitle: "释放遗留占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult

func confirmEndpointDelete(_ endpoint: EndpointRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "从 ServerPilot 移除这台服务器？"
    alert.informativeText = """
    \(endpoint.sshCommand)

    这会停止对本机的监控与协调，并移除本机关联记录，不会停止远端进程。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "从 ServerPilot 移除")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}


func confirmServerGroupDelete(_ group: ServerGroupRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "删除服务器组「\(group.displayName)」？"
    alert.informativeText = """
    只会删除分组，不会停止远端进程，也不会移除组内服务器。组内仍有服务器时删除会失败。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "删除服务器组")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}

// MARK: - Apple Home inspired native interface

