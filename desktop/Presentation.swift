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


func pressureColor(_ fraction: Double?) -> Color {
    guard let fraction else { return DesignTokens.mutedInk }
    switch fraction {
    case ..<0.70: return DesignTokens.success
    case ..<0.90: return DesignTokens.warning
    default: return DesignTokens.danger
    }
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
    case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED", "MAINTENANCE": return DesignTokens.warning
    default: return DesignTokens.danger
    }
}


private func gpuStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用 · 未开启占卡"
    case "HELD", "LEASED_IDLE", "RUNNING_MANAGED": return "使用中"
    case "KEEPALIVE": return "可用 · 空闲占卡"
    case "BUSY_UNMANAGED", "ORPHANED_BUSY": return "任务占用"
    case "RESERVED": return "不可分配"
    case "UNKNOWN_RECOVERING": return "正在连接"
    case "UNKNOWN_STALE": return "采集延迟"
    case "UNHEALTHY": return "GPU 故障"
    case "CONFLICT": return "归属待确认"
    case "MAINTENANCE": return "不可分配"
    default: return "不可分配"
    }
}


func gpuPresentationLabel(_ gpu: GPURecord) -> String {
    if let publicStatus = gpu.publicStatus {
        return publicStatus
    }
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        return task.map { "任务占用 · \($0)" } ?? "任务占用 · 进程已更新"
    }
    if gpu.keepalive.state == "ERROR" {
        let reason = gpu.keepalive.reason.map(localizedStateReason) ?? "未知原因"
        return gpu.isPubliclyAvailable
            ? "可用 · 占卡异常：\(reason)"
            : "占卡异常：\(reason)"
    }
    if gpu.state == "AVAILABLE" {
        return gpu.keepalive.desired == "ON"
            ? "可用 · 占卡未运行"
            : "可用 · 未开启占卡"
    }
    if gpu.state == "KEEPALIVE" { return "可用 · 空闲占卡" }
    if ["HELD", "LEASED_IDLE", "RUNNING_MANAGED"].contains(gpu.state) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let task, !task.isEmpty else { return "使用中" }
        return task
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "任务占用"
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
        return "此前观测到的任务进程与租约绑定不匹配；请由所属任务确认当前观测，或在任务结束后释放租约"
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


private func formattedTimestamp(_ value: String?) -> String {
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

