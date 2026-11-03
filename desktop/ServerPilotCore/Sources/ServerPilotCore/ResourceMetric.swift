import Foundation

public enum MetricPressure: Sendable {
    case normal
    case warning
    case critical
}

/// Which readings deserve a colour, and from where.
///
/// A metric decides this for itself: a GPU at 100% is doing what it is for, while
/// host memory at 100% is about to fail the job. Every bar, figure and square asks
/// here, so the App has one answer.
public enum ResourceMetric: Sendable {
    case gpuUtilization
    case gpuMemory
    case cpu
    case memory

    /// Another job would struggle to fit from here.
    public static let nearlyFull = 0.80

    private var thresholds: (warning: Double?, critical: Double?) {
        switch self {
        case .gpuUtilization: (nil, nil)
        case .gpuMemory: (Self.nearlyFull, nil)
        case .cpu: (0.90, nil)
        case .memory: (Self.nearlyFull, 0.95)
        }
    }

    public func pressure(_ fraction: Double) -> MetricPressure {
        if let critical = thresholds.critical, fraction >= critical { return .critical }
        if let warning = thresholds.warning, fraction >= warning { return .warning }
        return .normal
    }
}
