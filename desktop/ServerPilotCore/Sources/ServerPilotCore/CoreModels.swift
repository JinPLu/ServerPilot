import Foundation

public struct ServiceInfo: Equatable, Sendable {
    public let schemaVersion: String
    public let version: String?
    public let capabilities: Set<String>

    public init(schemaVersion: String, version: String? = nil, capabilities: Set<String>) {
        self.schemaVersion = schemaVersion
        self.version = version
        self.capabilities = capabilities
    }

    public init?(health: [String: Any]) {
        guard health.string("status") == "live", let schemaVersion = health.string("schema_version") else {
            return nil
        }
        self.schemaVersion = schemaVersion
        self.version = health.string("version")
        self.capabilities = Set((health["capabilities"] as? [String] ?? []).map { $0 })
    }

    public static let fixture = ServiceInfo(
        schemaVersion: "v1",
        version: "fixture",
        capabilities: ["instant_claims", "endpoint_update", "endpoint_delete", "endpoint_keepalive", "endpoint_conflict_cleanup", "operator_lease_release", "operator_lease_reassignment", "collector_settings", "telemetry_recent_averages", "observation_profiles", "server_group_crud"]
    )

    public var supportsEndpointUpdate: Bool {
        supports("endpoint_update")
    }

    public var supportsEndpointDelete: Bool {
        supports("endpoint_delete")
    }

    public var supportsEndpointTelemetryHistory: Bool {
        capabilities.contains("endpoint_telemetry_history") || capabilities.contains("telemetry_history")
    }

    public var supportsEndpointKeepalive: Bool {
        supports("endpoint_keepalive")
    }

    public var supportsEndpointConflictCleanup: Bool {
        supports("endpoint_conflict_cleanup")
    }

    public var supportsOperatorLeaseRelease: Bool {
        supports("operator_lease_release")
    }

    public var supportsOperatorLeaseReassignment: Bool {
        supports("operator_lease_reassignment")
    }

    public var supportsCollectorSettings: Bool {
        supports("collector_settings")
    }

    public var supportsObservationProfiles: Bool {
        supports("observation_profiles")
    }

    public var supportsMcpEntry: Bool {
        supports("mcp_entry")
    }

    public var supportsServerGroupCRUD: Bool {
        // Canonical advertised name is `server_group_crud`. Presence of
        // snapshot `server_groups` is not a capability signal.
        supports("server_group_crud")
    }

    /// Services predating capability advertisement are allowed to make the
    /// request and return a compatibility error.  A service which explicitly
    /// advertises capabilities is authoritative, so the UI can fail closed.
    private func supports(_ capability: String) -> Bool {
        capabilities.isEmpty || capabilities.contains(capability)
    }
}

public struct CollectorSettingsRecord: Equatable, Sendable {
    public let intervalSeconds: Int
    public let staleAfterSeconds: Int
    public let allowedIntervals: [Int]

    public init?(raw: [String: Any]) {
        guard let intervalSeconds = raw.optionalInt("interval_seconds") else { return nil }
        self.intervalSeconds = intervalSeconds
        staleAfterSeconds = raw.int("stale_after_seconds", default: intervalSeconds * 3)
        allowedIntervals = (raw["allowed_intervals"] as? [Int] ?? [5, 10, 30]).sorted()
    }
}

public struct MCPEntryRecord: Equatable, Sendable {
    public let available: Bool
    public let command: String?
    public let configJSON: String?
    public let hint: String?

    public init?(raw: [String: Any]) {
        guard raw["available"] != nil else { return nil }
        let available = raw.bool("available", default: false)
        let command = raw.string("command")
        let hint = raw.string("hint")
        if available {
            guard
                let command, !command.isEmpty,
                let servers = raw["mcpServers"] as? [String: Any],
                JSONSerialization.isValidJSONObject(["mcpServers": servers]),
                // Without .withoutEscapingSlashes the absolute path is rendered
                // as \/opt\/..., which is valid JSON but is what the user
                // copies into their agent's config and reads back.
                let data = try? JSONSerialization.data(
                    withJSONObject: ["mcpServers": servers],
                    options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes]
                ),
                let configJSON = String(data: data, encoding: .utf8)
            else {
                return nil
            }
            self.available = true
            self.command = command
            self.configJSON = configJSON
            self.hint = hint
        } else {
            guard command == nil, raw["mcpServers"] == nil || raw["mcpServers"] is NSNull else {
                return nil
            }
            self.available = false
            self.command = nil
            self.configJSON = nil
            self.hint = hint
        }
    }
}

public struct ResourceSummary: Equatable, Sendable {
    public var onlineServers = 0
    public var totalServers = 0
    public var totalGPUs = 0
    public var availableGPUs = 0
    public var busyGPUs = 0
    public var claimedGPUs = 0
    public var abnormalGPUs = 0
    public var attentionResources = 0

    public init(raw: [String: Any] = [:]) {
        onlineServers = raw.int("online_servers")
        totalServers = raw.int("total_servers")
        totalGPUs = raw.int("total_gpus")
        availableGPUs = raw.int("available_gpus")
        busyGPUs = raw.int("busy_gpus")
        claimedGPUs = raw.int("claimed_gpus")
        abnormalGPUs = raw.int("abnormal_gpus")
        let attention = raw["attention"] as? [String: Any] ?? [:]
        attentionResources = attention.int("total_resource_count", default: abnormalGPUs)
    }

    public init(
        onlineServers: Int = 0,
        totalServers: Int = 0,
        totalGPUs: Int = 0,
        availableGPUs: Int = 0,
        busyGPUs: Int = 0,
        claimedGPUs: Int = 0,
        abnormalGPUs: Int = 0,
        attentionResources: Int = 0
    ) {
        self.onlineServers = onlineServers
        self.totalServers = totalServers
        self.totalGPUs = totalGPUs
        self.availableGPUs = availableGPUs
        self.busyGPUs = busyGPUs
        self.claimedGPUs = claimedGPUs
        self.abnormalGPUs = abnormalGPUs
        self.attentionResources = attentionResources
    }
}

public struct ResourceQuantityRecord: Equatable, Sendable {
    public let cpuCores: Double
    public let memoryMiB: Int
    public let gpuCount: Int
    public let nodeCount: Int

    public init(raw: [String: Any] = [:]) {
        cpuCores = raw.optionalDouble("cpu_cores")
            ?? raw.optionalDouble("min_available_cpu_cores")
            ?? raw.optionalDouble("minimum_cpu_cores")
            ?? 0
        memoryMiB = raw.optionalInt("memory_mib")
            ?? raw.optionalInt("memory_mb")
            ?? raw.optionalInt("min_available_memory_mib")
            ?? raw.optionalInt("minimum_memory_mib")
            ?? 0
        gpuCount = raw.optionalInt("gpu_count")
            ?? raw.optionalInt("gpus")
            ?? 0
        nodeCount = raw.optionalInt("node_count")
            ?? raw.optionalInt("nodes")
            ?? 0
    }

    public init(
        cpuCores: Double = 0,
        memoryMiB: Int = 0,
        gpuCount: Int = 0,
        nodeCount: Int = 0
    ) {
        self.cpuCores = cpuCores
        self.memoryMiB = memoryMiB
        self.gpuCount = gpuCount
        self.nodeCount = nodeCount
    }

    public var compactLabel: String {
        var parts: [String] = []
        if cpuCores > 0 {
            parts.append("\(ResourceText.coreCount(cpuCores)) CPU")
        }
        if memoryMiB > 0 {
            parts.append("\(ResourceText.memory(memoryMiB)) RAM")
        }
        if gpuCount > 0 {
            parts.append("\(gpuCount) GPU")
        }
        if nodeCount > 0 {
            parts.append("\(nodeCount) 节点")
        }
        return parts.isEmpty ? "无资源" : parts.joined(separator: " · ")
    }

    public static func availableHost(endpoint: EndpointRecord) -> ResourceQuantityRecord {
        ResourceQuantityRecord(
            cpuCores: endpoint.availableCPUCores ?? 0,
            memoryMiB: endpoint.memoryAvailableMiB ?? 0
        )
    }
}

/// One way to write a core count and an amount of memory. A cgroup quota is not
/// a whole number of cores, so the fraction survives when it carries meaning.
public enum ResourceText {
    public static func coreCount(_ cores: Double) -> String {
        let rounded = (cores * 10).rounded() / 10
        return rounded == Double(Int(rounded)) ? "\(Int(rounded))" : String(format: "%.1f", rounded)
    }

    public static func cores(_ cores: Double) -> String {
        "\(coreCount(cores)) 核"
    }

    public static func memory(_ mebibytes: Int) -> String {
        let gibibytes = Double(mebibytes) / 1024
        if gibibytes == Double(Int(gibibytes)) {
            return "\(Int(gibibytes)) GB"
        }
        return String(format: "%.1f GB", gibibytes)
    }
}

public struct EndpointKeepaliveSummary: Equatable, Sendable {
    public let configured: Bool
    /// Desired endpoint policy.  Runtime coverage is deliberately reported per GPU.
    public let policy: String
    public let state: String
    public let activeGPUCount: Int
    public let errorGPUCount: Int
    public let eligibleIdleGPUCount: Int
    public let reasons: [String]
    public let message: String?

    public init?(raw: [String: Any], fallbackConfigured: Bool) {
        configured = raw.bool("configured", default: fallbackConfigured)
        let suppliedState = (raw.string("actual") ?? raw.string("state") ?? "OFF").uppercased()
        guard ["OFF", "ON", "ERROR"].contains(suppliedState) else { return nil }
        state = suppliedState
        let suppliedPolicy = (
            raw.string("policy") ?? (state == "OFF" ? "disabled" : "idle_keepalive")
        ).lowercased()
        guard ["disabled", "idle_keepalive"].contains(suppliedPolicy) else { return nil }
        policy = suppliedPolicy
        activeGPUCount = max(0, raw.int("active_gpu_count"))
        errorGPUCount = max(0, raw.int("error_gpu_count"))
        eligibleIdleGPUCount = max(0, raw.int("eligible_idle_gpu_count"))
        reasons = (raw["reasons"] as? [String] ?? []).filter { !$0.isEmpty }
            + (raw["reasons"] as? [[String: Any]] ?? []).compactMap { $0.string("reason") }
        message = raw.string("message") ?? reasons.first
    }

    public var label: String {
        guard configured else { return "未配置" }
        if policy == "disabled" { return "已关闭" }
        if errorGPUCount > 0 || state == "ERROR" { return "异常" }
        return "已开启"
    }

    public var isEnabled: Bool { policy == "idle_keepalive" }
    public var isActive: Bool { activeGPUCount > 0 || state == "ON" }
    public var isTransitioning: Bool { false }
    public var hasResidualLease: Bool {
        activeGPUCount > 0 || errorGPUCount > 0
    }

    public func coverageSummary(totalGPUCount: Int, taskGPUCount: Int) -> String {
        guard totalGPUCount > 0 else { return "无 GPU" }
        guard configured else { return "未配置空闲占卡" }
        guard isEnabled else {
            return "已关闭"
        }
        var details: [String] = ["\(min(activeGPUCount, max(totalGPUCount, 0)))/\(totalGPUCount) 占卡"]
        if taskGPUCount > 0 { details.append("\(taskGPUCount) 卡任务中") }
        if errorGPUCount > 0 { details.append("\(errorGPUCount) 卡异常") }
        if activeGPUCount == 0, taskGPUCount == 0, eligibleIdleGPUCount == 0 {
            details = ["等待空闲 GPU"]
        }
        return "已开启 · " + details.joined(separator: "，")
    }
}

public struct GPUKeepaliveStatus: Equatable, Sendable {
    public let configured: Bool
    public let policy: String
    public let desired: String
    public let state: String
    public let leaseID: String?
    public let reason: String?

    public init?(raw: [String: Any], fallbackConfigured: Bool, fallbackState: String) {
        configured = raw.bool("configured", default: fallbackConfigured)
        let suppliedPolicy = (raw.string("policy") ?? "disabled").lowercased()
        guard ["disabled", "idle_keepalive"].contains(suppliedPolicy) else { return nil }
        policy = suppliedPolicy
        let suppliedDesired = (
            raw.string("desired") ?? (suppliedPolicy == "idle_keepalive" ? "ON" : "OFF")
        ).uppercased()
        guard ["OFF", "ON"].contains(suppliedDesired) else { return nil }
        desired = suppliedDesired
        let suppliedState = (raw.string("actual") ?? raw.string("state") ?? fallbackState).uppercased()
        guard ["OFF", "ON", "ERROR"].contains(suppliedState) else { return nil }
        state = suppliedState
        leaseID = raw.string("lease_id")
        reason = raw.string("reason") ?? raw.string("message")
    }

    public var isActive: Bool { state == "ON" }

    public var presentationLabel: String {
        switch state {
        case "ON": return "空闲占卡"
        case "ERROR": return "占卡异常"
        default: return desired == "ON" ? "占卡未运行" : "未占卡"
        }
    }

}

public struct HostTelemetryRecentAverage: Equatable, Sendable {
    public let windowSeconds: Int
    public let sampleCount: Int
    public let firstObservedAt: String?
    public let lastObservedAt: String?
    public let cpuLoadFraction: Double?
    public let memoryFraction: Double?

    public init?(raw: [String: Any]) {
        let windowSeconds = raw.int("window_seconds")
        let sampleCount = raw.int("sample_count")
        guard windowSeconds > 0, sampleCount > 0 else { return nil }
        self.windowSeconds = windowSeconds
        self.sampleCount = sampleCount
        self.firstObservedAt = raw.string("first_observed_at")
        self.lastObservedAt = raw.string("last_observed_at")
        self.cpuLoadFraction = raw.optionalFraction("cpu_load_fraction")
        self.memoryFraction = raw.optionalPercent("memory_used_pct")
    }
}

public enum ServerGroupAllocation: String, Equatable, Sendable {
    case direct
    case delegated
}

public enum ServerGroupLeaseEnds: String, Equatable, Sendable {
    case onRelease = "on_release"
    case hardKillAtTimeLimit = "hard_kill_at_time_limit"
}

public struct ServerGroupLimits: Equatable, Sendable {
    public let maxGPUsPerLease: Int?
    public let maxLeaseSeconds: Int?
    public let leaseEnds: ServerGroupLeaseEnds?
    public let cpuCoresPerGPU: Int?
    public let memoryMiBPerGPU: Int?
    public let applyMaxSeconds: Int?
    public let queues: Bool?

    public init?(raw: [String: Any]) {
        if raw["lease_ends"] == nil || raw["lease_ends"] is NSNull {
            leaseEnds = nil
        } else if let value = raw.string("lease_ends"),
                  let parsed = ServerGroupLeaseEnds(rawValue: value) {
            leaseEnds = parsed
        } else {
            return nil
        }
        maxGPUsPerLease = raw.optionalInt("max_gpus_per_lease")
        maxLeaseSeconds = raw.optionalInt("max_lease_seconds")
        cpuCoresPerGPU = raw.optionalInt("cpu_cores_per_gpu")
        memoryMiBPerGPU = raw.optionalInt("memory_mib_per_gpu")
        applyMaxSeconds = raw.optionalInt("apply_max_seconds")
        queues = raw.optionalBool("queues")
    }

    public init(
        maxGPUsPerLease: Int? = nil,
        maxLeaseSeconds: Int? = nil,
        leaseEnds: ServerGroupLeaseEnds? = nil,
        cpuCoresPerGPU: Int? = nil,
        memoryMiBPerGPU: Int? = nil,
        applyMaxSeconds: Int? = nil,
        queues: Bool? = nil
    ) {
        self.maxGPUsPerLease = maxGPUsPerLease
        self.maxLeaseSeconds = maxLeaseSeconds
        self.leaseEnds = leaseEnds
        self.cpuCoresPerGPU = cpuCoresPerGPU
        self.memoryMiBPerGPU = memoryMiBPerGPU
        self.applyMaxSeconds = applyMaxSeconds
        self.queues = queues
    }
}

public struct SchedulerCapacity: Equatable, Sendable {
    public let freeGPUCount: Int
    public let gpuName: String
    public let largestFreeBlock: Int?
    public let vramMiB: Int?
    public let maxGPUsPerLease: Int?
    public let cpuCoresPerGPU: Int?
    public let memoryMiBPerGPU: Int?
    public let note: String?

    public init?(raw: [String: Any]) {
        guard
            let freeGPUCount = raw.optionalInt("free_gpu_count"),
            let gpuName = raw.string("gpu_name")?.trimmingCharacters(in: .whitespacesAndNewlines),
            !gpuName.isEmpty
        else {
            return nil
        }
        self.freeGPUCount = freeGPUCount
        self.gpuName = gpuName
        largestFreeBlock = raw.optionalInt("largest_free_block")
        vramMiB = raw.optionalInt("vram_mib")
        maxGPUsPerLease = raw.optionalInt("max_gpus_per_lease")
        cpuCoresPerGPU = raw.optionalInt("cpu_cores_per_gpu")
        memoryMiBPerGPU = raw.optionalInt("memory_mib_per_gpu")
        let note = raw.string("note")?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.note = (note?.isEmpty == false) ? note : nil
    }
}

public struct ServerGroupRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let displayName: String
    public let workspacePath: String
    public let environmentNotes: String
    public let description: String
    public let allocation: ServerGroupAllocation?
    public let limits: ServerGroupLimits?
    public let largestAllocatableBlock: Int?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), !id.isEmpty else { return nil }
        if raw["environment_notes"] is [String: Any] || raw["environment"] is [String: Any] {
            return nil
        }
        guard
            raw["command"] == nil,
            raw["keepalive_adapter_id"] == nil,
            raw["observation_profile"] == nil
        else {
            return nil
        }
        let workspacePath = raw.string("workspace_path") ?? ""
        guard CoreFieldValidation.isAbsoluteWorkspacePath(workspacePath) else { return nil }
        if raw["allocation"] == nil || raw["allocation"] is NSNull {
            allocation = nil
        } else if let value = raw.string("allocation"),
                  let parsed = ServerGroupAllocation(rawValue: value) {
            allocation = parsed
        } else {
            return nil
        }
        if raw["limits"] == nil || raw["limits"] is NSNull {
            limits = nil
        } else if let payload = raw["limits"] as? [String: Any] {
            guard let parsed = ServerGroupLimits(raw: payload) else { return nil }
            limits = parsed
        } else {
            return nil
        }
        if raw["largest_allocatable_block"] == nil || raw["largest_allocatable_block"] is NSNull {
            largestAllocatableBlock = nil
        } else if let value = raw.optionalInt("largest_allocatable_block") {
            largestAllocatableBlock = value
        } else {
            return nil
        }
        self.id = id
        self.displayName = raw.string("display_name") ?? id
        self.workspacePath = workspacePath
        self.environmentNotes = raw.string("environment_notes") ?? ""
        self.description = raw.string("description") ?? ""
    }

    public init(
        id: String,
        displayName: String,
        workspacePath: String,
        environmentNotes: String = "",
        description: String = "",
        allocation: ServerGroupAllocation? = nil,
        limits: ServerGroupLimits? = nil,
        largestAllocatableBlock: Int? = nil
    ) {
        self.id = id
        self.displayName = displayName
        self.workspacePath = workspacePath
        self.environmentNotes = environmentNotes
        self.description = description
        self.allocation = allocation
        self.limits = limits
        self.largestAllocatableBlock = largestAllocatableBlock
    }
}

public struct EndpointRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let host: String
    public let port: Int
    public let sshUser: String
    public let sshAlias: String?
    public let serverGroupID: String?
    public let workspacePath: String?
    public let workspacePathOverride: String?
    public let observationProfile: String
    public let keepaliveAdapterID: String?
    public let keepalive: EndpointKeepaliveSummary
    public let enabled: Bool
    public let lifecycleState: String?
    public let monitorStatus: String
    public let monitorError: String?
    public let monitorLastSuccessAt: String?
    public let monitorLastAttemptAt: String?
    public let cpuUtilizationFraction: Double?
    /// Effective CPU and memory for this endpoint, resolved by the broker: a
    /// container's cgroup budget where one is in force, the machine's own
    /// capacity otherwise.  The raw host readings are deliberately not carried
    /// here — a node's core count and MemTotal describe the machine, not the
    /// share this endpoint may use.
    public let cpuScope: String?
    public let cpuCores: Double?
    public let cpuAvailableCores: Double?
    public let memoryScope: String?
    public let memoryTotalMiB: Int?
    public let memoryUsedMiB: Int?
    public let memoryAvailableMiB: Int?
    public let recentTelemetryAverage: HostTelemetryRecentAverage?
    public let schedulerCapacity: SchedulerCapacity?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let host = raw.string("host"), let sshUser = raw.string("ssh_user") else {
            return nil
        }
        self.id = id
        self.host = host
        self.port = raw.int("port", default: 22)
        self.sshUser = sshUser
        self.sshAlias = raw.string("ssh_alias")
        let groupID = raw.string("server_group_id")?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.serverGroupID = (groupID?.isEmpty == false) ? groupID : nil
        self.workspacePath = raw.string("workspace_path")
        let pathOverride = raw.string("workspace_path_override")?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.workspacePathOverride = (pathOverride?.isEmpty == false) ? pathOverride : nil
        self.observationProfile = raw.string("observation_profile") ?? "linux-nvidia"
        self.keepaliveAdapterID = raw.string("keepalive_adapter_id")
        guard let keepalive = EndpointKeepaliveSummary(
            raw: raw["keepalive"] as? [String: Any] ?? [:],
            fallbackConfigured: keepaliveAdapterID != nil
        ) else { return nil }
        self.keepalive = keepalive
        self.enabled = raw.bool("enabled", default: true)
        self.lifecycleState = raw.string("lifecycle_state")?.uppercased()
        let monitor = raw["monitor"] as? [String: Any] ?? [:]
        self.monitorStatus = (monitor.string("status") ?? "PENDING").uppercased()
        self.monitorError = monitor.string("last_error")
        self.monitorLastSuccessAt = monitor.string("last_success_at")
        self.monitorLastAttemptAt = monitor.string("last_attempt_at")
        let hostTelemetry = raw["host_telemetry"] as? [String: Any] ?? [:]
        self.cpuUtilizationFraction = hostTelemetry.optionalPercent("cpu_utilization_pct")
        let capacity = hostTelemetry["capacity"] as? [String: Any] ?? [:]
        self.cpuScope = capacity.string("cpu_scope")
        self.cpuCores = capacity.optionalDouble("cpu_cores")
        self.cpuAvailableCores = capacity.optionalDouble("cpu_available_cores")
        self.memoryScope = capacity.string("memory_scope")
        self.memoryTotalMiB = capacity.optionalInt("memory_total_mib")
        self.memoryUsedMiB = capacity.optionalInt("memory_used_mib")
        self.memoryAvailableMiB = capacity.optionalInt("memory_available_mib")
        self.recentTelemetryAverage = HostTelemetryRecentAverage(
            raw: hostTelemetry["recent_average"] as? [String: Any] ?? [:]
        )
        if let payload = raw["scheduler_capacity"] as? [String: Any] {
            self.schedulerCapacity = SchedulerCapacity(raw: payload)
        } else {
            self.schedulerCapacity = nil
        }
    }

    public var sshCommand: String {
        let target = sshAlias ?? "\(sshUser)@\(host)"
        return "ssh -p \(port) \(target)"
    }

    public var displayName: String {
        sshAlias ?? "\(sshUser)@\(host):\(port)"
    }

    /// Effective `workspacePath` comes from the broker. An override is present
    /// only when this endpoint does not inherit its group's default path.
    public var inheritsGroupWorkspacePath: Bool {
        serverGroupID != nil && workspacePathOverride == nil
    }

    public var monitorLabel: String {
        switch monitorStatus {
        case "ONLINE": return "在线"
        case "PENDING": return "正在连接"
        case "STALE": return "采集延迟"
        case "ERROR": return "连接失败"
        case "DISABLED": return "已停用"
        case "DRAINING": return "已暂停"
        default: return monitorStatus
        }
    }

    /// Being stopped by a person and being unreachable are different answers to
    /// different questions, so the human-set states are answered first.  Only
    /// after them does `monitorError` -- which describes the most recent probe
    /// attempt, never the host itself -- get read as a reason for silence.
    public var monitorDetail: String? {
        if monitorStatus == "DISABLED" {
            return "这台服务器已停用，不接收新任务；不会停止远端任务。"
        }
        if lifecycleState == "DRAINING" || monitorStatus == "DRAINING" {
            return "这台服务器已暂停接收新任务，正在排空；不会停止远端任务。"
        }
        if monitorStatus == "PENDING" {
            return "正在进行首次连接"
        }
        if monitorStatus == "ERROR" || monitorStatus == "STALE" {
            if let monitorError, !monitorError.isEmpty {
                let lowered = monitorError.lowercased()
                if lowered.contains("timed out") || lowered.contains("timeout") {
                    return "连接或更新超时 · 检查服务器和 SSH"
                }
                if lowered.contains("connection refused") {
                    return "连接被拒绝 · 检查 SSH 服务和端口"
                }
                if lowered.contains("permission denied") || lowered.contains("authentication") {
                    return "SSH 验证失败 · 检查账号和密钥"
                }
                if lowered.contains("no route to host") || lowered.contains("network is unreachable") {
                    return "网络不可达"
                }
                return "无法连接服务器 · 检查 SSH"
            }
            return monitorStatus == "STALE" ? "最近一次服务器数据已过期" : "连接或更新失败"
        }
        if let monitorLastSuccessAt, !monitorLastSuccessAt.isEmpty {
            return "上次连接成功：\(monitorLastSuccessAt)"
        }
        if let monitorLastAttemptAt, !monitorLastAttemptAt.isEmpty {
            return "上次尝试连接：\(monitorLastAttemptAt)"
        }
        return nil
    }

    public var cpuLoadFraction: Double? {
        guard monitorStatus == "ONLINE" else { return nil }
        return cpuUtilizationFraction
    }

    public var availableCPUCores: Double? {
        guard monitorStatus == "ONLINE" else { return nil }
        return cpuAvailableCores
    }

    public var memoryFraction: Double? {
        guard
            monitorStatus == "ONLINE",
            let memoryTotalMiB,
            memoryTotalMiB > 0,
            let memoryUsedMiB
        else { return nil }
        return min(max(Double(memoryUsedMiB) / Double(memoryTotalMiB), 0), 1)
    }

    /// Says out loud that a container budget, not the machine, is what these
    /// numbers describe — a node's 128 cores are not this endpoint's to use.
    public var cpuScopeNote: String? { cpuScope == "container" ? "容器配额" : nil }

    public var memoryScopeNote: String? { memoryScope == "container" ? "容器配额" : nil }
}

public struct GPURecentTelemetryAverage: Equatable, Sendable {
    public let windowSeconds: Int
    public let sampleCount: Int
    public let firstObservedAt: String?
    public let lastObservedAt: String?
    public let memoryFraction: Double?
    public let utilizationFraction: Double?

    public init?(raw: [String: Any]) {
        let windowSeconds = raw.int("window_seconds")
        let sampleCount = raw.int("sample_count")
        guard windowSeconds > 0, sampleCount > 0 else { return nil }
        self.windowSeconds = windowSeconds
        self.sampleCount = sampleCount
        self.firstObservedAt = raw.string("first_observed_at")
        self.lastObservedAt = raw.string("last_observed_at")
        self.memoryFraction = raw.optionalPercent("memory_used_pct")
        self.utilizationFraction = raw.optionalPercent("gpu_utilization_pct")
    }
}

public struct GPURecord: Identifiable, Equatable, Sendable {
    /// The states the broker projects as allocatable capacity.
    static let allocatableStates: Set<String> = ["AVAILABLE", "KEEPALIVE"]
    /// The prefix a projected status carries when it claims the card is free.
    static let availableStatusPrefix = "可用"

    public let id: String
    public let endpointID: String
    public let gpuUUID: String?
    public let index: Int
    public let name: String
    public let totalVRAMMiB: Int
    public let state: String
    public let stateReason: String?
    public let memoryUsedMiB: Int?
    public let utilization: Int?
    public let temperature: Int?
    public let recentTelemetryAverage: GPURecentTelemetryAverage?
    public let owner: String?
    public let taskReference: String?
    public let leaseID: String?
    public let keepalive: GPUKeepaliveStatus
    /// Canonical short Chinese projection supplied by the broker.
    public let publicStatus: String?
    /// Broker-owned availability takes precedence over a local state inference.
    public let projectedPubliclyAvailable: Bool?

    public init?(raw: [String: Any]) {
        guard
            let endpointID = raw.string("endpoint_id"),
            let name = raw.string("name")
        else {
            return nil
        }
        let uuid = raw.string("gpu_uuid")
        if let suppliedID = raw.string("id"), !suppliedID.isEmpty {
            self.id = suppliedID
        } else if let uuid, !uuid.isEmpty {
            self.id = "\(endpointID):\(uuid)"
        } else {
            return nil
        }
        self.endpointID = endpointID
        self.gpuUUID = uuid
        self.index = raw.int("gpu_index")
        self.name = name
        self.totalVRAMMiB = raw.int("total_vram_mib")
        self.state = raw.string("state") ?? "UNKNOWN_RECOVERING"
        self.stateReason = raw.string("state_reason")
        let telemetry = raw["telemetry"] as? [String: Any] ?? [:]
        self.memoryUsedMiB = telemetry.optionalInt("memory_used_mib")
        self.utilization = telemetry.optionalInt("gpu_utilization_pct")
        self.temperature = telemetry.optionalInt("temperature_c")
        self.recentTelemetryAverage = GPURecentTelemetryAverage(
            raw: telemetry["recent_average"] as? [String: Any] ?? [:]
        )
        let lease = raw["lease"] as? [String: Any] ?? [:]
        self.leaseID = lease.string("id")
        self.owner = lease.string("actor_id")
        self.taskReference = lease.string("task_ref")
        let suppliedPublicStatus = raw.string("public_status")?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        self.publicStatus = suppliedPublicStatus?.isEmpty == false ? suppliedPublicStatus : nil
        self.projectedPubliclyAvailable = raw["publicly_available"] == nil
            ? nil
            : raw.bool("publicly_available", default: false)
        // Reject a row that contradicts itself. Both halves validate the same
        // untrusted payload: the state code catches an allocatable state marked
        // unavailable, and the status text catches a payload still claiming the
        // card is free. Keying on the state alone let the second case through.
        if self.projectedPubliclyAvailable == false,
           GPURecord.allocatableStates.contains(self.state)
               || self.publicStatus?.hasPrefix(GPURecord.availableStatusPrefix) == true {
            return nil
        }
        guard let keepalive = GPUKeepaliveStatus(
            raw: raw["keepalive"] as? [String: Any] ?? [:],
            fallbackConfigured: false,
            fallbackState: self.state == "KEEPALIVE" ? "ON" : "OFF"
        ) else { return nil }
        self.keepalive = keepalive
    }

    public var memoryFraction: Double {
        guard let memoryUsedMiB, totalVRAMMiB > 0 else { return 0 }
        return min(max(Double(memoryUsedMiB) / Double(totalVRAMMiB), 0), 1)
    }

    public var isTaskOccupancy: Bool {
        if keepalive.leaseID != nil { return false }
        switch state {
        case "HELD":
            return true
        case "LEASED_IDLE", "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY":
            return true
        default:
            return false
        }
    }

    public var isPubliclyAvailable: Bool {
        projectedPubliclyAvailable
            ?? (state == "AVAILABLE" || state == "KEEPALIVE")
    }

    public var memoryLabel: String {
        guard let memoryUsedMiB else { return "—" }
        return "\(memoryUsedMiB / 1024) / \(max(totalVRAMMiB / 1024, 1)) GB"
    }

    public var vramLabel: String {
        "\(max(totalVRAMMiB / 1024, 1)) GB"
    }

    public var uuidLabel: String {
        guard let gpuUUID, !gpuUUID.isEmpty else { return String(id.suffix(12)) }
        return String(gpuUUID.suffix(12))
    }
}

public struct BrokerStateHistory: Equatable, Sendable {
    public static let empty = BrokerStateHistory()

    public init(raw: [String: Any] = [:]) {
        _ = raw
    }
}

public enum EndpointTelemetryRange: String, CaseIterable, Identifiable, Sendable {
    case oneHour = "1h"
    case sixHours = "6h"
    case twentyFourHours = "24h"

    public var id: String { rawValue }

    public var windowSeconds: Int {
        switch self {
        case .oneHour:
            3_600
        case .sixHours:
            21_600
        case .twentyFourHours:
            86_400
        }
    }
}

public struct EndpointTelemetrySample: Identifiable, Equatable, Sendable {
    public var id: String { timestamp }
    public let timestamp: String
    public let cpuLoadFraction: Double?
    public let memoryFraction: Double?
    public let gpuUtilizationFraction: Double?
    public let gpuMemoryFraction: Double?
    public let status: String?

    public init?(raw: [String: Any]) {
        guard let timestamp = raw.string("timestamp") ?? raw.string("observed_at") ?? raw.string("server_time") else {
            return nil
        }
        self.timestamp = timestamp
        cpuLoadFraction = raw.optionalFraction("cpu_load_fraction")
            ?? raw.optionalFraction("cpu_fraction")
            ?? raw.optionalFraction("load_fraction")
            ?? raw.optionalPercent("cpu_utilization_pct")
        memoryFraction = raw.optionalFraction("memory_fraction")
            ?? raw.optionalFraction("memory_used_fraction")
            ?? raw.optionalPercent("memory_used_pct")
        gpuUtilizationFraction = raw.optionalFraction("gpu_utilization_fraction")
            ?? raw.optionalPercent("gpu_utilization_pct")
        gpuMemoryFraction = raw.optionalFraction("gpu_memory_fraction")
            ?? raw.optionalFraction("vram_fraction")
            ?? raw.optionalFraction("gpu_memory_used_fraction")
        status = raw.string("status")?.uppercased()
    }
}

public struct EndpointGPUHistorySample: Identifiable, Equatable, Sendable {
    public var id: String { timestamp }
    public let timestamp: String
    public let gpuUtilizationFraction: Double?
    public let memoryFraction: Double?
    public let memoryUsedMiB: Double?
    public let memoryTotalMiB: Double?

    public init?(raw: [String: Any]) {
        guard let timestamp = raw.string("timestamp") ?? raw.string("observed_at") else {
            return nil
        }
        self.timestamp = timestamp
        gpuUtilizationFraction = raw.optionalFraction("gpu_utilization_fraction")
            ?? raw.optionalPercent("gpu_utilization_pct")
        memoryFraction = raw.optionalFraction("memory_fraction")
            ?? raw.optionalPercent("memory_used_pct")
        memoryUsedMiB = raw.optionalDouble("memory_used_mib")
        memoryTotalMiB = raw.optionalDouble("memory_total_mib")
    }
}

public struct EndpointGPUHistorySeries: Identifiable, Equatable, Sendable {
    public let id: String
    public let gpuUUID: String
    public let index: Int
    public let label: String
    public let samples: [EndpointGPUHistorySample]

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("gpu_id"),
            let gpuUUID = raw.string("gpu_uuid"),
            let index = raw.optionalInt("gpu_index")
        else {
            return nil
        }
        self.id = id
        self.gpuUUID = gpuUUID
        self.index = index
        label = raw.string("label") ?? "GPU \(index)"
        let payload = raw["points"] as? [[String: Any]]
            ?? raw["samples"] as? [[String: Any]]
            ?? []
        samples = payload.compactMap(EndpointGPUHistorySample.init)
    }
}

public struct EndpointTelemetryHistory: Equatable, Sendable {
    public let endpointID: String
    public let range: EndpointTelemetryRange
    public let samples: [EndpointTelemetrySample]
    public let gpuSeries: [EndpointGPUHistorySeries]
    public let generatedAt: String?

    public static func empty(endpointID: String, range: EndpointTelemetryRange) -> EndpointTelemetryHistory {
        EndpointTelemetryHistory(endpointID: endpointID, range: range, samples: [], gpuSeries: [], generatedAt: nil)
    }

    public init(
        endpointID: String,
        range: EndpointTelemetryRange,
        samples: [EndpointTelemetrySample],
        gpuSeries: [EndpointGPUHistorySeries] = [],
        generatedAt: String?
    ) {
        self.endpointID = endpointID
        self.range = range
        self.samples = samples
        self.gpuSeries = gpuSeries
        self.generatedAt = generatedAt
    }

    public init(endpointID: String, range: EndpointTelemetryRange, envelope: [String: Any]) {
        let data = envelope["data"] as? [String: Any] ?? envelope
        let samplePayload = data["points"] as? [[String: Any]]
            ?? data["samples"] as? [[String: Any]]
            ?? data["history"] as? [[String: Any]]
            ?? envelope["samples"] as? [[String: Any]]
            ?? []
        self.endpointID = data.string("endpoint_id") ?? endpointID
        self.range = EndpointTelemetryRange.allCases.first {
            $0.windowSeconds == data.optionalInt("window_seconds")
        } ?? EndpointTelemetryRange(rawValue: data.string("range") ?? range.rawValue) ?? range
        self.samples = samplePayload.compactMap(EndpointTelemetrySample.init)
        self.gpuSeries = (data["gpu_series"] as? [[String: Any]] ?? []).compactMap(EndpointGPUHistorySeries.init)
        self.generatedAt = envelope.string("server_time") ?? data.string("generated_at") ?? data.string("server_time")
    }
}

public struct BrokerSnapshot: Equatable, Sendable {
    public var schemaVersion: String?
    public var snapshotRevision: Int?
    public var serverTime: String?
    public var summary: ResourceSummary
    public var endpoints: [EndpointRecord]
    public var serverGroups: [ServerGroupRecord]
    public var gpus: [GPURecord]
    public var leases: [LeaseRecord]
    public var requests: [AllocationRequestRecord]
    public var reservations: [ReservationRecord]
    public var history: BrokerStateHistory
    public var dataAgeSeconds: Double?
    public var freshnessSeconds: Double?
    public var admissionBoundary: String

    public static let empty = BrokerSnapshot(
        schemaVersion: nil,
        snapshotRevision: nil,
        serverTime: nil,
        summary: ResourceSummary(),
        endpoints: [],
        serverGroups: [],
        gpus: [],
        leases: [],
        requests: [],
        reservations: [],
        history: .empty,
        dataAgeSeconds: nil,
        freshnessSeconds: nil,
        admissionBoundary: "ServerPilot 只协调资源，不执行服务器上的任务。"
    )

    public var operationalEndpoints: [EndpointRecord] {
        endpoints
    }

    public var operationalGPUs: [GPURecord] {
        let endpointIDs = Set(operationalEndpoints.map(\.id))
        return gpus.filter { endpointIDs.contains($0.endpointID) }
    }

    public init(envelope: [String: Any]) {
        let stateData = envelope["data"] as? [String: Any]
        let payload = stateData?["current"] as? [String: Any]
            ?? stateData
            ?? envelope
        self.init(
            payload: payload,
            schemaVersion: envelope.string("schema_version"),
            snapshotRevision: envelope.optionalInt("snapshot_revision"),
            serverTime: envelope.string("server_time"),
            history: BrokerStateHistory(raw: stateData?["history"] as? [String: Any] ?? [:])
        )
    }

    public init(
        payload: [String: Any],
        schemaVersion: String? = nil,
        snapshotRevision: Int? = nil,
        serverTime: String? = nil,
        history: BrokerStateHistory = .empty
    ) {
        self.schemaVersion = schemaVersion
        self.snapshotRevision = snapshotRevision
        self.serverTime = serverTime
        self.history = history
        summary = ResourceSummary(raw: payload["summary"] as? [String: Any] ?? [:])
        endpoints = (payload["endpoints"] as? [[String: Any]] ?? []).compactMap(EndpointRecord.init)
        serverGroups = (payload["server_groups"] as? [[String: Any]] ?? []).compactMap(ServerGroupRecord.init)
        gpus = (payload["gpus"] as? [[String: Any]] ?? []).compactMap(GPURecord.init)
        let operationalEndpointIDs = Set(endpoints.map(\.id))
        let endpointAttention = endpoints.filter {
            ["ERROR", "STALE", "DRAINING"].contains($0.monitorStatus)
        }.count
        let gpuAttentionStates = Set(["BUSY_UNMANAGED", "UNKNOWN_RECOVERING", "UNKNOWN_STALE", "UNHEALTHY", "CONFLICT", "ORPHANED_BUSY", "DRAINING"])
        let gpuAttention = gpus.filter {
            operationalEndpointIDs.contains($0.endpointID) && gpuAttentionStates.contains($0.state)
        }.count
        summary.attentionResources = max(summary.attentionResources, endpointAttention + gpuAttention)
        leases = (payload["leases"] as? [[String: Any]] ?? []).compactMap(LeaseRecord.init)
        requests = (payload["requests"] as? [[String: Any]] ?? []).compactMap(AllocationRequestRecord.init)
        reservations = (payload["reservations"] as? [[String: Any]] ?? []).compactMap(ReservationRecord.init)
        dataAgeSeconds = payload.optionalDouble("data_age_seconds")
        freshnessSeconds = payload.optionalDouble("freshness_seconds")
        admissionBoundary = payload.string("admission_boundary") ?? BrokerSnapshot.empty.admissionBoundary
    }

    public init(
        schemaVersion: String? = nil,
        snapshotRevision: Int? = nil,
        serverTime: String? = nil,
        summary: ResourceSummary,
        endpoints: [EndpointRecord],
        serverGroups: [ServerGroupRecord] = [],
        gpus: [GPURecord],
        leases: [LeaseRecord],
        requests: [AllocationRequestRecord],
        reservations: [ReservationRecord] = [],
        history: BrokerStateHistory = .empty,
        dataAgeSeconds: Double?,
        freshnessSeconds: Double? = nil,
        admissionBoundary: String
    ) {
        self.schemaVersion = schemaVersion
        self.snapshotRevision = snapshotRevision
        self.serverTime = serverTime
        self.summary = summary
        self.endpoints = endpoints
        self.serverGroups = serverGroups
        self.gpus = gpus
        self.leases = leases
        self.requests = requests
        self.reservations = reservations
        self.history = history
        self.dataAgeSeconds = dataAgeSeconds
        self.freshnessSeconds = freshnessSeconds
        self.admissionBoundary = admissionBoundary
    }

    /// Compares the data that drives the desktop UI while intentionally ignoring
    /// the response timestamp. The service emits a new `serverTime` for every
    /// state read even when the revision and resource data have not changed.
    public func isSemanticallyEquivalentForRefresh(to other: BrokerSnapshot) -> Bool {
        schemaVersion == other.schemaVersion
            && snapshotRevision == other.snapshotRevision
            && summary == other.summary
            && endpoints == other.endpoints
            && serverGroups == other.serverGroups
            && gpus == other.gpus
            && leases == other.leases
            && requests == other.requests
            && reservations == other.reservations
            && history == other.history
            && dataAgeSeconds == other.dataAgeSeconds
            && freshnessSeconds == other.freshnessSeconds
            && admissionBoundary == other.admissionBoundary
    }

    public func gpus(for endpoint: EndpointRecord) -> [GPURecord] {
        gpus.filter { $0.endpointID == endpoint.id }
    }

    public func endpoint(id: String) -> EndpointRecord? {
        endpoints.first { $0.id == id }
    }

    public func serverGroup(id: String) -> ServerGroupRecord? {
        serverGroups.first { $0.id == id }
    }

    public func serverGroup(for endpoint: EndpointRecord) -> ServerGroupRecord? {
        guard let groupID = endpoint.serverGroupID else { return nil }
        return serverGroup(id: groupID)
    }

    public func endpoints(inGroup groupID: String) -> [EndpointRecord] {
        endpoints.filter { $0.serverGroupID == groupID }
    }

    public var ungroupedEndpoints: [EndpointRecord] {
        endpoints.filter { $0.serverGroupID == nil }
    }

    public func gpu(id: String) -> GPURecord? {
        gpus.first { $0.id == id }
    }

    public func stableEndpointSelection(currentID: String) -> String {
        endpoints.contains { $0.id == currentID } ? currentID : (endpoints.first?.id ?? "")
    }

    public func stableGPUSelection(currentID: String) -> String {
        gpus.contains { $0.id == currentID } ? currentID : (gpus.first?.id ?? "")
    }

    public func stableLeaseSelection(currentID: String) -> String {
        leases.contains { $0.id == currentID } ? currentID : (leases.first?.id ?? "")
    }

    public func stableRequestSelection(currentID: String) -> String {
        requests.contains { $0.id == currentID } ? currentID : (requests.first?.id ?? "")
    }
}

public struct ReservationRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let projectID: String?
    public let actorID: String?
    public let gpuIDs: [String]
    public let startsAt: String?
    public let endsAt: String?
    public let state: String
    public let purpose: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id") else { return nil }
        self.id = id
        self.projectID = raw.string("project_id")
        self.actorID = raw.string("actor_id")
        self.gpuIDs = raw["gpu_ids"] as? [String] ?? []
        self.startsAt = raw.string("starts_at") ?? raw.string("start_time")
        self.endsAt = raw.string("ends_at") ?? raw.string("end_time")
        self.state = raw.string("state") ?? "ACTIVE"
        self.purpose = raw.string("purpose")
    }

    public var stateLabel: String {
        switch state {
        case "ACTIVE": return "生效中"
        case "PENDING": return "等待生效"
        case "EXPIRED": return "已过期"
        case "CANCELLED": return "已取消"
        default: return state
        }
    }
}

public struct LeaseRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let requestID: String?
    public let actorID: String
    public let projectID: String
    public let kind: String
    public let state: String
    public let runtimeState: String
    public let gpuIDs: [String]
    public let issuedAt: String?
    public let expiresAt: String?
    /// When a compute process was last seen on this lease's own cards. Read-only:
    /// it never gates a release, it only lets a person tell a job between two
    /// batches from one that has ended.
    public let lastProcessObservedAt: String?
    public let taskReference: String?
    public let purpose: String?

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), let actorID = raw.string("actor_id"), let projectID = raw.string("project_id") else {
            return nil
        }
        // A keepalive lease is ServerPilot's own hold on an idle card, never a
        // user's task. The broker already withholds it; dropping it here too
        // keeps a server-side regression from surfacing an internal lease as
        // somebody's work. Per-card keepalive state travels on the GPU row.
        guard (raw.string("kind") ?? "workload").lowercased() != "keepalive" else {
            return nil
        }
        self.id = id
        self.requestID = raw.string("request_id")
        self.actorID = actorID
        self.projectID = projectID
        self.kind = (raw.string("kind") ?? "workload").lowercased()
        self.state = (raw.string("state") ?? "UNKNOWN").uppercased()
        self.runtimeState = (raw.string("runtime_state") ?? "ASSIGNED").uppercased()
        self.gpuIDs = raw["gpu_ids"] as? [String] ?? []
        self.issuedAt = raw.string("issued_at")
        self.expiresAt = raw.string("expires_at")
        self.lastProcessObservedAt = raw.string("last_process_observed_at")
        self.taskReference = raw.string("task_ref")
        self.purpose = raw.string("purpose")
    }

    public var stateLabel: String {
        switch state {
        case "ACTIVE", "HELD": return "使用中"
        case "CONFLICT": return "归属冲突"
        case "ORPHANED_BUSY": return "释放后仍占用"
        case "RELEASED": return "已释放"
        case "EXPIRED": return "已过期"
        default: return state
        }
    }

}

public struct AllocationRequestRecord: Identifiable, Equatable, Sendable {
    public let id: String
    public let actorID: String
    public let projectID: String
    public let taskReference: String
    public let purpose: String
    public let state: String
    public let blockedReason: String?
    public let gpuCount: Int
    public let createdAt: String?

    public init?(raw: [String: Any]) {
        guard
            let id = raw.string("id"),
            let actorID = raw.string("actor_id"),
            let projectID = raw.string("project_id"),
            let taskReference = raw.string("task_ref")
        else {
            return nil
        }
        self.id = id
        self.actorID = actorID
        self.projectID = projectID
        self.taskReference = taskReference
        self.purpose = raw.string("purpose") ?? ""
        self.state = (raw.string("state") ?? "UNKNOWN").uppercased()
        self.blockedReason = raw.string("blocked_reason")
        self.gpuCount = (raw["constraints"] as? [String: Any])?.int("gpu_count", default: 1) ?? 1
        self.createdAt = raw.string("created_at")
    }

    public var stateLabel: String {
        switch state {
        case "QUEUED": return "排队中"
        case "PENDING_APPROVAL": return "等待批准"
        case "ACTIVE": return "使用中"
        case "CANCELLED": return "已取消"
        case "RELEASED": return "已释放"
        default: return state
        }
    }
}

public struct ClaimSubmissionResult: Equatable, Sendable {
    public let allocated: Bool
    public let message: String

    public init(allocated: Bool, message: String) {
        self.allocated = allocated
        self.message = message
    }
}

public struct ClaimDraft: Equatable, Sendable {
    public var projectID: String
    public var taskReference: String
    public var purpose: String
    public var gpuCount: Int
    public var endpointID: String
    public var serverGroupID: String?
    public var minimumCPUCores: Double?
    public var minimumMemoryMiB: Int?
    public var minimumTotalVRAMMiB: Int?
    public var minimumFreeVRAMMiB: Int?

    public init(
        projectID: String,
        taskReference: String,
        purpose: String,
        gpuCount: Int,
        endpointID: String,
        serverGroupID: String? = nil,
        minimumCPUCores: Double? = nil,
        minimumMemoryMiB: Int? = nil,
        minimumTotalVRAMMiB: Int? = nil,
        minimumFreeVRAMMiB: Int? = nil
    ) {
        self.projectID = projectID
        self.taskReference = taskReference
        self.purpose = purpose
        self.gpuCount = gpuCount
        self.endpointID = endpointID
        self.serverGroupID = serverGroupID
        self.minimumCPUCores = minimumCPUCores
        self.minimumMemoryMiB = minimumMemoryMiB
        self.minimumTotalVRAMMiB = minimumTotalVRAMMiB
        self.minimumFreeVRAMMiB = minimumFreeVRAMMiB
    }
}

public struct EndpointDraft: Equatable, Sendable {
    public let id: String
    public let host: String
    public let port: Int
    public let sshUser: String
    public let workspacePath: String
    public let observationProfile: String
    public let serverGroupID: String?
    public let workspacePathOverride: String?

    public init(
        host: String,
        port: Int,
        sshUser: String,
        workspacePath: String,
        observationProfile: String,
        suppliedID: String,
        serverGroupID: String? = nil,
        workspacePathOverride: String? = nil
    ) throws {
        let cleanedHost = host.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedUser = sshUser.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedWorkspacePath = workspacePath.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedID = suppliedID.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedGroupID = serverGroupID?.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedGroupID = (cleanedGroupID?.isEmpty == false) ? cleanedGroupID : nil
        let cleanedOverride = workspacePathOverride?.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedOverride = (cleanedOverride?.isEmpty == false) ? cleanedOverride : nil
        let inheritedWorkspace = resolvedGroupID != nil && cleanedWorkspacePath.isEmpty
        guard
            !cleanedHost.isEmpty,
            (1...65535).contains(port),
            Self.isValidUser(cleanedUser),
            inheritedWorkspace || CoreFieldValidation.isAbsoluteWorkspacePath(cleanedWorkspacePath),
            CoreFieldValidation.isSlugID(cleanedID.isEmpty ? Self.defaultID(host: cleanedHost, port: port) : cleanedID),
            resolvedGroupID == nil || CoreFieldValidation.isSlugID(resolvedGroupID ?? ""),
            resolvedOverride == nil || CoreFieldValidation.isAbsoluteWorkspacePath(resolvedOverride ?? ""),
            resolvedOverride == nil || resolvedGroupID != nil
        else {
            throw EndpointDraftError.invalidEndpointFields
        }
        id = cleanedID.isEmpty ? Self.defaultID(host: cleanedHost, port: port) : cleanedID
        self.host = cleanedHost
        self.port = port
        self.sshUser = cleanedUser
        self.workspacePath = cleanedWorkspacePath
        self.observationProfile = observationProfile
        self.serverGroupID = resolvedGroupID
        self.workspacePathOverride = resolvedOverride
    }

    private static func defaultID(host: String, port: Int) -> String {
        let normalized = host.lowercased().map { character -> Character in
            character.isASCII && (character.isLetter || character.isNumber) ? character : "-"
        }
        let compact = String(normalized)
            .split(separator: "-", omittingEmptySubsequences: true)
            .joined(separator: "-")
        let base = compact.first?.isLetter == true ? compact : "server-\(compact)"
        return String("\(base)-p\(port)".prefix(120))
    }

    private static func isValidUser(_ value: String) -> Bool {
        guard let first = value.unicodeScalars.first else { return false }
        guard CharacterSet.letters.union(CharacterSet(charactersIn: "_")).contains(first) else { return false }
        return value.unicodeScalars.allSatisfy {
            CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "_-" )).contains($0)
        }
    }
}

public enum EndpointDraftError: LocalizedError, Equatable, Sendable {
    case invalidEndpointFields

    public var errorDescription: String? {
        "请填写有效的服务器地址、端口、SSH 用户、绝对远端工作区路径和服务器标识。"
    }
}

public struct ObservationProfileRecord: Equatable, Sendable, Identifiable, Hashable {
    public let id: String
    public let displayName: String
    public let description: String
    public let source: String
    public let capabilities: [String]

    public init(
        id: String,
        displayName: String,
        description: String,
        source: String,
        capabilities: [String]
    ) {
        self.id = id
        self.displayName = displayName
        self.description = description
        self.source = source
        self.capabilities = capabilities
    }

    public init?(raw: [String: Any]) {
        guard let id = raw.string("id"), !id.isEmpty else { return nil }
        self.id = id
        self.displayName = raw.string("display_name") ?? id
        self.description = raw.string("description") ?? ""
        self.source = raw.string("source") ?? "builtin"
        self.capabilities = (raw["capabilities"] as? [String]) ?? []
    }

    public static let serverCatalogFallback: [ObservationProfileRecord] = [
        ObservationProfileRecord(
            id: "linux-nvidia",
            displayName: "标准 NVIDIA 采集",
            description: "使用内置、只读的 Linux NVIDIA 观测配置。",
            source: "builtin",
            capabilities: ["observe"]
        ),
        ObservationProfileRecord(
            id: "linux-host",
            displayName: "主机容量采集",
            description: "使用内置、只读的 Linux 主机容量观测配置。",
            source: "builtin",
            capabilities: ["observe"]
        ),
        ObservationProfileRecord(
            id: "server-script-v1",
            displayName: "服务器采集脚本",
            description: "使用远端密封只读采集脚本；不能输入命令或容器参数。",
            source: "builtin",
            capabilities: ["observe"]
        ),
    ]
}

public struct EndpointUpdateDraft: Equatable, Sendable {
    public let sshUser: String
    public let workspacePath: String
    public let observationProfile: String
    public let serverGroupID: String?
    public let workspacePathOverride: String?
    public let includesGroupAssignment: Bool

    public init(endpoint: EndpointRecord) {
        sshUser = endpoint.sshUser
        workspacePath = endpoint.workspacePath ?? ""
        observationProfile = endpoint.observationProfile
        serverGroupID = endpoint.serverGroupID
        workspacePathOverride = endpoint.workspacePathOverride
        includesGroupAssignment = endpoint.serverGroupID != nil || endpoint.workspacePathOverride != nil
    }

    public init(
        sshUser: String,
        workspacePath: String,
        observationProfile: String
    ) throws {
        try self.init(
            sshUser: sshUser,
            workspacePath: workspacePath,
            observationProfile: observationProfile,
            serverGroupID: nil,
            workspacePathOverride: nil,
            includesGroupAssignment: false
        )
    }

    public init(
        sshUser: String,
        workspacePath: String,
        observationProfile: String,
        serverGroupID: String?,
        workspacePathOverride: String?
    ) throws {
        try self.init(
            sshUser: sshUser,
            workspacePath: workspacePath,
            observationProfile: observationProfile,
            serverGroupID: serverGroupID,
            workspacePathOverride: workspacePathOverride,
            includesGroupAssignment: true
        )
    }

    private init(
        sshUser: String,
        workspacePath: String,
        observationProfile: String,
        serverGroupID: String?,
        workspacePathOverride: String?,
        includesGroupAssignment: Bool
    ) throws {
        let cleanedUser = sshUser.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedWorkspacePath = workspacePath.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedGroupID = serverGroupID?.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedGroupID = (cleanedGroupID?.isEmpty == false) ? cleanedGroupID : nil
        let cleanedOverride = workspacePathOverride?.trimmingCharacters(in: .whitespacesAndNewlines)
        let resolvedOverride = (cleanedOverride?.isEmpty == false) ? cleanedOverride : nil
        let inheritedWorkspace = resolvedGroupID != nil && cleanedWorkspacePath.isEmpty
        guard
            !cleanedUser.isEmpty,
            inheritedWorkspace || CoreFieldValidation.isAbsoluteWorkspacePath(cleanedWorkspacePath),
            resolvedGroupID == nil || CoreFieldValidation.isSlugID(resolvedGroupID ?? ""),
            resolvedOverride == nil || CoreFieldValidation.isAbsoluteWorkspacePath(resolvedOverride ?? ""),
            resolvedOverride == nil || resolvedGroupID != nil
        else { throw EndpointDraftError.invalidEndpointFields }
        self.sshUser = cleanedUser
        self.workspacePath = cleanedWorkspacePath
        self.observationProfile = observationProfile
        self.serverGroupID = resolvedGroupID
        self.workspacePathOverride = resolvedOverride
        self.includesGroupAssignment = includesGroupAssignment
    }
}

public struct ServerGroupDraft: Equatable, Sendable {
    public let id: String
    public let displayName: String
    public let workspacePath: String
    public let environmentNotes: String
    public let description: String

    public init(
        id: String,
        displayName: String,
        workspacePath: String,
        environmentNotes: String = "",
        description: String = ""
    ) throws {
        let cleanedID = id.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedPath = workspacePath.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedNotes = environmentNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedDescription = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            CoreFieldValidation.isSlugID(cleanedID),
            CoreFieldValidation.isPlainDisplayName(cleanedName),
            CoreFieldValidation.isAbsoluteWorkspacePath(cleanedPath),
            CoreFieldValidation.isPlainEnvironmentNotes(cleanedNotes),
            CoreFieldValidation.isPlainDescription(cleanedDescription)
        else {
            throw ServerGroupDraftError.invalidGroupFields
        }
        self.id = cleanedID
        self.displayName = cleanedName
        self.workspacePath = cleanedPath
        self.environmentNotes = cleanedNotes
        self.description = cleanedDescription
    }
}

public struct ServerGroupUpdateDraft: Equatable, Sendable {
    public let displayName: String
    public let workspacePath: String
    public let environmentNotes: String
    public let description: String

    public init(
        displayName: String,
        workspacePath: String,
        environmentNotes: String = "",
        description: String = ""
    ) throws {
        let cleanedName = displayName.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedPath = workspacePath.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedNotes = environmentNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedDescription = description.trimmingCharacters(in: .whitespacesAndNewlines)
        guard
            CoreFieldValidation.isPlainDisplayName(cleanedName),
            CoreFieldValidation.isAbsoluteWorkspacePath(cleanedPath),
            CoreFieldValidation.isPlainEnvironmentNotes(cleanedNotes),
            CoreFieldValidation.isPlainDescription(cleanedDescription)
        else {
            throw ServerGroupDraftError.invalidGroupFields
        }
        self.displayName = cleanedName
        self.workspacePath = cleanedPath
        self.environmentNotes = cleanedNotes
        self.description = cleanedDescription
    }

    public init(group: ServerGroupRecord) throws {
        try self.init(
            displayName: group.displayName,
            workspacePath: group.workspacePath,
            environmentNotes: group.environmentNotes,
            description: group.description
        )
    }
}

public enum ServerGroupDraftError: LocalizedError, Equatable, Sendable {
    case invalidGroupFields

    public var errorDescription: String? {
        "请填写有效的分组标识、显示名称、绝对工作路径和纯文本说明。"
    }
}

enum CoreFieldValidation {
    private static let asciiLower = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyz")
    private static let asciiSlugRest = asciiLower.union(CharacterSet(charactersIn: "0123456789-"))

    /// Backend id pattern: `^[a-z][a-z0-9-]{1,127}$` (2...128 ASCII characters).
    static func isSlugID(_ value: String) -> Bool {
        guard (2...128).contains(value.count), let first = value.unicodeScalars.first else { return false }
        guard asciiLower.contains(first) else { return false }
        return value.unicodeScalars.dropFirst().allSatisfy { asciiSlugRest.contains($0) }
    }

    static func isAbsoluteWorkspacePath(_ value: String) -> Bool {
        value.hasPrefix("/") && !value.contains("\0") && !value.contains("\n") && !value.contains("\r")
    }

    static func isPlainDisplayName(_ value: String) -> Bool {
        guard (1...120).contains(value.count) else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            scalar != "\0" && scalar != "\n" && scalar != "\r" && scalar != "\t"
        }
    }

    static func isPlainEnvironmentNotes(_ value: String) -> Bool {
        isPlainText(value, maxLength: 8_000)
    }

    static func isPlainDescription(_ value: String) -> Bool {
        isPlainText(value, maxLength: 1_000)
    }

    static func isPlainText(_ value: String, maxLength: Int) -> Bool {
        guard value.count <= maxLength else { return false }
        return !value.contains("\0")
    }
}

public extension Dictionary where Key == String, Value == Any {
    func string(_ key: String) -> String? {
        self[key] as? String
    }

    func int(_ key: String, default fallback: Int = 0) -> Int {
        optionalInt(key) ?? fallback
    }

    func optionalInt(_ key: String) -> Int? {
        if let value = self[key] as? Int { return value }
        if let value = self[key] as? NSNumber { return value.intValue }
        if let value = self[key] as? String { return Int(value) }
        return nil
    }

    func optionalDouble(_ key: String) -> Double? {
        if let value = self[key] as? Double { return value }
        if let value = self[key] as? NSNumber { return value.doubleValue }
        if let value = self[key] as? String { return Double(value) }
        return nil
    }

    func optionalFraction(_ key: String) -> Double? {
        optionalDouble(key).map { Swift.min(Swift.max($0, 0), 1) }
    }

    func optionalPercent(_ key: String) -> Double? {
        optionalDouble(key).map { Swift.min(Swift.max($0 / 100, 0), 1) }
    }

    func bool(_ key: String, default fallback: Bool) -> Bool {
        optionalBool(key) ?? fallback
    }

    func optionalBool(_ key: String) -> Bool? {
        if let value = self[key] as? Bool { return value }
        if let value = self[key] as? NSNumber { return value.boolValue }
        return nil
    }
}
