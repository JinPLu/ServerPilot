import Foundation
import Testing
@testable import ServerPilotCore

@MainActor
@Suite(.serialized) struct ServerGroupTests {
    @Test func testSnapshotDecodesServerGroupsAndEndpointAssignment() throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        #expect(snapshot.serverGroups.map(\.id) == ["lab-a", "lab-b"])
        #expect(snapshot.serverGroup(id: "lab-a")?.displayName == "Lab A")
        #expect(snapshot.serverGroup(id: "lab-a")?.workspacePath == "/srv/lab-a")
        #expect(snapshot.serverGroup(id: "lab-a")?.environmentNotes == "weights under /data/lab-a")
        #expect(snapshot.serverGroup(id: "lab-a")?.description == "shared A100 pool")

        let inherited = try #require(snapshot.endpoint(id: "node-a1"))
        #expect(inherited.serverGroupID == "lab-a")
        #expect(inherited.workspacePath == "/srv/lab-a")
        #expect(inherited.workspacePathOverride == nil)
        #expect(inherited.inheritsGroupWorkspacePath)
        #expect(snapshot.serverGroup(for: inherited)?.id == "lab-a")

        let overridden = try #require(snapshot.endpoint(id: "node-a2"))
        #expect(overridden.serverGroupID == "lab-a")
        #expect(overridden.workspacePath == "/mnt/override-a2")
        #expect(overridden.workspacePathOverride == "/mnt/override-a2")
        #expect(!(overridden.inheritsGroupWorkspacePath))

        let ungrouped = try #require(snapshot.endpoint(id: "solo-1"))
        #expect(ungrouped.serverGroupID == nil)
        #expect(ungrouped.workspacePath == "/srv/solo")
        #expect(ungrouped.workspacePathOverride == nil)
        #expect(!(ungrouped.inheritsGroupWorkspacePath))
    }

    @Test func testGroupingHelpersSeparateMembershipFromUngroupedEndpoints() throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        #expect(snapshot.endpoints(inGroup: "lab-a").map(\.id) == ["node-a1", "node-a2"])
        #expect(snapshot.endpoints(inGroup: "lab-b").map(\.id) == ["node-b1"])
        #expect(snapshot.ungroupedEndpoints.map(\.id) == ["solo-1"])
        #expect(snapshot.endpoints(inGroup: "missing").isEmpty)
    }

    @Test func testMissingServerGroupsKeepsLegacyEndpointsCompatible() throws {
        let snapshot = try Self.fixtureSnapshot(named: "1")
        #expect(snapshot.serverGroups.isEmpty)
        #expect(snapshot.ungroupedEndpoints.map(\.id) == snapshot.endpoints.map(\.id))
        let endpoint = try #require(snapshot.endpoints.first)
        #expect(endpoint.serverGroupID == nil)
        #expect(endpoint.workspacePathOverride == nil)
        #expect(endpoint.workspacePath == "/srv/serverpilot-fixtures")
        #expect(!(endpoint.inheritsGroupWorkspacePath))
        #expect(snapshot.serverGroup(for: endpoint) == nil)
    }

    @Test func testStorageGroupLabelIsNotPromotedToServerGroup() throws {
        let endpoint = try #require(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "workspace_path": "/srv/legacy",
            "storage_group": "fixture",
            "monitor": ["status": "ONLINE"],
        ]))
        #expect(endpoint.serverGroupID == nil)
        #expect(endpoint.workspacePathOverride == nil)
        #expect(endpoint.workspacePath == "/srv/legacy")
        #expect(!(endpoint.inheritsGroupWorkspacePath))
    }

    @Test func testServerGroupRecordRejectsEnvironmentMapAndExecutionFields() {
        #expect(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment_notes": ["CUDA_HOME": "/usr/local/cuda"],
        ]) == nil)
        #expect(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment": ["PATH": "/bin"],
        ]) == nil)
        #expect(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "command": "nvidia-smi",
        ]) == nil)
        #expect(ServerGroupRecord(raw: [
            "id": "lab-a",
            "workspace_path": "relative/path",
        ]) == nil)
        #expect(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment_notes": "CUDA_HOME is under /usr/local/cuda",
            "description": "plain notes only",
        ]) != nil)
    }

    @Test func testServerGroupDraftRequiresSlugNameAbsolutePathAndPlainNotes() throws {
        let draft = try ServerGroupDraft(
            id: "lab-a",
            displayName: " Lab A ",
            workspacePath: "/srv/lab-a",
            environmentNotes: " weights under /data ",
            description: " shared pool "
        )
        #expect(draft.id == "lab-a")
        #expect(draft.displayName == "Lab A")
        #expect(draft.workspacePath == "/srv/lab-a")
        #expect(draft.environmentNotes == "weights under /data")
        #expect(draft.description == "shared pool")

        #expect(throws: Never.self) { try ServerGroupDraft(
            id: "ab",
            displayName: "AB",
            workspacePath: "/srv/ab"
        ) }
        #expect(throws: Never.self) { try ServerGroupDraft(
            id: "a" + String(repeating: "x", count: 127),
            displayName: String(repeating: "n", count: 120),
            workspacePath: "/srv/lab-a",
            environmentNotes: String(repeating: "e", count: 8_000),
            description: String(repeating: "d", count: 1_000)
        ) }

        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "a" + String(repeating: "x", count: 128),
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "Lab A",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab_a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: String(repeating: "n", count: 121),
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab\nA",
            workspacePath: "/srv/lab-a"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "relative/path"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: "bad\0notes"
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: String(repeating: "e", count: 8_001)
        ) }
        #expect(throws: (any Error).self) { try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            description: String(repeating: "d", count: 1_001)
        ) }

        let update = try ServerGroupUpdateDraft(
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: "notes",
            description: "desc"
        )
        #expect(update.displayName == "Lab A")
        #expect(throws: (any Error).self) { try ServerGroupUpdateDraft(
            displayName: "Lab A",
            workspacePath: ""
        ) }
        #expect(throws: (any Error).self) { try ServerGroupUpdateDraft(
            displayName: String(repeating: "n", count: 121),
            workspacePath: "/srv/lab-a"
        ) }
    }

    @Test func testEndpointDraftAllowsInheritedPathWhenGroupedAndRejectsOverrideWithoutGroup() throws {
        let inherited = try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "",
            observationProfile: "linux-nvidia",
            suppliedID: "node-a1",
            serverGroupID: "lab-a"
        )
        #expect(inherited.serverGroupID == "lab-a")
        #expect(inherited.workspacePath == "")
        #expect(inherited.workspacePathOverride == nil)

        let overridden = try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/mnt/override",
            observationProfile: "linux-nvidia",
            suppliedID: "node-a2",
            serverGroupID: "lab-a",
            workspacePathOverride: "/mnt/override"
        )
        #expect(overridden.workspacePathOverride == "/mnt/override")

        #expect(throws: (any Error).self) { try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "",
            observationProfile: "linux-nvidia",
            suppliedID: "solo-1"
        ) }
        #expect(throws: (any Error).self) { try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/srv/solo",
            observationProfile: "linux-nvidia",
            suppliedID: "solo-1",
            workspacePathOverride: "/mnt/override"
        ) }
    }

    @Test func testLegacyEndpointUpdatePayloadOmitsGroupFields() throws {
        let draft = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "server-script-v1"
        )
        let payload = BrokerStore.endpointUpdatePayload(draft)
        #expect(Set(payload.keys) == ["ssh_user", "workspace_path", "observation_profile"])
        #expect(payload["ssh_user"] as? String == "collector")
        #expect(payload["workspace_path"] as? String == "/srv/storyboard")
        #expect(payload["observation_profile"] as? String == "server-script-v1")
    }

    @Test func testGroupedEndpointPayloadsSendNullableOverrideAndOmitEffectivePath() throws {
        let inheritedCreate = try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/srv/lab-a",
            observationProfile: "linux-nvidia",
            suppliedID: "node-a1",
            serverGroupID: "lab-a"
        )
        let inheritedCreatePayload = BrokerStore.endpointCreatePayload(inheritedCreate)
        #expect(inheritedCreatePayload["server_group_id"] as? String == "lab-a")
        #expect(inheritedCreatePayload["workspace_path_override"] is NSNull)
        #expect(inheritedCreatePayload["workspace_path"] == nil)
        #expect(!(Set(inheritedCreatePayload.keys).contains("workspace_path")))

        let overrideCreate = try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/mnt/override",
            observationProfile: "linux-nvidia",
            suppliedID: "node-a2",
            serverGroupID: "lab-a",
            workspacePathOverride: "/mnt/override"
        )
        let overrideCreatePayload = BrokerStore.endpointCreatePayload(overrideCreate)
        #expect(overrideCreatePayload["server_group_id"] as? String == "lab-a")
        #expect(overrideCreatePayload["workspace_path_override"] as? String == "/mnt/override")
        #expect(overrideCreatePayload["workspace_path"] == nil)

        let assigned = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/lab-a",
            observationProfile: "linux-nvidia",
            serverGroupID: "lab-a",
            workspacePathOverride: nil
        )
        let assignedPayload = BrokerStore.endpointUpdatePayload(assigned)
        #expect(Set(assignedPayload.keys) == ["ssh_user", "observation_profile", "server_group_id", "workspace_path_override"])
        #expect(assignedPayload["server_group_id"] as? String == "lab-a")
        #expect(assignedPayload["workspace_path_override"] is NSNull)
        #expect(assignedPayload["workspace_path"] == nil)

        let unassigned = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/solo",
            observationProfile: "linux-nvidia",
            serverGroupID: nil,
            workspacePathOverride: nil
        )
        let unassignedPayload = BrokerStore.endpointUpdatePayload(unassigned)
        #expect(Set(unassignedPayload.keys) == ["ssh_user", "observation_profile", "workspace_path", "server_group_id"])
        #expect(unassignedPayload["server_group_id"] is NSNull)
        #expect(unassignedPayload["workspace_path_override"] == nil)
        #expect(unassignedPayload["workspace_path"] as? String == "/srv/solo")
    }

    @Test func testGroupedUpdateFromRecordDoesNotFreezeEffectiveWorkspacePath() throws {
        let inherited = try #require(EndpointRecord(raw: [
            "id": "node-a1",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "server_group_id": "lab-a",
            "workspace_path": "/srv/lab-a",
        ]))
        #expect(inherited.workspacePath == "/srv/lab-a")
        let draft = EndpointUpdateDraft(endpoint: inherited)
        #expect(draft.workspacePath == "/srv/lab-a")
        #expect(draft.includesGroupAssignment)
        let payload = BrokerStore.endpointUpdatePayload(draft)
        #expect(payload["server_group_id"] as? String == "lab-a")
        #expect(payload["workspace_path_override"] is NSNull)
        #expect(payload["workspace_path"] == nil)
    }

    @Test func testLegacyEndpointCreatePayloadSendsWorkspacePathAndOmitsOverride() throws {
        let draft = try EndpointDraft(
            host: "gpu.example.test",
            port: 2201,
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "linux-nvidia",
            suppliedID: "legacy-node"
        )
        let payload = BrokerStore.endpointCreatePayload(draft)
        #expect(payload["workspace_path"] as? String == "/srv/storyboard")
        #expect(payload["server_group_id"] == nil)
        #expect(payload["workspace_path_override"] == nil)
        #expect(!(Set(payload.keys).contains("workspace_path_override")))
    }

    @Test func testGroupedClaimConstraintsEmitServerGroupIdsAndOmitEndpointIds() {
        let grouped = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 2,
            endpointID: "node-a1",
            serverGroupID: "lab-a"
        )
        let constraints = BrokerStore.claimConstraints(for: grouped)
        #expect(Set(constraints.keys) == ["gpu_count", "placement", "same_host", "server_group_ids"])
        #expect(constraints["gpu_count"] as? Int == 2)
        #expect(constraints["placement"] as? String == "pack")
        #expect(constraints["same_host"] as? Bool == true)
        #expect(constraints["server_group_ids"] as? [String] == ["lab-a"])
        #expect(constraints["endpoint_ids"] == nil)

        let trimmed = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "node-a1",
            serverGroupID: "  lab-a  "
        )
        let trimmedConstraints = BrokerStore.claimConstraints(for: trimmed)
        #expect(Set(trimmedConstraints.keys) == ["gpu_count", "placement", "same_host", "server_group_ids"])
        #expect(trimmedConstraints["same_host"] as? Bool == true)
        #expect(trimmedConstraints["server_group_ids"] as? [String] == ["lab-a"])
        #expect(trimmedConstraints["endpoint_ids"] == nil)
    }

    @Test func testLegacyClaimConstraintsKeepEndpointIdsWhenGroupIsAbsent() {
        let legacy = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "solo-1"
        )
        #expect(legacy.serverGroupID == nil)
        let constraints = BrokerStore.claimConstraints(for: legacy)
        #expect(Set(constraints.keys) == ["gpu_count", "placement", "same_host", "endpoint_ids"])
        #expect(constraints["same_host"] as? Bool == true)
        #expect(constraints["endpoint_ids"] as? [String] == ["solo-1"])
        #expect(constraints["server_group_ids"] == nil)

        let blankGroup = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "solo-1",
            serverGroupID: "   "
        )
        let blankConstraints = BrokerStore.claimConstraints(for: blankGroup)
        #expect(Set(blankConstraints.keys) == ["gpu_count", "placement", "same_host", "endpoint_ids"])
        #expect(blankConstraints["same_host"] as? Bool == true)
        #expect(blankConstraints["endpoint_ids"] as? [String] == ["solo-1"])
        #expect(blankConstraints["server_group_ids"] == nil)
    }

    @Test func testSubmitClaimPostsGroupedAndLegacyConstraintPayloads() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GroupRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        var snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        snapshot.snapshotRevision = 102
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        GroupRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: [
            "snapshot_revision": 102,
            "request": ["id": "req-1"],
        ])
        defer { GroupRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: GroupRepeatingClient(snapshot),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["instant_claims"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let groupedRecorder = CompletionRecorder()
        store.submitClaim(ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 2,
            endpointID: "node-a1",
            serverGroupID: "lab-a"
        )) { result, message in
            groupedRecorder.success = result != nil
            groupedRecorder.message = message
        }
        try await waitUntil { groupedRecorder.success != nil }
        #expect(groupedRecorder.success == true)
        #expect(GroupRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(GroupRouteURLProtocol.lastRequest?.url?.path == "/api/v1/claims")
        let groupedPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let groupedPayload = try #require(
            try JSONSerialization.jsonObject(with: groupedPayloadBody) as? [String: Any]
        )
        let groupedConstraints = try #require(groupedPayload["constraints"] as? [String: Any])
        #expect(Set(groupedConstraints.keys) == ["gpu_count", "placement", "same_host", "server_group_ids"])
        #expect(groupedConstraints["same_host"] as? Bool == true)
        #expect(groupedConstraints["server_group_ids"] as? [String] == ["lab-a"])
        #expect(groupedConstraints["endpoint_ids"] == nil)

        let legacyRecorder = CompletionRecorder()
        store.submitClaim(ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "solo-1"
        )) { result, message in
            legacyRecorder.success = result != nil
            legacyRecorder.message = message
        }
        try await waitUntil { legacyRecorder.success != nil }
        #expect(legacyRecorder.success == true)
        let legacyPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let legacyPayload = try #require(
            try JSONSerialization.jsonObject(with: legacyPayloadBody) as? [String: Any]
        )
        let legacyConstraints = try #require(legacyPayload["constraints"] as? [String: Any])
        #expect(Set(legacyConstraints.keys) == ["gpu_count", "placement", "same_host", "endpoint_ids"])
        #expect(legacyConstraints["same_host"] as? Bool == true)
        #expect(legacyConstraints["endpoint_ids"] as? [String] == ["solo-1"])
        #expect(legacyConstraints["server_group_ids"] == nil)
    }

    @Test func testEndpointUpdateDraftFromRecordPreservesAssignment() throws {
        let inherited = try #require(EndpointRecord(raw: [
            "id": "node-a1",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "server_group_id": "lab-a",
            "workspace_path": "/srv/lab-a",
        ]))
        let draft = EndpointUpdateDraft(endpoint: inherited)
        #expect(draft.serverGroupID == "lab-a")
        #expect(draft.workspacePathOverride == nil)
        #expect(draft.includesGroupAssignment)
        #expect(draft.workspacePath == "/srv/lab-a")
    }

    @Test func testServerGroupCRUDUsesDocumentedRoutesAndPayloads() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GroupRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        var snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        snapshot.snapshotRevision = 102
        let group = try #require(snapshot.serverGroup(id: "lab-a"))
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        GroupRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 102])
        defer { GroupRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: GroupRepeatingClient(snapshot),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["server_group_crud"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let createRecorder = CompletionRecorder()
        store.createServerGroup(try ServerGroupDraft(
            id: "lab-c",
            displayName: "Lab C",
            workspacePath: "/srv/lab-c",
            environmentNotes: "sync nightly",
            description: "new pool"
        )) { success, message in
            createRecorder.success = success
            createRecorder.message = message
        }
        try await waitUntil { createRecorder.success != nil }
        #expect(createRecorder.success == true)
        #expect(GroupRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(GroupRouteURLProtocol.lastRequest?.url?.path == "/api/v1/server-groups")
        let createPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let createPayload = try #require(
            try JSONSerialization.jsonObject(with: createPayloadBody) as? [String: Any]
        )
        #expect(createPayload["id"] as? String == "lab-c")
        #expect(createPayload["display_name"] as? String == "Lab C")
        #expect(createPayload["workspace_path"] as? String == "/srv/lab-c")
        #expect(createPayload["environment_notes"] as? String == "sync nightly")
        #expect(createPayload["description"] as? String == "new pool")
        #expect(createPayload["environment"] == nil)
        #expect(createPayload["command"] == nil)

        let updateRecorder = CompletionRecorder()
        store.updateServerGroup(
            group,
            draft: try ServerGroupUpdateDraft(
                displayName: "Lab A2",
                workspacePath: "/srv/lab-a2",
                environmentNotes: "moved",
                description: "updated"
            )
        ) { success, message in
            updateRecorder.success = success
            updateRecorder.message = message
        }
        try await waitUntil { updateRecorder.success != nil }
        #expect(updateRecorder.success == true)
        #expect(GroupRouteURLProtocol.lastRequest?.httpMethod == "PATCH")
        #expect(GroupRouteURLProtocol.lastRequest?.url?.path == "/api/v1/server-groups/lab-a")
        let updatePayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let updatePayload = try #require(
            try JSONSerialization.jsonObject(with: updatePayloadBody) as? [String: Any]
        )
        #expect(updatePayload["display_name"] as? String == "Lab A2")
        #expect(updatePayload["workspace_path"] as? String == "/srv/lab-a2")
        #expect(updatePayload["id"] == nil)

        let deleteRecorder = CompletionRecorder()
        store.deleteServerGroup(group) { success, message in
            deleteRecorder.success = success
            deleteRecorder.message = message
        }
        try await waitUntil { deleteRecorder.success != nil }
        #expect(deleteRecorder.success == true)
        #expect(GroupRouteURLProtocol.lastRequest?.httpMethod == "DELETE")
        #expect(GroupRouteURLProtocol.lastRequest?.url?.path == "/api/v1/server-groups/lab-a")
    }

    @Test func testAddEndpointPayloadIncludesGroupAssignmentAndOverride() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GroupRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        var snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        snapshot.snapshotRevision = 102
        let store = BrokerStore(
            actorID: "tester",
            refreshTimeoutSeconds: 1,
            refreshIntervalSeconds: 0,
            mutationSession: mutationSession
        )
        GroupRouteURLProtocol.responseData = try JSONSerialization.data(withJSONObject: ["snapshot_revision": 102])
        defer { GroupRouteURLProtocol.reset() }
        store.connectForTesting(
            snapshotClient: GroupRepeatingClient(snapshot),
            serviceInfo: ServiceInfo(schemaVersion: "v1", capabilities: ["instant_claims", "server_group_crud"]),
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { store.freshness == .fresh && !store.isRefreshing }

        let groupedRecorder = CompletionRecorder()
        store.addEndpoint(try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "",
            observationProfile: "linux-nvidia",
            suppliedID: "node-new",
            serverGroupID: "lab-a",
            workspacePathOverride: "/mnt/override"
        )) { success, message in
            groupedRecorder.success = success
            groupedRecorder.message = message
        }
        try await waitUntil { groupedRecorder.success != nil }
        #expect(groupedRecorder.success == true)
        #expect(GroupRouteURLProtocol.lastRequest?.httpMethod == "POST")
        #expect(GroupRouteURLProtocol.lastRequest?.url?.path == "/api/v1/endpoints")
        let groupedPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let groupedPayload = try #require(
            try JSONSerialization.jsonObject(with: groupedPayloadBody) as? [String: Any]
        )
        #expect(groupedPayload["server_group_id"] as? String == "lab-a")
        #expect(groupedPayload["workspace_path_override"] as? String == "/mnt/override")
        #expect(groupedPayload["workspace_path"] == nil)

        let inheritRecorder = CompletionRecorder()
        store.addEndpoint(try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/srv/lab-a",
            observationProfile: "linux-nvidia",
            suppliedID: "node-inherit",
            serverGroupID: "lab-a"
        )) { success, message in
            inheritRecorder.success = success
            inheritRecorder.message = message
        }
        try await waitUntil { inheritRecorder.success != nil }
        #expect(inheritRecorder.success == true)
        let inheritPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let inheritPayload = try #require(
            try JSONSerialization.jsonObject(with: inheritPayloadBody) as? [String: Any]
        )
        #expect(inheritPayload["server_group_id"] as? String == "lab-a")
        #expect(inheritPayload["workspace_path_override"] is NSNull)
        #expect(inheritPayload["workspace_path"] == nil)

        let legacyRecorder = CompletionRecorder()
        store.addEndpoint(try EndpointDraft(
            host: "gpu.example.test",
            port: 2201,
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "linux-nvidia",
            suppliedID: "legacy-node"
        )) { success, message in
            legacyRecorder.success = success
            legacyRecorder.message = message
        }
        try await waitUntil { legacyRecorder.success != nil }
        #expect(legacyRecorder.success == true)
        let legacyPayloadBody = try #require(GroupRouteURLProtocol.lastBody)
        let legacyPayload = try #require(
            try JSONSerialization.jsonObject(with: legacyPayloadBody) as? [String: Any]
        )
        #expect(legacyPayload["workspace_path"] as? String == "/srv/storyboard")
        #expect(legacyPayload["server_group_id"] == nil)
        #expect(legacyPayload["workspace_path_override"] == nil)
    }

    @Test func testServerGroupCRUDFailsClosedWithoutCapabilityAndStaysReadOnlyInFixtureMode() async throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        let group = try #require(snapshot.serverGroup(id: "lab-a"))
        let advertised = ServiceInfo(schemaVersion: "v1", capabilities: ["endpoint_update"])
        #expect(!(advertised.supportsServerGroupCRUD))
        #expect(!(ServiceInfo(schemaVersion: "v1", capabilities: ["server_groups"]).supportsServerGroupCRUD))
        #expect(ServiceInfo.fixture.supportsServerGroupCRUD)
        #expect(ServiceInfo(schemaVersion: "v1", capabilities: []).supportsServerGroupCRUD)
        #expect(ServiceInfo(schemaVersion: "v1", capabilities: ["server_group_crud"]).supportsServerGroupCRUD)

        let liveStore = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        liveStore.connectForTesting(
            snapshotClient: GroupRepeatingClient(snapshot),
            serviceInfo: advertised,
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { liveStore.freshness == .fresh && !liveStore.isRefreshing }
        #expect(!(liveStore.supportsServerGroupCRUD))

        let createRecorder = CompletionRecorder()
        liveStore.createServerGroup(try ServerGroupDraft(
            id: "lab-c",
            displayName: "Lab C",
            workspacePath: "/srv/lab-c"
        )) { success, message in
            createRecorder.success = success
            createRecorder.message = message
        }
        try await waitUntil { createRecorder.success != nil }
        #expect(createRecorder.success == false)
        #expect(createRecorder.message?.contains("创建服务器分组") == true)

        let fixtureStore = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        fixtureStore.useFixture(snapshot: snapshot)
        #expect(fixtureStore.supportsServerGroupCRUD)
        #expect(!(fixtureStore.allowsMutations))

        let fixtureRecorder = CompletionRecorder()
        fixtureStore.deleteServerGroup(group) { success, message in
            fixtureRecorder.success = success
            fixtureRecorder.message = message
        }
        try await waitUntil { fixtureRecorder.success != nil }
        #expect(fixtureRecorder.success == false)
        #expect(fixtureRecorder.message == "当前为只读测试夹具或尚未连接本机服务，不能执行资源变更。")
    }

    @Test func testGroupedSnapshotChangeIsNotSemanticallyEquivalent() {
        let grouped = BrokerSnapshot(envelope: Self.groupedEnvelope())
        var withoutGroups = grouped
        withoutGroups.serverGroups = []
        #expect(!(grouped.isSemanticallyEquivalentForRefresh(to: withoutGroups)))
    }

    private static func groupedEnvelope() -> [String: Any] {
        [
            "schema_version": "v1",
            "snapshot_revision": 200,
            "server_time": "2026-08-27T00:00:00Z",
            "data": [
                "current": [
                    "summary": ["total_gpus": 0, "total_servers": 4],
                    "server_groups": [
                        [
                            "id": "lab-a",
                            "display_name": "Lab A",
                            "workspace_path": "/srv/lab-a",
                            "environment_notes": "weights under /data/lab-a",
                            "description": "shared A100 pool",
                        ],
                        [
                            "id": "lab-b",
                            "display_name": "Lab B",
                            "workspace_path": "/srv/lab-b",
                            "environment_notes": "",
                            "description": "",
                        ],
                    ],
                    "endpoints": [
                        [
                            "id": "node-a1",
                            "host": "10.0.0.1",
                            "ssh_user": "gpu",
                            "server_group_id": "lab-a",
                            "workspace_path": "/srv/lab-a",
                        ],
                        [
                            "id": "node-a2",
                            "host": "10.0.0.2",
                            "ssh_user": "gpu",
                            "server_group_id": "lab-a",
                            "workspace_path": "/mnt/override-a2",
                            "workspace_path_override": "/mnt/override-a2",
                        ],
                        [
                            "id": "node-b1",
                            "host": "10.0.0.3",
                            "ssh_user": "gpu",
                            "server_group_id": "lab-b",
                            "workspace_path": "/srv/lab-b",
                        ],
                        [
                            "id": "solo-1",
                            "host": "10.0.0.4",
                            "ssh_user": "gpu",
                            "workspace_path": "/srv/solo",
                            "storage_group": "fixture",
                        ],
                    ],
                    "gpus": [],
                    "leases": [],
                    "requests": [],
                    "data_age_seconds": 1,
                    "freshness_seconds": 30,
                    "admission_boundary": "test",
                ],
                "history": [:],
            ],
        ]
    }

    private static func fixtureSnapshot(named name: String) throws -> BrokerSnapshot {
        let fixturesRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures", isDirectory: true)
        let url = try FixtureSnapshots.resolve(name, fixturesRoot: fixturesRoot)
        return try FixtureSnapshots.load(from: url)
    }

    private func waitUntil(
        timeout: TimeInterval = 1.0,
        _ condition: @escaping () -> Bool
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline {
                Issue.record("Timed out waiting for condition")
                return
            }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }
}

@MainActor
private final class CompletionRecorder {
    var success: Bool?
    var message: String?
}

private actor GroupRepeatingClient: BrokerSnapshotClient {
    private let stored: BrokerSnapshot

    init(_ stored: BrokerSnapshot) {
        self.stored = stored
    }

    func snapshot(actorID: String) async throws -> BrokerSnapshot {
        stored
    }
}

private final class GroupRouteURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var responseData: Data?
    nonisolated(unsafe) static var lastRequest: URLRequest?
    nonisolated(unsafe) static var lastBody: Data?
    nonisolated(unsafe) static var statusCode = 200

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
