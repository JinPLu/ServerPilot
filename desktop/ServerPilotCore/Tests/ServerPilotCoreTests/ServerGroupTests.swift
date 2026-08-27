import Foundation
import XCTest
@testable import ServerPilotCore

@MainActor
final class ServerGroupTests: XCTestCase {
    func testSnapshotDecodesServerGroupsAndEndpointAssignment() throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        XCTAssertEqual(snapshot.serverGroups.map(\.id), ["lab-a", "lab-b"])
        XCTAssertEqual(snapshot.serverGroup(id: "lab-a")?.displayName, "Lab A")
        XCTAssertEqual(snapshot.serverGroup(id: "lab-a")?.workspacePath, "/srv/lab-a")
        XCTAssertEqual(snapshot.serverGroup(id: "lab-a")?.environmentNotes, "weights under /data/lab-a")
        XCTAssertEqual(snapshot.serverGroup(id: "lab-a")?.description, "shared A100 pool")

        let inherited = try XCTUnwrap(snapshot.endpoint(id: "node-a1"))
        XCTAssertEqual(inherited.serverGroupID, "lab-a")
        XCTAssertEqual(inherited.workspacePath, "/srv/lab-a")
        XCTAssertNil(inherited.workspacePathOverride)
        XCTAssertTrue(inherited.inheritsGroupWorkspacePath)
        XCTAssertEqual(snapshot.serverGroup(for: inherited)?.id, "lab-a")

        let overridden = try XCTUnwrap(snapshot.endpoint(id: "node-a2"))
        XCTAssertEqual(overridden.serverGroupID, "lab-a")
        XCTAssertEqual(overridden.workspacePath, "/mnt/override-a2")
        XCTAssertEqual(overridden.workspacePathOverride, "/mnt/override-a2")
        XCTAssertFalse(overridden.inheritsGroupWorkspacePath)

        let ungrouped = try XCTUnwrap(snapshot.endpoint(id: "solo-1"))
        XCTAssertNil(ungrouped.serverGroupID)
        XCTAssertEqual(ungrouped.workspacePath, "/srv/solo")
        XCTAssertNil(ungrouped.workspacePathOverride)
        XCTAssertFalse(ungrouped.inheritsGroupWorkspacePath)
    }

    func testGroupingHelpersSeparateMembershipFromUngroupedEndpoints() throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        XCTAssertEqual(snapshot.endpoints(inGroup: "lab-a").map(\.id), ["node-a1", "node-a2"])
        XCTAssertEqual(snapshot.endpoints(inGroup: "lab-b").map(\.id), ["node-b1"])
        XCTAssertEqual(snapshot.ungroupedEndpoints.map(\.id), ["solo-1"])
        XCTAssertTrue(snapshot.endpoints(inGroup: "missing").isEmpty)
    }

    func testMissingServerGroupsKeepsLegacyEndpointsCompatible() throws {
        let snapshot = try Self.fixtureSnapshot(named: "1")
        XCTAssertTrue(snapshot.serverGroups.isEmpty)
        XCTAssertEqual(snapshot.ungroupedEndpoints.map(\.id), snapshot.endpoints.map(\.id))
        let endpoint = try XCTUnwrap(snapshot.endpoints.first)
        XCTAssertNil(endpoint.serverGroupID)
        XCTAssertNil(endpoint.workspacePathOverride)
        XCTAssertEqual(endpoint.workspacePath, "/srv/serverpilot-fixtures")
        XCTAssertFalse(endpoint.inheritsGroupWorkspacePath)
        XCTAssertNil(snapshot.serverGroup(for: endpoint))
    }

    func testStorageGroupLabelIsNotPromotedToServerGroup() throws {
        let endpoint = try XCTUnwrap(EndpointRecord(raw: [
            "id": "gpu-node-01",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "workspace_path": "/srv/legacy",
            "storage_group": "fixture",
            "monitor": ["status": "ONLINE"],
        ]))
        XCTAssertNil(endpoint.serverGroupID)
        XCTAssertNil(endpoint.workspacePathOverride)
        XCTAssertEqual(endpoint.workspacePath, "/srv/legacy")
        XCTAssertFalse(endpoint.inheritsGroupWorkspacePath)
    }

    func testServerGroupRecordRejectsEnvironmentMapAndExecutionFields() {
        XCTAssertNil(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment_notes": ["CUDA_HOME": "/usr/local/cuda"],
        ]))
        XCTAssertNil(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment": ["PATH": "/bin"],
        ]))
        XCTAssertNil(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "command": "nvidia-smi",
        ]))
        XCTAssertNil(ServerGroupRecord(raw: [
            "id": "lab-a",
            "workspace_path": "relative/path",
        ]))
        XCTAssertNotNil(ServerGroupRecord(raw: [
            "id": "lab-a",
            "display_name": "Lab A",
            "workspace_path": "/srv/lab-a",
            "environment_notes": "CUDA_HOME is under /usr/local/cuda",
            "description": "plain notes only",
        ]))
    }

    func testServerGroupDraftRequiresSlugNameAbsolutePathAndPlainNotes() throws {
        let draft = try ServerGroupDraft(
            id: "lab-a",
            displayName: " Lab A ",
            workspacePath: "/srv/lab-a",
            environmentNotes: " weights under /data ",
            description: " shared pool "
        )
        XCTAssertEqual(draft.id, "lab-a")
        XCTAssertEqual(draft.displayName, "Lab A")
        XCTAssertEqual(draft.workspacePath, "/srv/lab-a")
        XCTAssertEqual(draft.environmentNotes, "weights under /data")
        XCTAssertEqual(draft.description, "shared pool")

        XCTAssertNoThrow(try ServerGroupDraft(
            id: "ab",
            displayName: "AB",
            workspacePath: "/srv/ab"
        ))
        XCTAssertNoThrow(try ServerGroupDraft(
            id: "a" + String(repeating: "x", count: 127),
            displayName: String(repeating: "n", count: 120),
            workspacePath: "/srv/lab-a",
            environmentNotes: String(repeating: "e", count: 8_000),
            description: String(repeating: "d", count: 1_000)
        ))

        XCTAssertThrowsError(try ServerGroupDraft(
            id: "a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "a" + String(repeating: "x", count: 128),
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "Lab A",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab_a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: String(repeating: "n", count: 121),
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab\nA",
            workspacePath: "/srv/lab-a"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "relative/path"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: "bad\0notes"
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: String(repeating: "e", count: 8_001)
        ))
        XCTAssertThrowsError(try ServerGroupDraft(
            id: "lab-a",
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            description: String(repeating: "d", count: 1_001)
        ))

        let update = try ServerGroupUpdateDraft(
            displayName: "Lab A",
            workspacePath: "/srv/lab-a",
            environmentNotes: "notes",
            description: "desc"
        )
        XCTAssertEqual(update.displayName, "Lab A")
        XCTAssertThrowsError(try ServerGroupUpdateDraft(
            displayName: "Lab A",
            workspacePath: ""
        ))
        XCTAssertThrowsError(try ServerGroupUpdateDraft(
            displayName: String(repeating: "n", count: 121),
            workspacePath: "/srv/lab-a"
        ))
    }

    func testEndpointDraftAllowsInheritedPathWhenGroupedAndRejectsOverrideWithoutGroup() throws {
        let inherited = try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "",
            observationProfile: "linux-nvidia",
            suppliedID: "node-a1",
            serverGroupID: "lab-a"
        )
        XCTAssertEqual(inherited.serverGroupID, "lab-a")
        XCTAssertEqual(inherited.workspacePath, "")
        XCTAssertNil(inherited.workspacePathOverride)

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
        XCTAssertEqual(overridden.workspacePathOverride, "/mnt/override")

        XCTAssertThrowsError(try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "",
            observationProfile: "linux-nvidia",
            suppliedID: "solo-1"
        ))
        XCTAssertThrowsError(try EndpointDraft(
            host: "gpu.example.test",
            port: 22,
            sshUser: "collector",
            workspacePath: "/srv/solo",
            observationProfile: "linux-nvidia",
            suppliedID: "solo-1",
            workspacePathOverride: "/mnt/override"
        ))
    }

    func testLegacyEndpointUpdatePayloadOmitsGroupFields() throws {
        let draft = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "server-script-v1"
        )
        let payload = BrokerStore.endpointUpdatePayload(draft)
        XCTAssertEqual(Set(payload.keys), ["ssh_user", "workspace_path", "observation_profile"])
        XCTAssertEqual(payload["ssh_user"] as? String, "collector")
        XCTAssertEqual(payload["workspace_path"] as? String, "/srv/storyboard")
        XCTAssertEqual(payload["observation_profile"] as? String, "server-script-v1")
    }

    func testGroupedEndpointPayloadsSendNullableOverrideAndOmitEffectivePath() throws {
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
        XCTAssertEqual(inheritedCreatePayload["server_group_id"] as? String, "lab-a")
        XCTAssertTrue(inheritedCreatePayload["workspace_path_override"] is NSNull)
        XCTAssertNil(inheritedCreatePayload["workspace_path"])
        XCTAssertFalse(Set(inheritedCreatePayload.keys).contains("workspace_path"))

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
        XCTAssertEqual(overrideCreatePayload["server_group_id"] as? String, "lab-a")
        XCTAssertEqual(overrideCreatePayload["workspace_path_override"] as? String, "/mnt/override")
        XCTAssertNil(overrideCreatePayload["workspace_path"])

        let assigned = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/lab-a",
            observationProfile: "linux-nvidia",
            serverGroupID: "lab-a",
            workspacePathOverride: nil
        )
        let assignedPayload = BrokerStore.endpointUpdatePayload(assigned)
        XCTAssertEqual(Set(assignedPayload.keys), ["ssh_user", "observation_profile", "server_group_id", "workspace_path_override"])
        XCTAssertEqual(assignedPayload["server_group_id"] as? String, "lab-a")
        XCTAssertTrue(assignedPayload["workspace_path_override"] is NSNull)
        XCTAssertNil(assignedPayload["workspace_path"])

        let unassigned = try EndpointUpdateDraft(
            sshUser: "collector",
            workspacePath: "/srv/solo",
            observationProfile: "linux-nvidia",
            serverGroupID: nil,
            workspacePathOverride: nil
        )
        let unassignedPayload = BrokerStore.endpointUpdatePayload(unassigned)
        XCTAssertEqual(Set(unassignedPayload.keys), ["ssh_user", "observation_profile", "workspace_path", "server_group_id"])
        XCTAssertTrue(unassignedPayload["server_group_id"] is NSNull)
        XCTAssertNil(unassignedPayload["workspace_path_override"])
        XCTAssertEqual(unassignedPayload["workspace_path"] as? String, "/srv/solo")
    }

    func testGroupedUpdateFromRecordDoesNotFreezeEffectiveWorkspacePath() throws {
        let inherited = try XCTUnwrap(EndpointRecord(raw: [
            "id": "node-a1",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "server_group_id": "lab-a",
            "workspace_path": "/srv/lab-a",
        ]))
        XCTAssertEqual(inherited.workspacePath, "/srv/lab-a")
        let draft = EndpointUpdateDraft(endpoint: inherited)
        XCTAssertEqual(draft.workspacePath, "/srv/lab-a")
        XCTAssertTrue(draft.includesGroupAssignment)
        let payload = BrokerStore.endpointUpdatePayload(draft)
        XCTAssertEqual(payload["server_group_id"] as? String, "lab-a")
        XCTAssertTrue(payload["workspace_path_override"] is NSNull)
        XCTAssertNil(payload["workspace_path"])
    }

    func testLegacyEndpointCreatePayloadSendsWorkspacePathAndOmitsOverride() throws {
        let draft = try EndpointDraft(
            host: "gpu.example.test",
            port: 2201,
            sshUser: "collector",
            workspacePath: "/srv/storyboard",
            observationProfile: "linux-nvidia",
            suppliedID: "legacy-node"
        )
        let payload = BrokerStore.endpointCreatePayload(draft)
        XCTAssertEqual(payload["workspace_path"] as? String, "/srv/storyboard")
        XCTAssertNil(payload["server_group_id"])
        XCTAssertNil(payload["workspace_path_override"])
        XCTAssertFalse(Set(payload.keys).contains("workspace_path_override"))
    }

    func testGroupedClaimConstraintsEmitServerGroupIdsAndOmitEndpointIds() {
        let grouped = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 2,
            endpointID: "node-a1",
            serverGroupID: "lab-a"
        )
        let constraints = BrokerStore.claimConstraints(for: grouped)
        XCTAssertEqual(Set(constraints.keys), ["gpu_count", "placement", "same_host", "server_group_ids"])
        XCTAssertEqual(constraints["gpu_count"] as? Int, 2)
        XCTAssertEqual(constraints["placement"] as? String, "pack")
        XCTAssertEqual(constraints["same_host"] as? Bool, true)
        XCTAssertEqual(constraints["server_group_ids"] as? [String], ["lab-a"])
        XCTAssertNil(constraints["endpoint_ids"])

        let trimmed = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "node-a1",
            serverGroupID: "  lab-a  "
        )
        let trimmedConstraints = BrokerStore.claimConstraints(for: trimmed)
        XCTAssertEqual(Set(trimmedConstraints.keys), ["gpu_count", "placement", "same_host", "server_group_ids"])
        XCTAssertEqual(trimmedConstraints["same_host"] as? Bool, true)
        XCTAssertEqual(trimmedConstraints["server_group_ids"] as? [String], ["lab-a"])
        XCTAssertNil(trimmedConstraints["endpoint_ids"])
    }

    func testLegacyClaimConstraintsKeepEndpointIdsWhenGroupIsAbsent() {
        let legacy = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "solo-1"
        )
        XCTAssertNil(legacy.serverGroupID)
        let constraints = BrokerStore.claimConstraints(for: legacy)
        XCTAssertEqual(Set(constraints.keys), ["gpu_count", "placement", "same_host", "endpoint_ids"])
        XCTAssertEqual(constraints["same_host"] as? Bool, true)
        XCTAssertEqual(constraints["endpoint_ids"] as? [String], ["solo-1"])
        XCTAssertNil(constraints["server_group_ids"])

        let blankGroup = ClaimDraft(
            projectID: "project-a",
            taskReference: "train",
            purpose: "fine-tune",
            gpuCount: 1,
            endpointID: "solo-1",
            serverGroupID: "   "
        )
        let blankConstraints = BrokerStore.claimConstraints(for: blankGroup)
        XCTAssertEqual(Set(blankConstraints.keys), ["gpu_count", "placement", "same_host", "endpoint_ids"])
        XCTAssertEqual(blankConstraints["same_host"] as? Bool, true)
        XCTAssertEqual(blankConstraints["endpoint_ids"] as? [String], ["solo-1"])
        XCTAssertNil(blankConstraints["server_group_ids"])
    }

    func testSubmitClaimPostsGroupedAndLegacyConstraintPayloads() async throws {
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
        XCTAssertEqual(groupedRecorder.success, true)
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.url?.path, "/api/v1/claims")
        let groupedPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        let groupedConstraints = try XCTUnwrap(groupedPayload["constraints"] as? [String: Any])
        XCTAssertEqual(Set(groupedConstraints.keys), ["gpu_count", "placement", "same_host", "server_group_ids"])
        XCTAssertEqual(groupedConstraints["same_host"] as? Bool, true)
        XCTAssertEqual(groupedConstraints["server_group_ids"] as? [String], ["lab-a"])
        XCTAssertNil(groupedConstraints["endpoint_ids"])

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
        XCTAssertEqual(legacyRecorder.success, true)
        let legacyPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        let legacyConstraints = try XCTUnwrap(legacyPayload["constraints"] as? [String: Any])
        XCTAssertEqual(Set(legacyConstraints.keys), ["gpu_count", "placement", "same_host", "endpoint_ids"])
        XCTAssertEqual(legacyConstraints["same_host"] as? Bool, true)
        XCTAssertEqual(legacyConstraints["endpoint_ids"] as? [String], ["solo-1"])
        XCTAssertNil(legacyConstraints["server_group_ids"])
    }

    func testEndpointUpdateDraftFromRecordPreservesAssignment() throws {
        let inherited = try XCTUnwrap(EndpointRecord(raw: [
            "id": "node-a1",
            "host": "10.0.0.1",
            "ssh_user": "gpu",
            "server_group_id": "lab-a",
            "workspace_path": "/srv/lab-a",
        ]))
        let draft = EndpointUpdateDraft(endpoint: inherited)
        XCTAssertEqual(draft.serverGroupID, "lab-a")
        XCTAssertNil(draft.workspacePathOverride)
        XCTAssertTrue(draft.includesGroupAssignment)
        XCTAssertEqual(draft.workspacePath, "/srv/lab-a")
    }

    func testServerGroupCRUDUsesDocumentedRoutesAndPayloads() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [GroupRouteURLProtocol.self]
        let mutationSession = URLSession(configuration: configuration)
        var snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        snapshot.snapshotRevision = 102
        let group = try XCTUnwrap(snapshot.serverGroup(id: "lab-a"))
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
        XCTAssertEqual(createRecorder.success, true)
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.url?.path, "/api/v1/server-groups")
        let createPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        XCTAssertEqual(createPayload["id"] as? String, "lab-c")
        XCTAssertEqual(createPayload["display_name"] as? String, "Lab C")
        XCTAssertEqual(createPayload["workspace_path"] as? String, "/srv/lab-c")
        XCTAssertEqual(createPayload["environment_notes"] as? String, "sync nightly")
        XCTAssertEqual(createPayload["description"] as? String, "new pool")
        XCTAssertNil(createPayload["environment"])
        XCTAssertNil(createPayload["command"])

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
        XCTAssertEqual(updateRecorder.success, true)
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.httpMethod, "PATCH")
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.url?.path, "/api/v1/server-groups/lab-a")
        let updatePayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        XCTAssertEqual(updatePayload["display_name"] as? String, "Lab A2")
        XCTAssertEqual(updatePayload["workspace_path"] as? String, "/srv/lab-a2")
        XCTAssertNil(updatePayload["id"])

        let deleteRecorder = CompletionRecorder()
        store.deleteServerGroup(group) { success, message in
            deleteRecorder.success = success
            deleteRecorder.message = message
        }
        try await waitUntil { deleteRecorder.success != nil }
        XCTAssertEqual(deleteRecorder.success, true)
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.httpMethod, "DELETE")
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.url?.path, "/api/v1/server-groups/lab-a")
    }

    func testAddEndpointPayloadIncludesGroupAssignmentAndOverride() async throws {
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
        XCTAssertEqual(groupedRecorder.success, true)
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.httpMethod, "POST")
        XCTAssertEqual(GroupRouteURLProtocol.lastRequest?.url?.path, "/api/v1/endpoints")
        let groupedPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        XCTAssertEqual(groupedPayload["server_group_id"] as? String, "lab-a")
        XCTAssertEqual(groupedPayload["workspace_path_override"] as? String, "/mnt/override")
        XCTAssertNil(groupedPayload["workspace_path"])

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
        XCTAssertEqual(inheritRecorder.success, true)
        let inheritPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        XCTAssertEqual(inheritPayload["server_group_id"] as? String, "lab-a")
        XCTAssertTrue(inheritPayload["workspace_path_override"] is NSNull)
        XCTAssertNil(inheritPayload["workspace_path"])

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
        XCTAssertEqual(legacyRecorder.success, true)
        let legacyPayload = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(GroupRouteURLProtocol.lastBody)) as? [String: Any]
        )
        XCTAssertEqual(legacyPayload["workspace_path"] as? String, "/srv/storyboard")
        XCTAssertNil(legacyPayload["server_group_id"])
        XCTAssertNil(legacyPayload["workspace_path_override"])
    }

    func testServerGroupCRUDFailsClosedWithoutCapabilityAndStaysReadOnlyInFixtureMode() async throws {
        let snapshot = BrokerSnapshot(envelope: Self.groupedEnvelope())
        let group = try XCTUnwrap(snapshot.serverGroup(id: "lab-a"))
        let advertised = ServiceInfo(schemaVersion: "v1", capabilities: ["endpoint_update"])
        XCTAssertFalse(advertised.supportsServerGroupCRUD)
        XCTAssertFalse(ServiceInfo(schemaVersion: "v1", capabilities: ["server_groups"]).supportsServerGroupCRUD)
        XCTAssertTrue(ServiceInfo.fixture.supportsServerGroupCRUD)
        XCTAssertTrue(ServiceInfo(schemaVersion: "v1", capabilities: []).supportsServerGroupCRUD)
        XCTAssertTrue(ServiceInfo(schemaVersion: "v1", capabilities: ["server_group_crud"]).supportsServerGroupCRUD)

        let liveStore = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        liveStore.connectForTesting(
            snapshotClient: GroupRepeatingClient(snapshot),
            serviceInfo: advertised,
            baseURL: URL(string: "http://broker.test/")!
        )
        try await waitUntil { liveStore.freshness == .fresh && !liveStore.isRefreshing }
        XCTAssertFalse(liveStore.supportsServerGroupCRUD)

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
        XCTAssertEqual(createRecorder.success, false)
        XCTAssertTrue(createRecorder.message?.contains("创建服务器分组") == true)

        let fixtureStore = BrokerStore(actorID: "tester", refreshTimeoutSeconds: 1, refreshIntervalSeconds: 0)
        fixtureStore.useFixture(snapshot: snapshot)
        XCTAssertTrue(fixtureStore.supportsServerGroupCRUD)
        XCTAssertFalse(fixtureStore.allowsMutations)

        let fixtureRecorder = CompletionRecorder()
        fixtureStore.deleteServerGroup(group) { success, message in
            fixtureRecorder.success = success
            fixtureRecorder.message = message
        }
        try await waitUntil { fixtureRecorder.success != nil }
        XCTAssertEqual(fixtureRecorder.success, false)
        XCTAssertEqual(
            fixtureRecorder.message,
            "当前为只读测试夹具或尚未连接本机服务，不能执行资源变更。"
        )
    }

    func testGroupedSnapshotChangeIsNotSemanticallyEquivalent() {
        let grouped = BrokerSnapshot(envelope: Self.groupedEnvelope())
        var withoutGroups = grouped
        withoutGroups.serverGroups = []
        XCTAssertFalse(grouped.isSemanticallyEquivalentForRefresh(to: withoutGroups))
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
                XCTFail("Timed out waiting for condition")
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
