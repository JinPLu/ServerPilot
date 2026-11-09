import Foundation
import Testing
@testable import ServerPilotCore

struct ResourceNodeUsageTests {
    @Test func directAndClusterDecodeTheSameUsage() throws {
        for id in ["self", "gnode07"] {
            // The table reads the short average; `usage` is the single latest
            // collection, kept for agents and admission.
            let sample = try #require(ResourceNodeRecord(raw: [
                "node_id": id, "fresh": true, "gpu_ids": ["gpu"],
                "usage": ["gpu_utilization_pct": 99, "gpu_memory_used_pct": 99,
                          "cpu_utilization_pct": 99, "memory_used_pct": 99],
                "short_usage": ["window_seconds": 30, "gpu_utilization_pct": 60, "gpu_memory_used_pct": 50,
                                "cpu_utilization_pct": 25, "memory_used_pct": 75]]))
            #expect(sample.gpuUtilizationFraction == 0.6)
            #expect(sample.gpuMemoryFraction == 0.5)
            #expect(sample.cpuUtilizationFraction == 0.25)
            #expect(sample.memoryFraction == 0.75)
            #expect(sample.averageWindowSeconds == 30)
        }
    }

    @Test func oldMetricsAreNotCurrentAfterFailure() throws {
        let sample = try #require(ResourceNodeRecord(raw: ["node_id": "gnode07", "fresh": false,
            "message": "SSH rejected", "short_usage": ["window_seconds": 30, "gpu_utilization_pct": 60, "memory_used_pct": 75]]))
        #expect(sample.gpuUtilizationFraction == nil)
        #expect(sample.memoryFraction == nil)
        #expect(sample.message == "SSH rejected")
    }
}
