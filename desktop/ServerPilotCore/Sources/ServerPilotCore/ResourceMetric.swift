import Foundation

public enum MetricPressure: Sendable {
    case normal
    case warning
    case critical
}

/// How full a reading is, and so what colour its bar takes.
///
/// One rule for every metric: yellow from nearly full, the same line a card's
/// square turns yellow at, and critical when nothing is left to give. Full is a
/// reading, not a fault: frameworks reserve nearly all of a card's memory as a
/// matter of course. Every bar and square asks here, so the App has one answer.
public enum ResourceMetric: Sendable {
    case gpuUtilization
    case gpuMemory
    case cpu
    case memory

    /// Another job would struggle to fit from here.
    public static let nearlyFull = 0.80

    /// Nothing is left to give.
    private static let exhausted = 0.95

    public func pressure(_ fraction: Double) -> MetricPressure {
        if fraction >= Self.exhausted { return .critical }
        return fraction >= Self.nearlyFull ? .warning : .normal
    }
}
