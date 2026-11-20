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

    public init?(_ payload: [String: Any]) {
        guard let version = payload["version"] as? String,
              let revision = payload["revision"] as? Int else { return nil }
        self.version = version
        self.revision = revision
    }
}
