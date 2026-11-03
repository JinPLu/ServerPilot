import Foundation

/// What one card's square says: how full the card is, not who holds it.
///
/// Who holds a card stays in the words beside the squares. Keepalive counts as
/// free because it steps aside on a claim, whatever memory it reserves.
public enum CardLoad: String, CaseIterable, Sendable {
    case free
    case low
    case nearFull
    /// In use, but nothing measured it. A cluster's allocation count is a
    /// scheduling fact, so it must not read as a low load.
    case unmeasured
    case fault
    case stale

    public var label: String {
        switch self {
        case .free: "空闲"
        case .low: "利用率低"
        case .nearFull: "接近满"
        case .unmeasured: "利用率未知"
        case .fault: "有错误"
        case .stale: "数据过期"
        }
    }

    /// The busier of compute and memory decides: either one can stop another job fitting.
    public init(state: String, utilization: Double?, memory: Double?) {
        switch state {
        case "AVAILABLE", "KEEPALIVE":
            self = .free
        case "RUNNING_MANAGED", "HELD", "LEASED_IDLE", "BUSY_UNMANAGED":
            guard let peak = [utilization, memory].compactMap({ $0 }).max() else {
                self = .unmeasured
                return
            }
            self = peak >= ResourceMetric.nearlyFull ? .nearFull : .low
        case "CONFLICT", "ORPHANED_BUSY", "UNHEALTHY", "ERROR", "MAINTENANCE":
            self = .fault
        default:
            self = .stale
        }
    }
}

extension GPURecord {
    /// The current reading, the same one the server table prints beside the squares.
    public var load: CardLoad {
        CardLoad(
            state: state,
            utilization: utilization.map { Double($0) / 100 },
            memory: memoryUsedMiB == nil ? nil : memoryFraction
        )
    }
}
