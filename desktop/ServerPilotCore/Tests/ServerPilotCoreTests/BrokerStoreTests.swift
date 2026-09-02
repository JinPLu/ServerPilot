import Combine
import Foundation
import Testing
@testable import ServerPilotCore

@MainActor
@Suite(.serialized) struct BrokerStoreTests {
    @Test func testManualTriggersCoalesceBehindSingleActiveRefresh() async throws {
        let client = DelayedSequenceClient(
            snapshots: [try Self.snapshot(named: "1"), try Self.snapshot(named: "8")],
            delayNanoseconds: 50_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        store.reload()
        store.reload()

        try await waitUntil { store.snapshot.summary.totalGPUs == 8 && !store.isRefreshing }
        let metrics = await client.metrics()
        #expect(metrics.callCount == 2)
        #expect(metrics.maxConcurrentCalls == 1)
        #expect(store.snapshot.summary.totalGPUs == 8)
        #expect(store.freshness == .fresh)
    }

    @Test func testEquivalentRefreshDoesNotRepublishSnapshotOrLastGoodSnapshot() async throws {
        let initial = try Self.snapshot(named: "1")
        var equivalent = initial
        equivalent.serverTime = "2026-08-10T10:00:00Z"
        let client = DelayedSequenceClient(
            snapshots: [initial, equivalent],
            delayNanoseconds: 5_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        var snapshotPublicationCount = 0
        var lastGoodPublicationCount = 0
        let snapshotSubscription = store.$snapshot.dropFirst().sink { _ in
            snapshotPublicationCount += 1
        }
        let lastGoodSubscription = store.$lastGoodSnapshot.dropFirst().sink { _ in
            lastGoodPublicationCount += 1
        }
        defer {
            snapshotSubscription.cancel()
            lastGoodSubscription.cancel()
        }

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == initial.snapshotRevision && !store.isRefreshing }
        snapshotPublicationCount = 0
        lastGoodPublicationCount = 0

        store.reload()
        try await waitUntilAsync { await client.metrics().callCount == 2 }
        try await waitUntil { !store.isRefreshing }

        #expect(snapshotPublicationCount == 0)
        #expect(lastGoodPublicationCount == 0)
        #expect(store.snapshot.serverTime == initial.serverTime)
        #expect(store.lastGoodSnapshot?.serverTime == initial.serverTime)
        #expect(store.freshness == .fresh)
        #expect(store.isConnected)
        #expect(store.errorMessage == nil)
    }

    @Test func testEquivalentRefreshRecoversFreshnessAndClearsError() async throws {
        let initial = try Self.snapshot(named: "1")
        var equivalent = initial
        equivalent.serverTime = "2026-08-10T10:00:00Z"
        let client = ScriptedClient(results: [
            .success(initial),
            .failure(BrokerRefreshError.invalidSnapshot),
            .success(equivalent)
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == initial.snapshotRevision && !store.isRefreshing }

        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }
        #expect(!(store.isConnected))
        #expect(store.errorMessage != nil)

        store.reload()
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        #expect(store.isConnected)
        #expect(store.errorMessage == nil)
        #expect(store.snapshot.serverTime == initial.serverTime)
        #expect(store.lastGoodSnapshot?.serverTime == initial.serverTime)
    }

    @Test func testTimeoutDoesNotCommitCancellationIgnoringClient() async throws {
        let client = CancellationIgnoringClient(
            snapshot: try Self.snapshot(named: "8"),
            delayNanoseconds: 200_000_000
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 0.03, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)

        try await waitUntil { store.freshness == .failed }
        #expect(store.snapshot == .empty)
        #expect(!(store.isRefreshing))

        try await Task.sleep(nanoseconds: 260_000_000)
        #expect(store.snapshot == .empty)
        #expect(store.freshness == .failed)
        let callCount = await client.metrics()
        #expect(callCount == 1)
    }

    @Test func testLastGoodSnapshotSurvivesStaleFailureAndThenRecovers() async throws {
        let client = ScriptedClient(results: [
            .success(try Self.snapshot(named: "1")),
            .failure(BrokerRefreshError.invalidSnapshot),
            .success(try Self.snapshot(named: "8"))
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.summary.totalGPUs == 1 }

        store.reload()
        try await waitUntil { store.freshness == .stale }
        #expect(store.snapshot.summary.totalGPUs == 1)
        #expect(store.lastGoodSnapshot?.summary.totalGPUs == 1)
        #expect(store.errorMessage != nil)

        store.reload()
        try await waitUntil { store.snapshot.summary.totalGPUs == 8 }
        #expect(store.freshness == .fresh)
        #expect(store.errorMessage == nil)
    }

    @Test func testFailureBeforeAnySnapshotHasFailedFreshness() async throws {
        let client = ScriptedClient(results: [.failure(BrokerRefreshError.invalidSnapshot)])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)

        try await waitUntil { store.freshness == .failed }
        #expect(store.snapshot == .empty)
        #expect(store.lastGoodSnapshot == nil)
        #expect(store.errorMessage != nil)
    }

    @Test func testSuccessfulSnapshotKeepsControlPlaneFreshWhenOneEndpointHasOldTelemetry() async throws {
        let client = ScriptedClient(results: [.success(try Self.snapshot(named: "stale"))])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(
            snapshotClient: client,
            baseURL: URL(string: "http://127.0.0.1:8787/")
        )

        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }
        #expect(store.isConnected)
        #expect(store.allowsMutations)
        #expect(store.snapshot.dataAgeSeconds == 94)
        #expect(store.snapshot.freshnessSeconds == 30)
        #expect(store.snapshot.endpoints.first?.monitorLabel == "采集延迟")
        #expect(store.snapshot.endpoints.first?.monitorDetail == "最近一次服务器数据已过期")
    }

    @Test func testEndpointFailureExplainsARecordedObservationTimeout() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "error"))

        // The cause comes from the broker's closed error code, and the app
        // adds how long the silence has lasted. It no longer guesses a cause
        // by matching English substrings in the server's own message.
        #expect(store.snapshot.endpoints.first?.monitorLabel == "连接失败")
        #expect(store.snapshot.endpoints.first?.monitorErrorCode == "connect_timeout")
        #expect(
            store.snapshot.endpoints.first?.monitorDetail
                == "连接超时 · 检查网络或 VPN · 上次成功 7 分钟前"
        )
    }

    @Test func testFixtureWithOldEndpointTelemetryRemainsLoadedAndReadOnly() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "stale"))

        #expect(store.freshness == .fresh)
        #expect(store.isConnected)
        #expect(!(store.allowsMutations))
    }

    @Test func testFixtureCommunicatesFixedReadOnlyBehavior() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.useFixture(snapshot: try Self.snapshot(named: "8"))

        #expect(store.freshness == .fresh)
        #expect(!(store.allowsMutations))
        #expect(!(store.canRefresh))
        #expect(store.mutationUnavailableReason == "当前为只读测试夹具或尚未连接本机服务，不能执行资源变更。")
    }

    @Test func testSnapshotParsesTenMinuteOverviewMetricsSeparatelyFromCurrentTelemetry() throws {
        let snapshot = try Self.snapshot(named: "resource-ownership")
        let endpoint = try #require(snapshot.endpoint(id: "gpu-node-01"))
        let gpu = try #require(snapshot.gpu(id: "gpu-node-01:GPU-FIXTURE-0"))

        #expect(endpoint.cpuLoadFraction == nil)
        #expect(endpoint.recentTelemetryAverage?.windowSeconds == 600)
        #expect(endpoint.recentTelemetryAverage?.sampleCount == 10)
        #expect(endpoint.recentTelemetryAverage?.cpuLoadFraction == 0.21)
        #expect(endpoint.recentTelemetryAverage?.memoryFraction == 0.34)

        #expect(gpu.utilization == 67)
        #expect(gpu.recentTelemetryAverage?.windowSeconds == 600)
        #expect(gpu.recentTelemetryAverage?.sampleCount == 10)
        #expect(gpu.recentTelemetryAverage?.utilizationFraction == 0.52)
        #expect(gpu.recentTelemetryAverage?.memoryFraction == 0.35)
    }

    @Test func testEndpointDraftUsesServerListedObservationProfileId() throws {
        // Intentionally replaces the former sealed CaseIterable enum: a
        // plugin id cannot be represented by that enum, so drafts now store
        // the server-validated profile id as a String.
        let draft = try EndpointDraft(
            host: "gpu.example.test",
            port: 2201,
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "linux",
            suppliedID: ""
        )

        #expect(draft.id == "gpu-example-test-p2201")
        #expect(draft.host == "gpu.example.test")
        #expect(draft.workspacePath == "/srv/storyboard")
        #expect(draft.observationProfile == "linux")
        #expect(
            ObservationProfileRecord.serverCatalogFallback
                .first { $0.id == draft.observationProfile }?.displayName == "Linux 只读采集"
        )
        #expect(throws: (any Error).self) { try EndpointDraft(
                host: "",
                port: 0,
                sshUser: "collector",
                workspacePath: "relative/path",
                observationProfile: "linux",
                suppliedID: "bad id"
            ) }
    }

    @Test func testURLSessionClientFetchesUnifiedStateRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = URLSessionBrokerSnapshotClient(
            baseURL: URL(string: "http://broker.test/")!,
            session: session
        )
        let fixtureData = try Data(contentsOf: Self.fixturesRoot.appendingPathComponent("1.json"))
        StateRouteURLProtocol.responseData = fixtureData
        defer { StateRouteURLProtocol.reset() }

        let snapshot = try await client.snapshot(actorID: "tester")

        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/state")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor") == "tester")
        #expect(snapshot.snapshotRevision == 101)
        #expect(snapshot.summary.totalGPUs == 1)
    }

    @Test func testURLSessionEndpointTelemetryHistoryClientUsesSeparateOptionalRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let client = URLSessionEndpointTelemetryHistoryClient(
            baseURL: URL(string: "http://broker.test/")!,
            session: session
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "schema_version": "v1",
            "server_time": "2026-08-06T08:10:00Z",
            "data": [
                "endpoint_id": "gpu-node-01",
                "window_seconds": 3600,
                "point_count": 1,
                "points": [
                    [
                        "observed_at": "2026-08-06T08:00:00Z",
                        "cpu_count": 64,
                        "load_1m": 16,
                        "cpu_utilization_pct": 25,
                        "memory_total_mib": 100,
                        "memory_available_mib": 60,
                        "memory_used_pct": 40
                    ]
                ],
                "gpu_series": [
                    [
                        "gpu_id": "gpu-node-01:GPU-uuid-0",
                        "gpu_uuid": "GPU-uuid-0",
                        "gpu_index": 0,
                        "label": "GPU 0",
                        "points": [
                            [
                                "observed_at": "2026-08-06T08:00:00Z",
                                "gpu_utilization_pct": 80,
                                "memory_used_pct": 25,
                                "memory_used_mib": 20000,
                                "memory_total_mib": 80000
                            ]
                        ]
                    ]
                ]
            ]
        ])
        defer { StateRouteURLProtocol.reset() }

        let history = try await client.history(endpointID: "gpu-node-01", range: .oneHour, actorID: "tester")

        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints/gpu-node-01/history")
        #expect(StateRouteURLProtocol.lastRequest?.url?.query == "window_seconds=3600&points=120")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor") == "tester")
        #expect(history.endpointID == "gpu-node-01")
        #expect(history.range == .oneHour)
        #expect(history.samples.first?.cpuLoadFraction == 0.25)
        #expect(history.samples.first?.memoryFraction == 0.40)
        #expect(history.samples.first?.gpuUtilizationFraction == nil)
        #expect(history.gpuSeries.count == 1)
        #expect(history.gpuSeries.first?.id == "gpu-node-01:GPU-uuid-0")
        #expect(history.gpuSeries.first?.samples.first?.gpuUtilizationFraction == 0.8)
        #expect(history.gpuSeries.first?.samples.first?.memoryFraction == 0.25)
    }

    @Test func testEndpointCpuLoadUsesUtilizationAndDoesNotFallBackToHostLoad() throws {
        let utilized = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "monitor": ["status": "ONLINE"],
            "host_telemetry": [
                "cpu_count": 128,
                "load_1m": 380,
                "cpu_utilization_pct": 25
            ]
        ]))
        #expect(utilized.cpuLoadFraction == 0.25)

        let loadOnly = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "monitor": ["status": "ONLINE"],
            "host_telemetry": [
                "cpu_count": 128,
                "load_1m": 380
            ]
        ]))
        #expect(loadOnly.cpuLoadFraction != 1.0)
        #expect(loadOnly.cpuLoadFraction == nil)

        let utilizedSample = try #require(EndpointTelemetrySample(raw: [
            "observed_at": "2026-08-19T06:00:00Z",
            "cpu_count": 128,
            "load_1m": 380,
            "cpu_utilization_pct": 25
        ]))
        #expect(utilizedSample.cpuLoadFraction == 0.25)

        let loadOnlySample = try #require(EndpointTelemetrySample(raw: [
            "observed_at": "2026-08-19T06:00:00Z",
            "cpu_count": 128,
            "load_1m": 380
        ]))
        #expect(loadOnlySample.cpuLoadFraction != 1.0)
        #expect(loadOnlySample.cpuLoadFraction == nil)
    }

    @Test func testEndpointReadsTheContainerBudgetAndNotTheWholeMachine() throws {
        // The node has 128 cores and 1 TB; this endpoint owns 60 cores and 480 GB
        // of it, and only the broker-resolved capacity may reach the surface.
        let container = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "monitor": ["status": "ONLINE"],
            "host_telemetry": [
                "cpu_count": 128,
                "load_1m": 48.41,
                "memory_total_mib": 1_029_120,
                "memory_available_mib": 921_600,
                "capacity": [
                    "cpu_scope": "container",
                    "cpu_cores": 60.0,
                    "cpu_available_cores": 12.36,
                    "memory_scope": "container",
                    "memory_total_mib": 491_520,
                    "memory_used_mib": 132_337,
                    "memory_available_mib": 359_183
                ]
            ]
        ]))
        #expect(container.cpuCores == 60.0)
        #expect(container.availableCPUCores == 12.36)
        #expect(container.memoryTotalMiB == 491_520)
        #expect(container.memoryAvailableMiB == 359_183)
        #expect(container.cpuScopeNote == "容器配额")
        #expect(container.memoryScopeNote == "容器配额")
        #expect(abs((container.memoryFraction!) - (132_337.0 / 491_520.0)) < 0.0001)

        let wholeMachine = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "monitor": ["status": "ONLINE"],
            "host_telemetry": [
                "cpu_count": 64,
                "load_1m": 4.0,
                "memory_total_mib": 1_029_120,
                "memory_available_mib": 921_600,
                "capacity": [
                    "cpu_scope": "host",
                    "cpu_cores": 64.0,
                    "cpu_available_cores": 60.0,
                    "memory_scope": "host",
                    "memory_total_mib": 1_029_120,
                    "memory_used_mib": 107_520,
                    "memory_available_mib": 921_600
                ]
            ]
        ]))
        #expect(wholeMachine.availableCPUCores == 60.0)
        #expect(wholeMachine.cpuScopeNote == nil)
        #expect(abs((wholeMachine.memoryFraction!) - (1 - 921_600.0 / 1_029_120.0)) < 0.0001)

        let noTelemetry = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "monitor": ["status": "ONLINE"]
        ]))
        #expect(noTelemetry.cpuCores == nil)
        #expect(noTelemetry.availableCPUCores == nil)
        #expect(noTelemetry.memoryFraction == nil)
    }

    @Test func testEndpointTelemetryHistoryCapabilityGateDegradesWithoutChangingSnapshot() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        let snapshot = try Self.snapshot(named: "1")

        store.useFixture(snapshot: snapshot)
        store.requestEndpointTelemetryHistory(endpointID: "fixture-1", range: .oneHour)

        #expect(store.snapshot == snapshot)
        #expect(store.endpointTelemetryHistoryErrors["fixture-1"] == "当前本机服务未提供资源历史能力。")
        #expect(!(store.endpointTelemetryHistoryLoading.contains("fixture-1")))
    }

    @Test func testEndpointTelemetryHistoryCancelsOlderRequestAndIgnoresOutOfOrderCompletion() async throws {
        let endpointID = "fixture-1"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = DelayedEndpointHistoryClient(
            results: [
                .success(.init(endpointID: endpointID, range: .twentyFourHours, samples: [
                    EndpointTelemetrySample(raw: [
                        "timestamp": "2026-08-06T06:00:00Z",
                        "gpu_utilization_pct": 10
                    ])!
                ], generatedAt: nil)),
                .success(.init(endpointID: endpointID, range: .oneHour, samples: [
                    EndpointTelemetrySample(raw: [
                        "timestamp": "2026-08-06T08:00:00Z",
                        "gpu_utilization_pct": 80
                    ])!
                ], generatedAt: nil))
            ],
            delaysNanoseconds: [180_000_000, 20_000_000]
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["instant_claims", "endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .twentyFourHours)
        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)

        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour)?.samples.first?.gpuUtilizationFraction == 0.8
                && !store.endpointTelemetryHistoryLoading.contains(endpointID)
        }
        try await Task.sleep(nanoseconds: 220_000_000)

        #expect(store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour)?.samples.first?.gpuUtilizationFraction == 0.8)
        #expect(store.endpointTelemetryHistory(endpointID: endpointID, range: .twentyFourHours) == nil)
    }

    @Test func testEndpointTelemetryHistoryReusesFreshCachedRange() async throws {
        let endpointID = "fixture-1"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = CountingEndpointHistoryClient()
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)
        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: endpointID, range: .oneHour) != nil
                && !store.endpointTelemetryHistoryLoading.contains(endpointID)
        }
        store.requestEndpointTelemetryHistory(endpointID: endpointID, range: .oneHour)

        let historyCallCount = await historyClient.callCount()
        #expect(historyCallCount == 1)
        #expect(store.endpointTelemetryHistoryErrors[endpointID] == nil)
    }

    @Test func testEndpointTelemetryHistoryCancelsInactiveDetailRequest() async throws {
        let firstEndpointID = "fixture-1"
        let secondEndpointID = "fixture-2"
        let snapshotClient = ScriptedClient(results: [.success(try Self.snapshot(named: "1"))])
        let historyClient = CooperativeDelayedEndpointHistoryClient(
            delaysNanoseconds: [500_000_000, 5_000_000]
        )
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: snapshotClient,
            endpointTelemetryHistoryClient: historyClient,
            serviceInfo: ServiceInfo(
                schemaVersion: "v1",
                version: "test",
                capabilities: ["endpoint_telemetry_history"]
            )
        )

        store.requestEndpointTelemetryHistory(endpointID: firstEndpointID, range: .oneHour)
        try await waitUntilAsync { await historyClient.metrics().callCount == 1 }
        #expect(store.endpointTelemetryHistoryLoading.contains(firstEndpointID))

        store.requestEndpointTelemetryHistory(endpointID: secondEndpointID, range: .oneHour)
        try await waitUntil {
            store.endpointTelemetryHistory(endpointID: secondEndpointID, range: .oneHour) != nil
                && !store.endpointTelemetryHistoryLoading.contains(secondEndpointID)
        }
        try await waitUntilAsync { await historyClient.metrics().cancellationCount == 1 }

        #expect(!(store.endpointTelemetryHistoryLoading.contains(firstEndpointID)))
        #expect(store.endpointTelemetryHistory(endpointID: firstEndpointID, range: .oneHour) == nil)
        #expect(store.endpointTelemetryHistoryErrors[firstEndpointID] == nil)
    }

    @Test func testUnifiedStateEnvelopeParsesCurrentAndHistory() throws {
        let history = [
            "summary": ["total_gpus": 1],
            "data_age_seconds": 8,
            "freshness_seconds": 30
        ] as [String: Any]
        let snapshot = BrokerSnapshot(envelope: [
            "schema_version": "v1",
            "snapshot_revision": 150,
            "server_time": "2026-08-06T08:00:00Z",
            "data": [
                "current": [
                    "summary": ["total_gpus": 8],
                    "endpoints": [],
                    "gpus": [],
                    "leases": [],
                    "requests": [],
                    "reservations": [],
                    "data_age_seconds": 2,
                    "freshness_seconds": 30,
                    "admission_boundary": "test"
                ],
                "history": [
                    "summary_samples": [
                        history
                    ]
                ]
            ]
        ])

        #expect(snapshot.snapshotRevision == 150)
        #expect(snapshot.summary.totalGPUs == 8)
        #expect(snapshot.history == .empty)
        #expect(snapshot.dataAgeSeconds == 2)
    }

    @Test func testMutationRevisionFloorRejectsRollbackSnapshotAndLaterCommitsRequiredRevision() async throws {
        var beforeMutation = try Self.snapshot(named: "1")
        beforeMutation.snapshotRevision = 101
        var rollback = try Self.snapshot(named: "8")
        rollback.snapshotRevision = 120
        var afterMutation = try Self.snapshot(named: "8")
        afterMutation.snapshotRevision = 150
        let client = ScriptedClient(results: [
            .success(beforeMutation),
            .success(rollback),
            .success(afterMutation)
        ])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == 101 && !store.isRefreshing }

        let mutationResponse = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 150])
        store.raiseMinimumRequiredSnapshotRevision(from: mutationResponse)
        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }

        #expect(store.snapshot.snapshotRevision == 101)
        #expect(store.lastGoodSnapshot?.snapshotRevision == 101)
        #expect(store.errorMessage == "无法更新资源：\(BrokerRefreshError.snapshotRevisionBehind(required: 150, received: 120).localizedDescription)")

        store.reload()
        try await waitUntil { store.snapshot.snapshotRevision == 150 && !store.isRefreshing }

        #expect(store.freshness == .fresh)
        #expect(store.errorMessage == nil)
    }

    @Test func testOrdinaryRefreshRejectsRevisionRollbackAndPreservesLastGoodSnapshot() async throws {
        var accepted = try Self.snapshot(named: "8")
        accepted.snapshotRevision = 150
        var rollback = try Self.snapshot(named: "1")
        rollback.snapshotRevision = 120
        let client = ScriptedClient(results: [.success(accepted), .success(rollback)])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(snapshotClient: client)
        try await waitUntil { store.snapshot.snapshotRevision == 150 && !store.isRefreshing }

        store.reload()
        try await waitUntil { store.freshness == .stale && !store.isRefreshing }

        #expect(store.snapshot.snapshotRevision == 150)
        #expect(store.lastGoodSnapshot?.snapshotRevision == 150)
        #expect(store.errorMessage == "无法更新资源：\(BrokerRefreshError.snapshotRevisionBehind(required: 150, received: 120).localizedDescription)")
    }

    @Test func testEndpointLifecycleActionsUseDistinctDocumentedRoutes() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        var snapshot = try Self.snapshot(named: "1")
        // Mutations raise a snapshot_revision floor from their response. A
        // repeating snapshot that stays behind that floor fail-closes the
        // store, so later mutations never reach the network.
        snapshot.snapshotRevision = 102
        let endpoint = try #require(snapshot.endpoints.first)
        let capabilities: Set<String> = ["endpoint_update", "endpoint_delete", "endpoint_keepalive", "endpoint_conflict_cleanup"]
        let serviceInfo = ServiceInfo(schemaVersion: "v1", capabilities: capabilities)
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 102])
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: RepeatingClient(snapshot),
            serviceInfo: serviceInfo,
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let updateRecorder = CompletionRecorder()
        store.updateEndpoint(
            endpoint,
            draft: try EndpointUpdateDraft(
                sshUser: "collector",
                workspacePath: "/srv/storyboard",
                observationProfile: "linux"
            )
        ) { success, message in
            updateRecorder.success = success
            updateRecorder.message = message
        }
        try await waitUntil { updateRecorder.success != nil }
        #expect(updateRecorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "PATCH")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints/fixture-1")
        let updateBody = try #require(StateRouteURLProtocol.lastBody)
        let updatePayload = try #require(try JSONSerialization.jsonObject(with: updateBody) as? [String: Any])
        #expect(updatePayload["ssh_user"] as? String == "collector")
        #expect(updatePayload["workspace_path"] as? String == "/srv/storyboard")
        #expect(updatePayload["observation_profile"] as? String == "linux")

        let keepaliveRecorder = CompletionRecorder()
        store.setEndpointKeepalive(endpoint, enabled: true) { success, message in
            keepaliveRecorder.success = success
            keepaliveRecorder.message = message
        }
        try await waitUntil { keepaliveRecorder.success != nil }
        #expect(keepaliveRecorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints/fixture-1/keepalive")
        let keepaliveBody = try #require(StateRouteURLProtocol.lastBody)
        let keepalivePayload = try #require(try JSONSerialization.jsonObject(with: keepaliveBody) as? [String: Any])
        #expect(keepalivePayload["enabled"] as? Bool == true)
        #expect(Set(keepalivePayload.keys) == Set(["enabled"]))

        let conflictRecorder = CompletionRecorder()
        store.clearEmptyConflictedLease(
            endpointID: endpoint.id,
            leaseID: "lease-conflict"
        ) { success, message in
            conflictRecorder.success = success
            conflictRecorder.message = message
        }
        try await waitUntil { conflictRecorder.success != nil }
        #expect(conflictRecorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints/fixture-1/leases/lease-conflict/release-empty")

        let deleteRecorder = CompletionRecorder()
        store.deleteEndpoint(endpoint) { success, message in
            deleteRecorder.success = success
            deleteRecorder.message = message
        }
        try await waitUntil { deleteRecorder.success != nil }
        #expect(deleteRecorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "DELETE")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints/fixture-1")

    }

    @Test func testHumanTaskGPUReassignmentUsesExactPatchRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let lease = try #require(LeaseRecord(raw: [
            "id": "lease-manual-move",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-old"],
        ]))
        let store = BrokerStore(
            actorID: "human",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(
            withJSONObject: ["snapshot_revision": 103]
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: RepeatingClient(snapshot),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: []),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.reassignLease(lease, gpuIDs: ["fixture-1:GPU-new"]) { success, message in
            recorder.success = success
            recorder.message = message
        }

        try await waitUntil { recorder.success != nil }
        #expect(recorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "PATCH")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/operator/leases/lease-manual-move/gpus")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Client") == "desktop-app")
        let body = try #require(StateRouteURLProtocol.lastBody)
        let payload = try #require(try JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(payload["gpu_ids"] as? [String] == ["fixture-1:GPU-new"])
        #expect(store.notice?.contains("对应 Agent") == true)
    }

    @Test func testHumanReleaseUsesOperatorCorrectionRoute() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let lease = try #require(LeaseRecord(raw: [
            "id": "lease-owned-by-agent",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-old"],
        ]))
        let store = BrokerStore(
            actorID: "human",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.responseData = try JSONSerialization.data(
            withJSONObject: ["snapshot_revision": 104]
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: []),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.releaseLease(lease) { success, message in
            recorder.success = success
            recorder.message = message
        }

        try await waitUntil { recorder.success != nil }
        #expect(recorder.success == true)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/operator/leases/lease-owned-by-agent/release")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor") == "human")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Client") == "desktop-app")
    }

    @Test func testCollectorIntervalReadsAndUpdatesServerSetting() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: session
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot), .success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["collector_settings"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "data": [
                "interval_seconds": 10,
                "stale_after_seconds": 30,
                "allowed_intervals": [5, 10, 30],
            ]
        ])
        store.requestCollectorSettings()
        try await waitUntil { store.collectorSettings?.intervalSeconds == 10 }
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "GET")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/settings/collector")

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "snapshot_revision": 102,
            "settings": [
                "interval_seconds": 5,
                "stale_after_seconds": 15,
                "allowed_intervals": [5, 10, 30],
            ]
        ])
        let recorder = CompletionRecorder()
        store.updateCollectorInterval(5) { success, message in
            recorder.success = success
            recorder.message = message
        }
        try await waitUntil { recorder.success != nil }
        #expect(recorder.success == true)
        #expect(store.collectorSettings?.intervalSeconds == 5)
        #expect(store.collectorSettings?.staleAfterSeconds == 15)
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "PATCH")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/settings/collector")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "Idempotency-Key") != nil)
    }

    @Test func testCollectorIntervalRemainsVisibleButReadOnlyInFixtureMode() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.useFixture(snapshot: try Self.snapshot(named: "1"))

        #expect(store.supportsCollectorSettings)
        #expect(!(store.canUpdateCollectorSettings))
        #expect(store.collectorSettings?.intervalSeconds == 10)
        #expect(store.collectorSettings?.allowedIntervals == [5, 10, 30])
    }

    @Test func testMCPEntryRecordAcceptsResolvedEntryAndRejectsContradictoryPayload() throws {
        let available = try #require(MCPEntryRecord(raw: [
            "available": true,
            "command": "/opt/serverpilot/bin/serverpilot-mcp",
            "mcpServers": [
                "serverpilot": [
                    "command": "/opt/serverpilot/bin/serverpilot-mcp",
                    "env": ["SERVERPILOT_URL": "http://127.0.0.1:8787"],
                ]
            ],
        ]))
        #expect(available.available)
        #expect(available.command == "/opt/serverpilot/bin/serverpilot-mcp")
        #expect(available.configJSON != nil)
        #expect(available.configJSON?.contains("\"mcpServers\"") == true)
        #expect(available.configJSON?.contains("/opt/serverpilot/bin/serverpilot-mcp") == true)
        #expect(available.configJSON?.contains("SERVERPILOT_URL") == true)

        #expect(MCPEntryRecord(raw: [
            "available": true,
            "command": "/opt/serverpilot/bin/serverpilot-mcp",
        ]) == nil)
        #expect(MCPEntryRecord(raw: [
            "available": false,
            "command": "/opt/serverpilot/bin/serverpilot-mcp",
            "mcpServers": NSNull(),
            "hint": "cannot find serverpilot-mcp",
        ]) == nil)
    }

    @Test func testMCPEntryReadsAbsolutePathAndPasteableConfig() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: session
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["mcp_entry"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "data": [
                "available": true,
                "command": "/opt/serverpilot/bin/serverpilot-mcp",
                "mcpServers": [
                    "serverpilot": [
                        "command": "/opt/serverpilot/bin/serverpilot-mcp",
                        "env": ["SERVERPILOT_URL": "http://127.0.0.1:8787"],
                    ]
                ],
                "hint": NSNull(),
            ]
        ])
        store.requestMcpEntry()
        try await waitUntil { store.mcpEntry?.available == true }
        #expect(StateRouteURLProtocol.lastRequest?.httpMethod == "GET")
        #expect(StateRouteURLProtocol.lastRequest?.url?.path == "/api/v1/mcp-entry")
        #expect(StateRouteURLProtocol.lastRequest?.value(forHTTPHeaderField: "X-ServerPilot-Actor") == "tester")
        #expect(store.mcpEntry?.command == "/opt/serverpilot/bin/serverpilot-mcp")
        #expect(store.mcpEntry?.configJSON?.contains("\"mcpServers\"") == true)
    }

    @Test func testMCPEntryUnavailablePayloadDoesNotInventAPath() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let session = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: session
        )
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["mcp_entry"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "data": [
                "available": false,
                "command": NSNull(),
                "mcpServers": NSNull(),
                "hint": "cannot find serverpilot-mcp; install this project with `uv tool install --force .` or use the packaged desktop archive",
            ]
        ])
        store.requestMcpEntry()
        try await waitUntil { store.mcpEntry != nil }
        #expect(store.mcpEntry?.available == false)
        #expect(store.mcpEntry?.command == nil)
        #expect(store.mcpEntry?.configJSON == nil)
        #expect(store.mcpEntry?.hint?.contains("uv tool install") == true)
    }

    @Test func testMCPEntryStaysHiddenInFixtureMode() throws {
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.useFixture(snapshot: try Self.snapshot(named: "1"))

        #expect(!(store.supportsMcpEntry))
        #expect(store.mcpEntry == nil)
    }

    @Test func testEndpointAndControlPlaneMutationsStayAvailableWhenOneEndpointHasOldTelemetry() async throws {
        let client = ScriptedClient(results: [.success(try Self.snapshot(named: "stale"))])
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)

        store.connectForTesting(
            snapshotClient: client,
            baseURL: URL(string: "http://127.0.0.1:8787/")
        )

        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }
        #expect(store.allowsMutations)
        #expect(store.allowsEndpointLifecycleMutations)
    }

    @Test func testEndpointLifecycleCapabilityGateAndErrorParsingRemainSafe() throws {
        let payload = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 250])
        #expect(BrokerStore.snapshotRevision(from: payload) == 250)

        let errorPayload = try JSONSerialization.data(withJSONObject: [
            "error": ["code": "endpoint_not_found"]
        ])
        #expect(BrokerStore.apiErrorCode(from: errorPayload) == "endpoint_not_found")

        let advertised = ServiceInfo(schemaVersion: "v1", capabilities: ["endpoint_update"])
        #expect(advertised.supportsEndpointUpdate)
        #expect(!(advertised.supportsEndpointDelete))

        let fixture = ServiceInfo.fixture
        #expect(fixture.supportsEndpointDelete)
    }

    @Test func testDeleteEndpointKeepsActionableConflictError() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StateRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        let snapshot = try Self.snapshot(named: "1")
        let endpoint = try #require(snapshot.endpoints.first)
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        StateRouteURLProtocol.statusCode = 409
        StateRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "error": [
                "code": "endpoint_has_active_allocations",
                "message": "这台服务器上还有进行中的资源分配，请先结束后再删除。",
            ]
        ])
        defer { StateRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["endpoint_delete"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.deleteEndpoint(endpoint) { success, message in
            recorder.success = success
            recorder.message = message
        }
        try await waitUntil { recorder.success != nil }
        #expect(recorder.success == false)
        #expect(recorder.message?.contains("先结束") == true)
        #expect(store.snapshot.endpoints.contains(where: { $0.id == endpoint.id }))
    }

    @Test func testDeleteEndpointFailsClosedWithoutCapability() async throws {
        let snapshot = try Self.snapshot(named: "1")
        let endpoint = try #require(snapshot.endpoints.first)
        let store = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        store.connectForTesting(
            snapshotClient: ScriptedClient(results: [.success(snapshot)]),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["endpoint_update"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let recorder = CompletionRecorder()
        store.deleteEndpoint(endpoint) { success, message in
            recorder.success = success
            recorder.message = message
        }
        try await waitUntil { recorder.success != nil }
        #expect(recorder.success == false)
        #expect(recorder.message?.contains("删除服务器") == true)
        #expect(!(store.supportsEndpointDelete))
    }

    @Test func testKeepaliveSnapshotUsesPerGPUCoverageAndRedactsInternalLease() throws {
        let fixture = try Self.snapshot(named: "keepalive")
        let endpoint = try #require(fixture.endpoints.first)
        #expect(endpoint.keepalive.configured)
        #expect(endpoint.keepalive.policy == "idle_keepalive")
        #expect(endpoint.keepalive.state == "ON")
        #expect(endpoint.keepalive.label == "已开启")
        #expect(endpoint.keepalive.hasResidualLease)
        #expect(endpoint.keepalive.coverageSummary(totalGPUCount: 2, taskGPUCount: 1) == "已开启 · 1/2 占卡，1 卡任务中")
        #expect(endpoint.keepalive.coverageSummary(totalGPUCount: 0, taskGPUCount: 0) == "无 GPU")
        #expect(fixture.gpus.map(\.state) == ["KEEPALIVE", "RUNNING_MANAGED"])
        #expect(try #require(fixture.gpus.first).isPubliclyAvailable)
        #expect(fixture.gpus.first?.keepalive.desired == "ON")
        #expect(fixture.gpus.first?.keepalive.presentationLabel == "空闲占卡")
        #expect(fixture.gpus.last?.keepalive.reason == "managed workload is running")
        #expect(fixture.leases.isEmpty)

        let missingKeeper = try #require(GPURecord(raw: [
            "id": "fixture-1:GPU-missing-keeper",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-missing-keeper",
            "gpu_index": 2,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "AVAILABLE",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "OFF",
                "state": "OFF",
                "lease_id": "keepalive-missing-lease"
            ],
        ]))
        #expect(!(missingKeeper.isTaskOccupancy))
        #expect(missingKeeper.isPubliclyAvailable)
        #expect(missingKeeper.keepalive.desired == "ON")
        #expect(missingKeeper.keepalive.presentationLabel == "占卡未运行")
        #expect(missingKeeper.keepalive.leaseID == "keepalive-missing-lease")
        #expect(try #require(fixture.gpus.last).isTaskOccupancy)

        let conflictedKeeper = try #require(GPURecord(raw: [
            "id": "fixture-1:GPU-conflicted-keeper",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-conflicted-keeper",
            "gpu_index": 3,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "CONFLICT",
            "publicly_available": false,
            "public_status": "任务占用",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "ERROR",
                "reason": "占卡进程身份尚未建立",
                "lease_id": "keepalive-conflicted-lease"
            ],
        ]))
        #expect(conflictedKeeper.publicStatus == "任务占用")
        #expect(conflictedKeeper.projectedPubliclyAvailable == false)
        #expect(!(conflictedKeeper.isPubliclyAvailable))
        #expect(conflictedKeeper.keepalive.desired == "ON")
        #expect(conflictedKeeper.keepalive.state == "ERROR")

        #expect(GPURecord(raw: [
            "id": "fixture-1:GPU-invalid-public-projection",
            "endpoint_id": "fixture-1",
            "gpu_uuid": "GPU-invalid-public-projection",
            "gpu_index": 4,
            "name": "Fixture GPU",
            "total_vram_mib": 81920,
            "state": "CONFLICT",
            "publicly_available": false,
            "public_status": "可用 · 占卡异常",
            "keepalive": [
                "configured": true,
                "policy": "idle_keepalive",
                "desired": "ON",
                "actual": "ERROR"
            ],
        ]) == nil)

        let defensiveSnapshot = BrokerSnapshot(envelope: [
            "data": [
                "summary": [:],
                "leases": [[
                    "id": "internal-keepalive",
                    "actor_id": "__serverpilot_system__",
                    "project_id": "__serverpilot_system__",
                    "kind": "keepalive",
                    "state": "ACTIVE",
                    "runtime_state": "RUNNING",
                    "gpu_ids": []
                ]]
            ]
        ])
        #expect(defensiveSnapshot.leases.isEmpty)
    }

    @Test func testKeepaliveProtocolRejectsUnknownPolicyAndState() {
        #expect(EndpointKeepaliveSummary(
            raw: ["configured": true, "policy": "unknown", "state": "OFF"],
            fallbackConfigured: true
        ) == nil)
        #expect(EndpointKeepaliveSummary(
            raw: ["configured": true, "policy": "disabled", "state": "UNKNOWN"],
            fallbackConfigured: true
        ) == nil)
        #expect(GPUKeepaliveStatus(
            raw: ["policy": "unknown", "state": "OFF"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ) == nil)
        #expect(GPUKeepaliveStatus(
            raw: ["policy": "disabled", "state": "UNKNOWN"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ) == nil)
        #expect(GPUKeepaliveStatus(
            raw: ["policy": "idle_keepalive", "desired": "UNKNOWN", "state": "OFF"],
            fallbackConfigured: true,
            fallbackState: "OFF"
        ) == nil)
    }

    @Test func testStableSelectionFallsBackToFirstAvailableRecord() throws {
        let snapshot = try Self.snapshot(named: "queued")

        #expect(snapshot.stableEndpointSelection(currentID: "missing") == "fixture-queued")
        #expect(snapshot.stableEndpointSelection(currentID: "fixture-queued") == "fixture-queued")
        #expect(snapshot.stableRequestSelection(currentID: "missing") == "request-fixture-queued")
        #expect(BrokerSnapshot.empty.stableEndpointSelection(currentID: "missing") == "")
    }

    @Test func testFixturesResolveInsideDesktopFixturesAndRejectProjectState() throws {
        let fixturesRoot = Self.fixturesRoot
        let projectRoot = fixturesRoot.deletingLastPathComponent().deletingLastPathComponent()

        let fixtureURL = try FixtureSnapshots.resolve("64", fixturesRoot: fixturesRoot, projectRoot: projectRoot)
        #expect(try FixtureSnapshots.load(from: fixtureURL).summary.totalGPUs == 64)

        let stateURL = projectRoot.appendingPathComponent("state/live.json").path
        let rejected = #expect(throws: FixtureSnapshotError.self) {
            try FixtureSnapshots.resolve(
                stateURL,
                fixturesRoot: fixturesRoot,
                projectRoot: projectRoot
            )
        }
        #expect(
            rejected == .rejectedProductionState(URL(fileURLWithPath: stateURL).standardizedFileURL)
        )
    }

    @Test func testEndpointHistoryFixtureIsSeparateFromAllocationSnapshotFixture() throws {
        let fixturesRoot = Self.fixturesRoot
        let historyURL = try FixtureSnapshots.resolve("8-history", fixturesRoot: fixturesRoot)

        let history = try FixtureSnapshots.loadEndpointTelemetryHistory(from: historyURL)

        #expect(history.endpointID == "fixture-8")
        #expect(history.range == .oneHour)
        #expect(history.samples.count == 6)
        #expect(history.gpuSeries.count == 8)
        #expect(history.gpuSeries.allSatisfy { $0.samples.count == 6 })
    }

    @Test func testFixtureSymlinkIntoProjectStateIsRejected() throws {
        let fileManager = FileManager.default
        let projectRoot = fileManager.temporaryDirectory
            .appendingPathComponent("serverpilot-fixture-test-\(UUID().uuidString)", isDirectory: true)
        let fixtureRoot = projectRoot.appendingPathComponent("desktop/Fixtures", isDirectory: true)
        let stateRoot = projectRoot.appendingPathComponent("state", isDirectory: true)
        defer { try? fileManager.removeItem(at: projectRoot) }

        try fileManager.createDirectory(at: fixtureRoot, withIntermediateDirectories: true)
        try fileManager.createDirectory(at: stateRoot, withIntermediateDirectories: true)
        let stateFixture = stateRoot.appendingPathComponent("live.json")
        try Data(contentsOf: Self.fixturesRoot.appendingPathComponent("1.json")).write(to: stateFixture)
        let fixtureSymlink = fixtureRoot.appendingPathComponent("linked.json")
        try fileManager.createSymbolicLink(at: fixtureSymlink, withDestinationURL: stateFixture)

        let linkedError = #expect(throws: FixtureSnapshotError.self) {
            try FixtureSnapshots.resolve(
                "linked.json",
                fixturesRoot: fixtureRoot,
                projectRoot: projectRoot
            )
        }
        guard case .rejectedProductionState(let rejectedURL) = linkedError else {
            Issue.record("Expected rejectedProductionState, got \(String(describing: linkedError))")
            return
        }
        #expect(rejectedURL == stateFixture.resolvingSymlinksInPath())
    }

    private static var fixturesRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures", isDirectory: true)
    }

    private static func snapshot(named name: String) throws -> BrokerSnapshot {
        let url = try FixtureSnapshots.resolve(name, fixturesRoot: fixturesRoot)
        return try FixtureSnapshots.load(from: url)
    }

    private func waitUntil(
        timeoutNanoseconds: UInt64 = 1_000_000_000,
        _ predicate: @escaping @MainActor () -> Bool
    ) async throws {
        let started = DispatchTime.now().uptimeNanoseconds
        while !predicate() {
            if DispatchTime.now().uptimeNanoseconds - started > timeoutNanoseconds {
                Issue.record("Timed out waiting for condition")
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }

    private func waitUntilAsync(
        timeoutNanoseconds: UInt64 = 1_000_000_000,
        _ predicate: @escaping () async -> Bool
    ) async throws {
        let started = DispatchTime.now().uptimeNanoseconds
        while !(await predicate()) {
            if DispatchTime.now().uptimeNanoseconds - started > timeoutNanoseconds {
                Issue.record("Timed out waiting for asynchronous condition")
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }

    @Test func testLeaseRecordCarriesWhenItLastRanSomething() throws {
        // Whoever is about to clear an "idle" lease reads this to tell a burst
        // gap from an ending. A lease that has never run anything decodes to
        // nil rather than to a fabricated time, so the dialog says so plainly.
        let ran = try #require(LeaseRecord(raw: [
            "id": "lease-with-history",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "ACTIVE",
            "gpu_ids": ["fixture-1:GPU-a"],
            "last_process_observed_at": "2026-07-31T23:59:52Z",
        ]))
        #expect(ran.lastProcessObservedAt == "2026-07-31T23:59:52Z")

        let neverRan = try #require(LeaseRecord(raw: [
            "id": "lease-never-ran",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-b"],
        ]))
        #expect(neverRan.lastProcessObservedAt == nil)
    }

    @Test func testLeaseWithoutManualReleaseFieldIsNotReleasable() throws {
        // Whether a lease may be cleared by hand is the broker's answer alone.
        // A payload that does not carry it has not said yes, and there is no
        // local second opinion: inferring releasability from GPU states is
        // what invited a person to clear a claim that was still working.
        let silent = try #require(LeaseRecord(raw: [
            "id": "lease-silent",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-a"],
        ]))
        #expect(silent.manualRelease.allowed == false)
        #expect(silent.manualRelease.blockedReason == nil)

        let allowed = try #require(LeaseRecord(raw: [
            "id": "lease-allowed",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "HELD",
            "gpu_ids": ["fixture-1:GPU-b"],
            "manual_release": ["allowed": true],
        ]))
        #expect(allowed.manualRelease.allowed)
        #expect(allowed.manualRelease.blockedReason == nil)

        let refused = try #require(LeaseRecord(raw: [
            "id": "lease-refused",
            "actor_id": "agent-a",
            "project_id": "project-a",
            "kind": "workload",
            "state": "ACTIVE",
            "gpu_ids": ["fixture-1:GPU-c"],
            "manual_release": [
                "allowed": false,
                "blocked_reason": "lease_holder_recently_alive",
                "message": "这个租约的持有者刚刚还在。",
            ],
        ]))
        #expect(refused.manualRelease.allowed == false)
        #expect(refused.manualRelease.blockedReason == "lease_holder_recently_alive")
        #expect(refused.manualRelease.message == "这个租约的持有者刚刚还在。")
    }
}

@MainActor
private final class CompletionRecorder {
    var success: Bool?
    var message: String?
}

private actor DelayedSequenceClient: BrokerSnapshotClient {
    struct Metrics: Sendable {
        let callCount: Int
        let maxConcurrentCalls: Int
        let activeCalls: Int
    }

    private let snapshots: [BrokerSnapshot]
    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0
    private var activeCalls = 0
    private var callCount = 0
    private var maxConcurrentCalls = 0

    init(snapshots: [BrokerSnapshot], delayNanoseconds: UInt64) {
        self.snapshots = snapshots
        self.delaysNanoseconds = Array(repeating: delayNanoseconds, count: snapshots.count)
    }

    init(snapshots: [BrokerSnapshot], delaysNanoseconds: [UInt64]) {
        self.snapshots = snapshots
        self.delaysNanoseconds = delaysNanoseconds
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        callCount += 1
        activeCalls += 1
        maxConcurrentCalls = max(maxConcurrentCalls, activeCalls)
        let index = min(nextIndex, snapshots.count - 1)
        nextIndex += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            activeCalls -= 1
            throw error
        }
        activeCalls -= 1
        return snapshots[index]
    }

    func metrics() -> Metrics {
        Metrics(callCount: callCount, maxConcurrentCalls: maxConcurrentCalls, activeCalls: activeCalls)
    }
}

private actor CancellationIgnoringClient: BrokerSnapshotClient {
    private let snapshotValue: BrokerSnapshot
    private let delayNanoseconds: UInt64
    private var callCount = 0

    init(snapshot: BrokerSnapshot, delayNanoseconds: UInt64) {
        self.snapshotValue = snapshot
        self.delayNanoseconds = delayNanoseconds
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        callCount += 1
        do {
            try await Task.sleep(nanoseconds: delayNanoseconds)
        } catch {
            try? await Task.sleep(nanoseconds: delayNanoseconds)
        }
        return snapshotValue
    }

    func metrics() -> Int { callCount }
}

/// Answers every refresh with the same snapshot.
///
/// A test that walks several mutations cannot use a finite script: the store
/// reloads after each one, and with `refreshIntervalSeconds: 0` the poller
/// drains the script, after which failing refreshes disconnect the store and
/// the next mutation is refused before it ever reaches the network.
/// The repeating snapshot's revision must also meet the mutation response's
/// `snapshot_revision` floor; a behind snapshot fail-closes the same way.
private actor RepeatingClient: BrokerSnapshotClient {
    private let stored: BrokerSnapshot

    init(_ stored: BrokerSnapshot) {
        self.stored = stored
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        stored
    }
}

private actor ScriptedClient: BrokerSnapshotClient {
    private var results: [Result<BrokerSnapshot, BrokerRefreshError>]

    init(results: [Result<BrokerSnapshot, BrokerRefreshError>]) {
        self.results = results
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        let result = results.isEmpty ? .failure(BrokerRefreshError.invalidSnapshot) : results.removeFirst()
        return try result.get()
    }
}

private actor DelayedEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    private var results: [Result<EndpointTelemetryHistory, BrokerRefreshError>]
    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0

    init(results: [Result<EndpointTelemetryHistory, BrokerRefreshError>], delaysNanoseconds: [UInt64]) {
        self.results = results
        self.delaysNanoseconds = delaysNanoseconds
    }

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        let index = nextIndex
        nextIndex += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        let result = results.isEmpty
            ? Result<EndpointTelemetryHistory, BrokerRefreshError>.failure(BrokerRefreshError.invalidSnapshot)
            : results[min(index, results.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            try? await Task.sleep(nanoseconds: delay)
        }
        return try result.get()
    }
}

private actor CountingEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    private var calls = 0

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        calls += 1
        return .empty(endpointID: endpointID, range: range)
    }

    func callCount() -> Int { calls }
}

private actor CooperativeDelayedEndpointHistoryClient: BrokerEndpointTelemetryHistoryClient {
    struct Metrics: Sendable {
        let callCount: Int
        let cancellationCount: Int
    }

    private let delaysNanoseconds: [UInt64]
    private var nextIndex = 0
    private var calls = 0
    private var cancellations = 0

    init(delaysNanoseconds: [UInt64]) {
        self.delaysNanoseconds = delaysNanoseconds
    }

    func history(endpointID: String, range: EndpointTelemetryRange, actorID: String) async throws -> EndpointTelemetryHistory {
        let index = nextIndex
        nextIndex += 1
        calls += 1
        let delay = delaysNanoseconds[min(index, delaysNanoseconds.count - 1)]
        do {
            try await Task.sleep(nanoseconds: delay)
        } catch {
            cancellations += 1
            throw error
        }
        return .empty(endpointID: endpointID, range: range)
    }

    func metrics() -> Metrics {
        Metrics(callCount: calls, cancellationCount: cancellations)
    }
}

private final class StateRouteURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var responseData: Data?
    nonisolated(unsafe) static var lastRequest: URLRequest?
    nonisolated(unsafe) static var lastBody: Data?
    nonisolated(unsafe) static var statusCode = 200

    /// URLSession turns `httpBody` into a stream before a protocol sees the
    /// request, so the body has to be drained here or the test observes nothing.
    private static func drain(_ stream: InputStream?) -> Data? {
        guard let stream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data.isEmpty ? nil : data
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.lastRequest = request
        Self.lastBody = request.httpBody ?? Self.drain(request.httpBodyStream)
        let data = Self.responseData ?? Data("{}".utf8)
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: Self.statusCode,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    static func reset() {
        responseData = nil
        lastRequest = nil
        lastBody = nil
        statusCode = 200
    }
}

@MainActor
@Suite struct RegistrationOutcomeTests {
    @Test func testReachableHostReportsTheGPUsItFound() {
        let outcome = BrokerStore.registrationOutcome(
            id: "server-a",
            payload: ["observation": ["observed": true, "gpu_count": 8, "error": NSNull()]]
        )

        #expect(outcome.reachable)
        #expect(outcome.message.contains("8"))
    }

    @Test func testUnreachableHostReportsWhyItDidNotConnect() {
        // The registration succeeded and the row exists; what the operator has
        // to be told is that the machine never answered, and what said so.
        let outcome = BrokerStore.registrationOutcome(
            id: "server-b",
            payload: [
                "observation": [
                    "observed": false,
                    "gpu_count": 0,
                    "error": "CollectionError: Host key verification failed.",
                ]
            ]
        )

        #expect(!outcome.reachable)
        #expect(outcome.message.contains("Host key verification failed"))
    }

    @Test func testConnectedHostWithoutGPUsPointsAtTheObservationProfile() {
        let outcome = BrokerStore.registrationOutcome(
            id: "server-c",
            payload: ["observation": ["observed": true, "gpu_count": 0]]
        )

        #expect(outcome.reachable)
        #expect(outcome.message.contains("观测方式"))
    }

    @Test func testRegistrationOutlastsTheCollectionItWaitsFor() {
        // Registration observes the host before answering; a plain mutation is
        // a local write. The app used to give every mutation ten seconds, so
        // adding a server would have timed out while the control plane was
        // still connecting to it.
        #expect(BrokerStore.mutationTimeout(forPath: "api/v1/endpoints") > 45)
        #expect(BrokerStore.mutationTimeout(forPath: "api/v1/endpoints/server-a") == 10)
    }
}
