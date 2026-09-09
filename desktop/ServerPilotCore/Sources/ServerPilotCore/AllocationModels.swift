import Foundation

public struct ProviderConnectionRecord: Equatable, Sendable {
    public let supported: Bool
    public let enabled: Bool
    public let state: String
    public let message: String?

    public init(raw: [String: Any]) {
        supported = raw["supported"] as? Bool ?? false
        enabled = raw["enabled"] as? Bool ?? false
        state = raw.string("state") ?? "unmanaged"
        message = raw.string("message")
    }

    public var label: String {
        switch state {
        case "connected": return "已连接"
        case "connecting", "recovering": return "正在恢复连接"
        case "disconnecting": return "正在断开"
        case "network_unavailable": return "等待网络恢复"
        case "action_required": return "需要处理连接"
        case "disconnected": return "未连接"
        default: return "连接未托管"
        }
    }
}

public struct AllocatedDeviceRecord: Equatable, Identifiable, Sendable {
    public let id: String
    public let physicalIndex: Int?
    public let deviceSelector: String?
    public let name: String?
    public let runningProcessCount: Int?
    public let gpuUtilizationPercent: Double?
    public let memoryUsedMiB: Int?
    public let totalVRAMMiB: Int?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("gpu_uuid") else { return nil }
        self.id = id
        physicalIndex = raw.optionalInt("physical_index")
        deviceSelector = raw.string("device_selector")
        name = raw.string("name")
        runningProcessCount = raw.optionalInt("running_process_count")
        gpuUtilizationPercent = (raw["gpu_utilization_pct"] as? NSNumber)?.doubleValue
        memoryUsedMiB = raw.optionalInt("memory_used_mib")
        totalVRAMMiB = raw.optionalInt("total_vram_mib")
    }

    public var locationLabel: String {
        "物理卡 \(physicalIndex.map(String.init) ?? "未知") · 作业内设备 \(deviceSelector ?? "未知")"
    }
}

public struct AllocationLocationRecord: Equatable, Identifiable, Sendable {
    public var id: String { node }
    public let node: String
    public let gpuCount: Int
    public let devices: [AllocatedDeviceRecord]
    public let runningProcessCount: Int?

    public init?(raw: [String: Any]) {
        guard let node = raw.string("node") else { return nil }
        self.node = node
        gpuCount = raw.int("gpu_count")
        devices = (raw["devices"] as? [[String: Any]] ?? []).compactMap(AllocatedDeviceRecord.init)
        runningProcessCount = raw.optionalInt("running_process_count")
    }
}

public struct AllocationEntryRecord: Equatable, Sendable {
    public let kind: String
    public let host: String
    public let port: Int
    public let user: String
    public let workspacePath: String
    public let argv: [String]
    public let instructions: String
    public let verified: Bool

    public init?(raw: [String: Any]) {
        guard let host = raw.string("host"), let user = raw.string("user"), let workspace = raw.string("workspace_path") else { return nil }
        kind = raw.string("kind") ?? "ssh"
        self.host = host
        port = raw.int("port", default: 22)
        self.user = user
        workspacePath = workspace
        argv = raw["argv"] as? [String] ?? []
        instructions = raw.string("instructions") ?? ""
        verified = raw["verified"] as? Bool ?? false
    }

    public var commandText: String {
        argv.map { "'" + $0.replacingOccurrences(of: "'", with: "'\\''") + "'" }.joined(separator: " ")
    }

    public var loginAddress: String { "\(user)@\(host):\(port)" }
}

public struct JobAccountingRecord: Equatable, Sendable {
    public let cpuSeconds: Double?
    public let rssMiB: Double?
    public let observedAt: String?
    public init(raw: [String: Any]) {
        cpuSeconds = raw.optionalDouble("cpu_time_seconds")
        rssMiB = raw.optionalDouble("rss_mib")
        observedAt = raw.string("observed_at")
    }
}

public struct ResourceAllocationRecord: Equatable, Identifiable, Sendable {
    public let id: String
    public let endpointID: String
    public let accounting: JobAccountingRecord?
    public let reference: String?
    public let state: String
    public let releaseGranularity: String
    public let locations: [AllocationLocationRecord]
    public let entry: AllocationEntryRecord?
    public let expiresAt: String?
    public let confirmedAt: String?
    public let fresh: Bool
    public let workloadFresh: Bool
    public let workloadObservedAt: String?
    public let lastError: String?
    public let taskReference: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let endpoint = raw.string("endpoint_id") else { return nil }
        self.id = id
        endpointID = endpoint
        accounting = (raw["accounting"] as? [String: Any]).map(JobAccountingRecord.init)
        reference = raw.string("allocation_ref")
        state = raw.string("state") ?? "unconfirmed"
        releaseGranularity = raw.string("release_granularity") ?? "allocation"
        locations = (raw["locations"] as? [[String: Any]] ?? []).compactMap(AllocationLocationRecord.init)
        entry = (raw["entry"] as? [String: Any]).flatMap(AllocationEntryRecord.init)
        expiresAt = raw.string("expires_at")
        confirmedAt = raw.string("confirmed_at")
        fresh = raw["fresh"] as? Bool ?? false
        workloadFresh = raw["workload_fresh"] as? Bool ?? false
        workloadObservedAt = raw.string("workload_observed_at")
        lastError = raw.string("last_error")
        taskReference = (raw["request"] as? [String: Any])?.string("task_ref")
    }

    public var stateLabel: String {
        switch state {
        case "applying": return "正在申请"
        case "release_pending": return "归还待确认"
        case "released": return "已归还"
        case "expired": return "提供方确认到期"
        case "failed": return "已结束"
        case "active": return fresh ? "已分配" : "位置待重新确认"
        default: return "分配结果待确认"
        }
    }

    public var locationSummary: String {
        locations.isEmpty ? "位置尚未确认" : locations.map { "\($0.node) · \($0.gpuCount) GPU" }.joined(separator: "；")
    }
}

public struct ExternalAllocationRecord: Equatable, Identifiable, Sendable {
    public var id: String { reference }
    public let name: String?
    public let owner: String?
    public let accounting: JobAccountingRecord?
    public let reference: String
    public let locations: [AllocationLocationRecord]
    public let observedAt: String?
    public let state: String

    public init?(raw: [String: Any]) {
        guard let reference = raw.string("allocation_ref") else { return nil }
        name = raw.string("name")
        owner = raw.string("owner")
        accounting = (raw["accounting"] as? [String: Any]).map(JobAccountingRecord.init)
        self.reference = reference
        locations = (raw["locations"] as? [[String: Any]] ?? []).compactMap(AllocationLocationRecord.init)
        observedAt = raw.string("observed_at")
        state = raw.string("state") ?? "unknown"
    }
}


