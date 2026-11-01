import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

func durationLabel(_ seconds: Int) -> String {
    if seconds <= 0 { return "\(seconds) 秒" }
    if seconds % 3_600 == 0 {
        let hours = seconds / 3_600
        return hours == 1 ? "1 小时" : "\(hours) 小时"
    }
    if seconds % 60 == 0 {
        let minutes = seconds / 60
        return minutes == 1 ? "1 分钟" : "\(minutes) 分钟"
    }
    if seconds > 3_600 {
        let hours = seconds / 3_600
        let minutes = (seconds % 3_600) / 60
        if minutes == 0 { return hours == 1 ? "1 小时" : "\(hours) 小时" }
        return "\(hours) 小时 \(minutes) 分钟"
    }
    return "\(seconds) 秒"
}


func memoryMiBLabel(_ mib: Int) -> String {
    if mib > 0, mib % 1024 == 0 {
        return "\(mib / 1024) GB"
    }
    return "\(mib) MiB"
}

@MainActor

private func endpointGPUAvailabilitySummary(
    _ endpoints: [EndpointRecord],
    store: BrokerStore
) -> (available: Int, total: Int) {
    let gpus = endpoints.flatMap { store.snapshot.gpus(for: $0) }
    let available = store.freshness == .fresh
        ? endpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .flatMap { store.snapshot.gpus(for: $0) }
            .filter(\.isPubliclyAvailable)
            .count
        : 0
    return (available, gpus.count)
}

/// Inventory counts stay on per-card rows.  Delegated capacity uses the
/// one-apply cap, never pool `free_gpu_count`.  `limits` and
/// `largest_allocatable_block` exist on every group; they are not a
/// delegated signal — only `allocation == delegated` or a member
/// `schedulerCapacity` is.
@MainActor

func endpointGroupCapacitySummary(
    _ endpoints: [EndpointRecord],
    group: ServerGroupRecord?,
    store: BrokerStore
) -> String {
    let inventory = endpointGPUAvailabilitySummary(endpoints, store: store)
    let inventoryPart: String?
    if inventory.total > 0 {
        inventoryPart = store.freshness == .fresh
            ? "\(inventory.available)/\(inventory.total) 空闲"
            : "空闲未确认 · \(inventory.total) 张"
    } else {
        inventoryPart = nil
    }
    let hasOnDemand = group?.allocation == .delegated
        || endpoints.contains { $0.schedulerCapacity != nil }
    let onDemandPart: String?
    if hasOnDemand {
        if let limit = onDemandApplyLimit(group: group, endpoints: endpoints) {
            onDemandPart = "按需申请 · 一次最多 \(limit) 卡"
        } else {
            onDemandPart = "按需申请"
        }
    } else {
        onDemandPart = nil
    }
    switch (inventoryPart, onDemandPart) {
    case (let inventory?, let onDemand?):
        return "\(inventory) · \(onDemand)"
    case (let inventory?, nil):
        return inventory
    case (nil, let onDemand?):
        return onDemand
    case (nil, nil):
        return "无 GPU"
    }
}


private func onDemandApplyLimit(group: ServerGroupRecord?, endpoints: [EndpointRecord]) -> Int? {
    if let block = group?.largestAllocatableBlock { return block }
    if let leaseMax = group?.limits?.maxGPUsPerLease { return leaseMax }
    let endpointLimits = endpoints.compactMap { endpoint -> Int? in
        endpoint.schedulerCapacity?.maxGPUsPerLease ?? endpoint.schedulerCapacity?.largestFreeBlock
    }
    return endpointLimits.max()
}

/// "60 核（容器配额）".  Without the note a container's budget reads as the
/// size of the whole machine, which is the number a caller must not plan with.

func scopedFact(_ text: String, note: String?) -> String {
    guard let note else { return text }
    return "\(text)（\(note)）"
}


func endpointGPUModelSummary(_ gpus: [GPURecord]) -> String {
    guard !gpus.isEmpty else { return "无 GPU" }
    let names = Array(Set(gpus.map(\.name))).sorted()
    guard let first = names.first else { return "无 GPU" }
    return names.count == 1 ? first : "\(first) +\(names.count - 1) 类"
}


func endpointOverviewGPUMemoryFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.memoryFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}


func endpointOverviewGPUUtilizationFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.utilizationFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}


func endpointOverviewCPULoadFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.cpuLoadFraction
}


func endpointOverviewMemoryFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.memoryFraction
}


func percentageLabel(_ value: Double?) -> String {
    value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—"
}

private let endpointHighPressureThreshold = 0.85


private func endpointPressureFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    [
        endpointOverviewCPULoadFraction(endpoint: endpoint),
        endpointOverviewMemoryFraction(endpoint: endpoint),
        endpointOverviewGPUUtilizationFraction(endpoint: endpoint, gpus: gpus)
    ]
    .compactMap { $0 }
    .max()
}


func endpointHighPressure(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    guard let pressure = endpointPressureFraction(endpoint: endpoint, gpus: gpus) else { return false }
    return pressure >= endpointHighPressureThreshold
}


func endpointRequiresAttention(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    endpointNeedsAttention(endpoint)
        || gpus.contains(where: gpuNeedsAttention)
        || endpointHighPressure(endpoint: endpoint, gpus: gpus)
}


/// Colour is for pressure that needs a look.  Below the threshold a bar is
/// neutral, so a row of healthy machines reads as calm rather than as green.
func pressureColor(_ fraction: Double?) -> Color {
    guard let fraction else { return DesignTokens.mutedInk }
    switch fraction {
    case ..<0.70: return DesignTokens.quietBar
    case ..<0.90: return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

/// The same thresholds for the figure beside a bar, which stays ink until then.
func pressureTextColor(_ fraction: Double?) -> Color {
    guard let fraction else { return DesignTokens.mutedInk }
    return fraction < 0.70 ? DesignTokens.ink : pressureColor(fraction)
}


func endpointNeedsAttention(_ endpoint: EndpointRecord) -> Bool {
    ["ERROR", "STALE"].contains(endpoint.monitorStatus)
}


func gpuNeedsAttention(_ gpu: GPURecord) -> Bool {
    [
        "BUSY_UNMANAGED",
        "UNKNOWN_RECOVERING",
        "UNKNOWN_STALE",
        "UNHEALTHY",
        "CONFLICT",
        "ORPHANED_BUSY",
        "MAINTENANCE"
    ].contains(gpu.state)
}

/// Older brokers can report a workload's ordinary worker replacement as a
/// process-attribution conflict.  That is distinct from a keeper or foreign
/// process conflict, both of which remain error/attention states.

func gpuHasLegacyWorkloadProcessReview(_ gpu: GPURecord) -> Bool {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    return gpu.state == "CONFLICT"
        && gpu.stateReason == "lease/process attribution conflict"
        && gpu.keepalive.leaseID == nil
        && task?.isEmpty == false
}


func gpuTaskObservationLabel(_ gpu: GPURecord) -> String? {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        if let task, !task.isEmpty {
            return "任务：\(task) · 观测到进程已更新"
        }
        return "任务已指派 · 观测到进程已更新"
    }
    if let task, !task.isEmpty, !gpu.isPubliclyAvailable {
        return "任务：\(task)"
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "观测到计算任务"
    }
    return nil
}


private func gpuStateColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE": return DesignTokens.success
    // Held is real state but not a warning, and a user-chosen accent could sit
    // anywhere on the luminance scale — a neutral keeps the graphical floor.
    case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.hold
    case "RUNNING_MANAGED": return DesignTokens.interaction
    case "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED", "MAINTENANCE": return DesignTokens.warning
    default: return DesignTokens.danger
    }
}


private func gpuStateLabel(_ state: String) -> String {
    CardVocabulary.label(forState: state)
}


func gpuPresentationLabel(_ gpu: GPURecord) -> String {
    if let publicStatus = gpu.publicStatus {
        return publicStatus
    }
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        return task.map { "运行中 · \($0)" } ?? "运行中 · 进程已更新"
    }
    if gpu.keepalive.state == "ERROR" {
        let reason = gpu.keepalive.reason.map(localizedStateReason) ?? "未知原因"
        return gpu.isPubliclyAvailable
            ? "空闲 · 占卡异常：\(reason)"
            : "占卡异常：\(reason)"
    }
    if gpu.state == "AVAILABLE" {
        return gpu.keepalive.desired == "ON"
            ? "空闲 · 占卡未运行"
            : "空闲"
    }
    if gpu.state == "KEEPALIVE" { return "空闲 · 占卡中" }
    if ["HELD", "LEASED_IDLE", "RUNNING_MANAGED"].contains(gpu.state) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let task, !task.isEmpty else { return gpuStateLabel(gpu.state) }
        return task
    }
    return gpuStateLabel(gpu.state)
}


private func gpuVisualStateColor(_ gpu: GPURecord) -> Color {
    gpuHasLegacyWorkloadProcessReview(gpu) ? DesignTokens.warning : gpuStateColor(gpu.state)
}


func endpointStateIcon(_ state: String) -> String {
    switch state {
    case "ONLINE": return "server.rack"
    case "PENDING": return "hourglass"
    case "STALE": return "clock.badge.exclamationmark"
    case "ERROR": return "exclamationmark.triangle.fill"
    default: return "questionmark.diamond.fill"
    }
}

/// Only a failed probe is red. Waiting for a first answer, or working from a
/// reading that has aged out, is not the same news and is shared by every
/// remaining non-ONLINE status so the mapping lives in one table.

func endpointMonitorStatusColor(_ state: String) -> Color {
    switch state {
    case "ERROR": return DesignTokens.danger
    default: return DesignTokens.warning
    }
}

/// The footer says why the sheet is not offering a claim.

func endpointFooterMessage(_ endpoint: EndpointRecord) -> String {
    switch endpoint.monitorStatus {
    case "ONLINE": return "状态按设定周期自动更新"
    case "PENDING": return "正在进行首次连接，暂不可申请 GPU"
    default: return "当前数据已过期，暂不可申请 GPU"
    }
}


func localizedStateReason(_ reason: String) -> String {
    if reason == "no fresh telemetry after service start" {
        return "正在进行首次连接"
    }
    if reason == "GPU absent from latest complete endpoint observation" {
        return "本次更新未检测到这块 GPU"
    }
    if reason == "lease/process attribution conflict" {
        return "此前观测到的任务进程与租约绑定不匹配；请由所属任务确认当前观测，或在任务结束后归还租约"
    }
    if reason == "lease expired while a compute process remains" {
        return "资源使用记录已到期，但服务器上仍有任务运行"
    }
    if reason == "bound workload process observed" || reason == "compute process observed on assigned GPU" {
        return "已指派任务，当前检测到计算进程"
    }
    if reason == "compute process observed; admission blocked" {
        return "检测到未登记的计算进程，暂不能分配"
    }
    if reason == "exclusive lease active" {
        return "资源已分配给其他项目或任务"
    }
    if reason.hasPrefix("telemetry age "), reason.contains("exceeds stale threshold") {
        return "最近一次服务器数据已过期"
    }
    if reason.hasPrefix("reservation "), reason.contains(" is active") {
        return "预约正在生效"
    }
    return "状态需要人工确认"
}


func formattedTimestamp(_ value: String?) -> String {
    guard let value else { return "未知" }
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return "时间格式异常" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm"
    return formatter.string(from: date)
}


private func historyTimestamp(_ value: String) -> String {
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return value }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: date)
}


// ---- shared across the overview, the server table and the menu bar ----

extension Notification.Name {
    /// The menu bar asks the window to open its claim sheet.
    static let serverPilotRequestClaim = Notification.Name("serverPilotRequestClaim")
}

/// A server group as the overview and the sidebar show it: the groups in name
/// order, then whatever is outside one.
struct ServerFleet: Identifiable {
    let id: String
    let name: String
    let servers: [EndpointRecord]

    @MainActor
    func freeCardCount(in snapshot: BrokerSnapshot) -> Int {
        servers.reduce(0) { total, endpoint in
            if let capacity = endpoint.schedulerCapacity { return total + capacity.freeGPUCount }
            guard endpoint.monitorStatus == "ONLINE" else { return total }
            return total + snapshot.gpus(for: endpoint).filter(\.isPubliclyAvailable).count
        }
    }
}

func serverFleets(in snapshot: BrokerSnapshot) -> [ServerFleet] {
    let shown = snapshot.operationalEndpoints.filter {
        !snapshot.gpus(for: $0).isEmpty || $0.schedulerCapacity != nil || !$0.nodes.isEmpty
    }
    let grouped = Dictionary(grouping: shown) { snapshot.serverGroup(for: $0)?.id ?? "" }
    var fleets = snapshot.serverGroups
        .sorted { $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending }
        .compactMap { group in
            grouped[group.id].map { ServerFleet(id: group.id, name: group.displayName, servers: $0) }
        }
    if let loose = grouped[""] {
        fleets.append(ServerFleet(id: "", name: "未分组", servers: loose))
    }
    return fleets
}

/// The words that answer "can I ask this machine for a GPU", wherever a server
/// is named: what it is doing, then how many cards are still free, because one
/// conflicted GPU must never hide the rest.  A scheduler endpoint has no local
/// inventory and is not a CPU node.
///
/// Unreachable is not one thing: a stale local snapshot is this machine's
/// loopback hop, not the remote server, and monitorLabel already carries the
/// distinct words for a first connection, a late reading and a failed probe.
struct ServerStatus {
    enum Tone { case free, working, held, attention, fault, quiet }

    let word: String
    let tone: Tone

    init(endpoint: EndpointRecord, gpus: [GPURecord], group: ServerGroupRecord?, isSnapshotFresh: Bool) {
        guard isSnapshotFresh else {
            word = "本机未更新"
            tone = .fault
            return
        }
        guard endpoint.monitorStatus == "ONLINE" else {
            word = endpoint.monitorLabel
            tone = ["ERROR", "STALE"].contains(endpoint.monitorStatus) ? .fault : .attention
            return
        }
        if endpoint.schedulerCapacity != nil || group?.allocation == .delegated {
            let limit = group?.largestAllocatableBlock
                ?? group?.limits?.maxGPUsPerLease
                ?? endpoint.schedulerCapacity?.maxGPUsPerLease
                ?? endpoint.schedulerCapacity?.largestFreeBlock
            word = limit.map { "按需申请 · 一次最多 \($0) 卡" } ?? "按需申请"
            let hasCapacity = endpoint.schedulerCapacity.map { $0.freeGPUCount > 0 }
                ?? ((group?.largestAllocatableBlock ?? 0) > 0)
            tone = hasCapacity ? .free : .quiet
            return
        }
        guard !gpus.isEmpty else {
            word = "CPU 节点"
            tone = .quiet
            return
        }
        // The state that needs a person most, in the one vocabulary agents read too.
        let order = ["CONFLICT", "ORPHANED_BUSY", "BUSY_UNMANAGED", "HELD", "LEASED_IDLE", "RUNNING_MANAGED"]
        let state = order.first { candidate in gpus.contains { $0.state == candidate } }
        let free = gpus.filter(\.isPubliclyAvailable).count
        if state == nil, free > 0 {
            word = "空闲"
            tone = .free
            return
        }
        let label = CardVocabulary.label(forState: state ?? gpus.first?.state ?? "UNKNOWN_STALE")
        word = free > 0 ? "\(label) · \(free) 张空闲" : label
        switch state {
        case "RUNNING_MANAGED": tone = .working
        case "HELD", "LEASED_IDLE": tone = .held
        case nil: tone = .fault
        default: tone = .attention
        }
    }

    var color: Color {
        switch tone {
        case .free: DesignTokens.success
        case .working: DesignTokens.interaction
        case .held: DesignTokens.hold
        case .attention: DesignTokens.warning
        case .fault: DesignTokens.danger
        case .quiet: DesignTokens.mutedInk
        }
    }
}

/// Slurm's node words in the vocabulary the rest of the App uses.
func schedulerNodeStateLabel(_ state: String) -> String {
    let base = state.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "*~#!%$@^-+"))
    switch base {
    case "idle": return "空闲"
    case "mix", "mixed": return "部分占用"
    case "alloc", "allocated": return "已占满"
    case "comp", "completing": return "正在收尾"
    case "drain", "drng", "draining", "drained": return "维护中"
    case "down", "fail", "failing", "maint", "reserved", "resv": return "不可用"
    default: return state
    }
}

/// "3 小时 5 分": how long something has been going, to the minute.
func elapsedLabel(_ seconds: Int) -> String {
    let minutes = seconds / 60
    if minutes < 60 { return "\(max(minutes, 1)) 分钟" }
    let hours = minutes / 60
    if hours >= 48 { return "\(hours / 24) 天" }
    return minutes % 60 == 0 ? "\(hours) 小时" : "\(hours) 小时 \(minutes % 60) 分"
}

/// A quiet line of recent values.  A gap in the data breaks the line.
struct Sparkline: View {
    let points: [Double?]
    var color: Color = DesignTokens.quietBar
    var lineWidth: CGFloat = 1.5

    var body: some View {
        GeometryReader { proxy in
            Path { path in
                guard points.count > 1 else { return }
                let step = proxy.size.width / CGFloat(points.count - 1)
                var drawing = false
                for (index, value) in points.enumerated() {
                    guard let value else {
                        drawing = false
                        continue
                    }
                    let point = CGPoint(
                        x: CGFloat(index) * step,
                        y: proxy.size.height * (1 - CGFloat(min(max(value, 0), 1)))
                    )
                    if drawing {
                        path.addLine(to: point)
                    } else {
                        path.move(to: point)
                        drawing = true
                    }
                }
            }
            .stroke(color, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round, lineJoin: .round))
        }
        .accessibilityHidden(true)
    }
}
