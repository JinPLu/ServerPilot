import Foundation

/// What one card's square says: how full the card is, not who holds it.
///
/// Who holds a card stays in the words beside the squares. Keepalive counts as
/// free because it steps aside on a claim, whatever memory it reserves.
public enum CardLoad: String, CaseIterable, Sendable {
    case free
    case low
    case nearFull
    case full
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
        case .full: "已满"
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
            // The bars' rule, so a square and the bar beside it never disagree.
            self = switch ResourceMetric.gpuUtilization.pressure(peak) {
            case .normal: .low
            case .warning: .nearFull
            case .critical: .full
            }
        case "CONFLICT", "ORPHANED_BUSY", "UNHEALTHY", "ERROR", "MAINTENANCE":
            self = .fault
        default:
            self = .stale
        }
    }
}

extension CardLoad {
    /// The bar colour a measured square shares; nil for a square that is not a reading.
    public var pressure: MetricPressure? {
        switch self {
        case .low: .normal
        case .nearFull: .warning
        case .full: .critical
        case .free, .unmeasured, .fault, .stale: nil
        }
    }
}

extension GPURecord {
    /// The short average, the same reading the server table prints beside the squares.
    public var load: CardLoad {
        CardLoad(
            state: state,
            utilization: shortTelemetryAverage?.utilizationFraction,
            memory: shortTelemetryAverage?.memoryFraction
        )
    }
}
