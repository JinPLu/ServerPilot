import Foundation
import Testing
@testable import ServerPilotCore

struct UtilizationTrendsTests {
    private func series(_ values: [Any]) -> [String: Any] {
        ["window_seconds": 3600, "points": values.map { ["at": "2026-11-02T00:00:00Z", "gpu_utilization_pct": $0] }]
    }

    @Test func aGapStaysAGapRatherThanReadingAsIdle() throws {
        let trends = try #require(UtilizationTrends(payload: [
            "fleet": series([50, NSNull(), 25]),
            "servers": ["a": series([100])],
        ]))
        #expect(trends.fleet?.points == [0.5, nil, 0.25])
        #expect(trends.servers["a"]?.latest == 1.0)
    }

    @Test func theLatestReadingSkipsTrailingGaps() {
        let line = UtilizationSeries(windowSeconds: 3600, points: [0.4, 0.2, nil])
        #expect(line.latest == 0.2)
        #expect(UtilizationSeries(windowSeconds: 3600, points: [nil, nil]).hasData == false)
    }

    @Test func aGapIsOnlyWhatLiesBetweenTwoReadings() {
        let line = UtilizationSeries(windowSeconds: 3600, points: [nil, 0.5, 0.6, nil, nil, 0.4, nil])
        #expect(line.gaps == [
            .init(beforeIndex: 2, afterIndex: 5, before: 0.6, after: 0.4, seconds: 1028),
        ])
        #expect(UtilizationSeries(windowSeconds: 3600, points: [0.5, 0.6]).gaps.isEmpty)
    }

    @Test func aMalformedPayloadIsRefusedWhole() {
        #expect(UtilizationTrends(payload: ["servers": [:] as [String: [String: Any]]]) == nil)
    }

    @Test func aLeaseKnowsWhetherItsDurationWasDeclared() throws {
        let declared = try #require(LeaseRecord(raw: [
            "id": "l", "actor_id": "claude-code", "project_id": "p",
            "duration_seconds": 3600, "duration_declared": true,
        ]))
        #expect(declared.declaredDurationSeconds == 3600)
        let undeclared = try #require(LeaseRecord(raw: [
            "id": "l", "actor_id": "claude-code", "project_id": "p",
            "duration_seconds": 2_592_000, "duration_declared": false,
        ]))
        #expect(undeclared.declaredDurationSeconds == nil)
    }
}
