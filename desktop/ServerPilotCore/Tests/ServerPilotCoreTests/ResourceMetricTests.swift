import Foundation
import Testing
@testable import ServerPilotCore

struct ResourceMetricTests {
    @Test func everyMetricSharesOneRule() {
        for metric in [ResourceMetric.gpuUtilization, .gpuMemory, .cpu, .memory] {
            #expect(metric.pressure(0.79) == .normal)
            #expect(metric.pressure(0.80) == .warning)
            #expect(metric.pressure(0.94) == .warning)
            #expect(metric.pressure(0.95) == .critical)
        }
    }

    @Test func aNodeCarriesItsLastReadingOnceItGoesQuiet() throws {
        let node = try #require(ResourceNodeRecord(raw: [
            "node_id": "self",
            "fresh": false,
            "usage": ["gpu_utilization_pct": NSNull(), "memory_used_pct": NSNull()],
            "last_usage": ["gpu_utilization_pct": 40, "memory_used_pct": 25],
        ]))
        #expect(node.gpuUtilizationFraction == nil)
        #expect(node.lastUsage?.gpuUtilization == 0.40)
        #expect(node.lastUsage?.memory == 0.25)
        #expect(node.lastUsage?.cpu == nil)
    }
}
