import Foundation

/// The outcome of something a person asked for. It stands until they dismiss
/// it: a refresh arriving a second later says nothing about whether their
/// action worked, and used to wipe the only record that it had not.
public struct Feedback: Equatable, Sendable, Identifiable {
    public enum Kind: Equatable, Sendable {
        case success
        /// Nothing went wrong, but it did not happen. Comes with a next step.
        case refused
        case failure
        case pending
    }

    public let id = UUID()
    public let kind: Kind
    public let message: String

    public init(_ kind: Kind, _ message: String) {
        self.kind = kind
        self.message = message
    }

    public static func == (lhs: Feedback, rhs: Feedback) -> Bool {
        lhs.kind == rhs.kind && lhs.message == rhs.message
    }
}

public struct PulseRecord: Equatable, Sendable {
    public let version: String
    public let revision: Int
    public let openAlerts: Int

    public init?(_ payload: [String: Any]) {
        guard let version = payload["version"] as? String,
              let revision = payload["revision"] as? Int else { return nil }
        self.version = version
        self.revision = revision
        self.openAlerts = payload["open_alerts"] as? Int ?? 0
    }
}

public struct AlertRecord: Equatable, Sendable, Identifiable {
    public enum Category: String, Sendable, CaseIterable {
        case taskImpacted = "task_impacted"
        case serverAbnormal = "server_abnormal"
        case resourceIdle = "resource_idle"
        case capacityAvailable = "capacity_available"

        public var title: String {
            switch self {
            case .taskImpacted: "任务受影响"
            case .serverAbnormal: "服务器异常"
            case .resourceIdle: "资源闲置"
            case .capacityAvailable: "有卡可用"
            }
        }
    }

    public let id: String
    public let category: Category
    public let state: String
    public let message: String
    public let task: String?
    public let firstSeenAt: String
    public let alertIDs: [String]

    public init?(_ payload: [String: Any]) {
        guard let id = payload["id"] as? String,
              let raw = payload["category"] as? String,
              let category = Category(rawValue: raw),
              let message = payload["message"] as? String else { return nil }
        self.id = id
        self.category = category
        self.state = payload["state"] as? String ?? "active"
        self.message = message
        self.task = payload["task"] as? String
        self.firstSeenAt = payload["first_seen_at"] as? String ?? ""
        self.alertIDs = payload["alert_ids"] as? [String] ?? [id]
    }

    public var isOpen: Bool { state == "active" }
}
