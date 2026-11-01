import Foundation

/// One line of recent GPU utilization. A nil point is a gap in collection, and
/// a chart breaks there rather than drawing an outage as idle cards.
public struct UtilizationSeries: Equatable, Sendable {
    public let windowSeconds: Int
    public let points: [Double?]

    public init(windowSeconds: Int, points: [Double?]) {
        self.windowSeconds = windowSeconds
        self.points = points
    }

    init?(raw: [String: Any]) {
        guard let window = raw.optionalInt("window_seconds"),
              let rows = raw["points"] as? [[String: Any]] else { return nil }
        windowSeconds = window
        points = rows.map { row in
            guard let percent = row["gpu_utilization_pct"] as? NSNumber else { return nil }
            return percent.doubleValue / 100
        }
    }

    /// The newest reading, nil when nothing recent was collected.
    public var latest: Double? {
        points.last(where: { $0 != nil }) ?? nil
    }

    public var hasData: Bool { latest != nil }
}

/// The fleet line for the overview and one short line per server, from one read.
public struct UtilizationTrends: Equatable, Sendable {
    public let fleet: UtilizationSeries?
    public let servers: [String: UtilizationSeries]

    public static let empty = UtilizationTrends(fleet: nil, servers: [:])

    public init(fleet: UtilizationSeries?, servers: [String: UtilizationSeries]) {
        self.fleet = fleet
        self.servers = servers
    }

    /// `payload` is the envelope's `data`.
    public init?(payload: [String: Any]) {
        guard let fleetRaw = payload["fleet"] as? [String: Any],
              let fleet = UtilizationSeries(raw: fleetRaw),
              let serversRaw = payload["servers"] as? [String: [String: Any]] else { return nil }
        self.fleet = fleet
        servers = serversRaw.compactMapValues(UtilizationSeries.init(raw:))
    }
}
