import SwiftUI

private enum ResourceUsageScope: String, CaseIterable, Identifiable {
    case project
    case agent
    case task

    var id: String { rawValue }

    static let visibleCases: [ResourceUsageScope] = [.project, .task]

    var label: String {
        switch self {
        case .project: return "项目"
        case .agent: return "Agent"
        case .task: return "任务"
        }
    }

    var icon: String {
        switch self {
        case .project: return "folder.fill"
        case .agent: return "person.crop.circle.fill"
        case .task: return "checklist.checked"
        }
    }
}

private struct ResourceUsageBucket {
    let key: String
    let title: String
    var projectIDs: Set<String> = []
    var actorIDs: Set<String> = []
    var taskReferences: Set<String> = []
    var claims: [ResourceClaimRecord] = []
    var leases: [LeaseRecord] = []
    var requests: [AllocationRequestRecord] = []
    var reservations: [ReservationRecord] = []
    var actuals: [ResourceRunActualRecord] = []
}

private struct ResourceUsageGroup: Identifiable {
    let id: String
    let scope: ResourceUsageScope
    let title: String
    let projectIDs: [String]
    let actorIDs: [String]
    let taskReferences: [String]
    let claims: [ResourceClaimRecord]
    let leases: [LeaseRecord]
    let requests: [AllocationRequestRecord]
    let reservations: [ReservationRecord]
    let actuals: [ResourceRunActualRecord]

    private let assignedClaimStates = Set(["HELD", "ACTIVE"])
    private let pendingClaimStates = Set(["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"])

    var assignedClaims: [ResourceClaimRecord] {
        claims.filter {
            assignedClaimStates.contains($0.state) && $0.runtimeState != "RUNNING"
        }
    }

    var runningClaims: [ResourceClaimRecord] {
        claims.filter { $0.runtimeState == "RUNNING" || $0.state == "RUNNING" }
    }

    var pendingClaims: [ResourceClaimRecord] {
        claims.filter { pendingClaimStates.contains($0.state) }
    }

    var visibleLegacyRequests: [AllocationRequestRecord] {
        requests
    }

    var assignedLegacyLeases: [LeaseRecord] {
        leases.filter { $0.runtimeState != "RUNNING" }
    }

    var runningLegacyLeases: [LeaseRecord] {
        leases.filter { $0.runtimeState == "RUNNING" }
    }

    var assignedQuantities: ResourceQuantityRecord {
        combinedQuantities(
            assignedClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: assignedLegacyLeases.reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
    }

    var runningQuantities: ResourceQuantityRecord {
        combinedQuantities(
            runningClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: runningLegacyLeases.reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
    }

    var requestedQuantities: ResourceQuantityRecord {
        combinedQuantities(
            pendingClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: visibleLegacyRequests.reduce(0) { $0 + $1.gpuCount }
                )
            ]
        )
    }

    var subtitle: String {
        switch scope {
        case .project:
            return "\(taskReferences.count) 个任务"
        case .agent:
            return "\(projectIDs.count) 个项目 · \(taskReferences.count) 个任务"
        case .task:
            return projectIDs.first ?? "未标注项目"
        }
    }

    var activityCount: Int {
        claims.count + leases.count + visibleLegacyRequests.count + reservations.count + actuals.count
    }

    var visibleActivityCount: Int {
        assignedClaims.count + runningClaims.count + leases.count + actuals.count
    }
}

private struct ResourceUsageProjection {
    let projectCount: Int
    let agentCount: Int
    let taskCount: Int
    let activeTaskCount: Int
    let assignedQuantities: ResourceQuantityRecord
    let runningQuantities: ResourceQuantityRecord
    let requestedQuantities: ResourceQuantityRecord
    let groupsByScope: [ResourceUsageScope: [ResourceUsageGroup]]

    static let empty = ResourceUsageProjection(
        projectCount: 0,
        agentCount: 0,
        taskCount: 0,
        activeTaskCount: 0,
        assignedQuantities: ResourceQuantityRecord(),
        runningQuantities: ResourceQuantityRecord(),
        requestedQuantities: ResourceQuantityRecord(),
        groupsByScope: [:]
    )

    init(snapshot: BrokerSnapshot, scope: ResourceUsageScope) {
        let identities = resourceUsageIdentities(snapshot: snapshot)
        projectCount = Set(identities.map(\.projectID)).count
        agentCount = Set(identities.map(\.actorID)).count
        taskCount = Set(identities.map { "\($0.projectID)\u{1F}\($0.taskReference)" }).count
        activeTaskCount = resourceUsageActiveTaskCount(snapshot: snapshot)

        let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
        let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))
        let legacyLeases = snapshot.leases.filter { !linkedLeaseIDs.contains($0.id) }
        let legacyRequests = snapshot.requests.filter {
            !linkedRequestIDs.contains($0.id) && resourceUsageRequestIsPending($0)
        }
        let assignedClaims = snapshot.resourceClaims.filter {
            ["HELD", "ACTIVE"].contains($0.state) && $0.runtimeState != "RUNNING"
        }
        let runningClaims = snapshot.resourceClaims.filter {
            $0.runtimeState == "RUNNING" || $0.state == "RUNNING"
        }
        let pendingClaims = snapshot.resourceClaims.filter {
            ["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"].contains($0.state)
        }
        assignedQuantities = combinedQuantities(
            assignedClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyLeases
                        .filter { $0.runtimeState != "RUNNING" }
                        .reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
        runningQuantities = combinedQuantities(
            runningClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyLeases
                        .filter { $0.runtimeState == "RUNNING" }
                        .reduce(0) { $0 + $1.gpuIDs.count }
                )
            ]
        )
        requestedQuantities = combinedQuantities(
            pendingClaims.map(\.quantities) + [
                ResourceQuantityRecord(
                    gpuCount: legacyRequests.reduce(0) { $0 + $1.gpuCount }
                )
            ]
        )

        groupsByScope = [scope: makeResourceUsageGroups(snapshot: snapshot, scope: scope)]
    }

    private init(
        projectCount: Int,
        agentCount: Int,
        taskCount: Int,
        activeTaskCount: Int,
        assignedQuantities: ResourceQuantityRecord,
        runningQuantities: ResourceQuantityRecord,
        requestedQuantities: ResourceQuantityRecord,
        groupsByScope: [ResourceUsageScope: [ResourceUsageGroup]]
    ) {
        self.projectCount = projectCount
        self.agentCount = agentCount
        self.taskCount = taskCount
        self.activeTaskCount = activeTaskCount
        self.assignedQuantities = assignedQuantities
        self.runningQuantities = runningQuantities
        self.requestedQuantities = requestedQuantities
        self.groupsByScope = groupsByScope
    }

    func groups(for scope: ResourceUsageScope) -> [ResourceUsageGroup] {
        groupsByScope[scope] ?? []
    }
}

struct ResourceUsageDashboard: View {
    @ObservedObject var store: BrokerStore
    let claimGPU: () -> Void
    @State private var scope: ResourceUsageScope = .project
    @State private var projection: ResourceUsageProjection
    @State private var selectedGroupID = ""
    @State private var showsCompactDetail = false
    @State private var inlineMessage: String?

    init(store: BrokerStore, claimGPU: @escaping () -> Void) {
        self.store = store
        self.claimGPU = claimGPU
#if DEBUG || DESKTOP_FIXTURES
        let requestedScope = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_USAGE_SCOPE"]
        let initialScope: ResourceUsageScope
        switch requestedScope {
        case "task": initialScope = .task
        default: initialScope = .project
        }
        _scope = State(initialValue: initialScope)
#else
        let initialScope: ResourceUsageScope = .project
#endif
        _projection = State(initialValue: ResourceUsageProjection(snapshot: store.snapshot, scope: initialScope))
    }

    private var snapshot: BrokerSnapshot { store.snapshot }

    private var groups: [ResourceUsageGroup] {
        projection.groups(for: scope)
    }

    private var selectedGroup: ResourceUsageGroup? {
        groups.first { $0.id == selectedGroupID } ?? groups.first
    }

    var body: some View {
        VStack(spacing: 0) {
            overviewBar
            Divider().opacity(DesignTokens.Alpha.strong)
            PersistedMasterDetailSplit(
                configuration: .ownership,
                showsCompactDetail: $showsCompactDetail,
                master: { groupNavigator.background(DesignTokens.surface) },
                detail: { groupDetail }
            )
        }
        .background(DesignTokens.surface)
        .onAppear { updateProjection() }
        .onChange(of: scope) { _, _ in updateProjection(resetSelection: true) }
        .onChange(of: store.snapshot.snapshotRevision) { _, _ in updateProjection() }
        // Labelling this container collapsed the whole page into one leaf and
        // repeated "使用情况" over every descendant.  The sidebar tab and the
        // visible page title already name it.
        .accessibilityElement(children: .contain)
    }

    private var overviewBar: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 18) { overviewCount; Spacer(minLength: 16) }
            .frame(height: 44)

            VStack(alignment: .leading, spacing: 7) {
                overviewCount
            }
            .padding(.vertical, 8)
        }
        .padding(.horizontal, 24)
        .background(DesignTokens.surface)
    }

    private var overviewCount: some View {
        Text("\(projection.activeTaskCount) 个当前任务  ·  \(projection.assignedQuantities.gpuCount + projection.runningQuantities.gpuCount) 张 GPU")
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(DesignTokens.ink)
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
    }

    private var groupNavigator: some View {
        VStack(alignment: .leading, spacing: 0) {
            Picker("查看方式", selection: $scope) {
                ForEach(ResourceUsageScope.visibleCases) { item in
                    Text(item.label).tag(item)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .accessibilityLabel("按项目或任务查看使用情况")

            HStack {
                Text("\(groups.count) 个\(scope.label)")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                Spacer()
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 7)

            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(groups) { group in
                        ResourceUsageGroupRow(
                            group: group,
                            selected: group.id == selectedGroup?.id
                        ) {
                            selectedGroupID = group.id
                            inlineMessage = nil
                            showsCompactDetail = true
                        }
                    }
                }
                .padding(.horizontal, 9)
                .padding(.bottom, 12)
            }
        }
    }

    @ViewBuilder
    private var groupDetail: some View {
        if let selectedGroup {
            ResourceUsageGroupDetail(
                store: store,
                group: selectedGroup,
                inlineMessage: inlineMessage,
                release: release
            )
            .id(selectedGroup.id)
        } else {
            VStack(spacing: 12) {
                ContentUnavailableView("当前没有资源分配。", systemImage: "square.stack.3d.up.slash")
                Button("申请 GPU", action: claimGPU)
                    .buttonStyle(PrimaryActionButtonStyle())
                    .disabled(!store.allowsMutations)
                    .help(store.allowsMutations ? "申请空闲 GPU" : store.mutationUnavailableReason)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .spatialContentSurface()
        }
    }

    private func ensureSelectedGroup(reset: Bool = false) {
        if reset || !groups.contains(where: { $0.id == selectedGroupID }) {
            selectedGroupID = groups.first?.id ?? ""
            inlineMessage = nil
        }
    }

    private func updateProjection(resetSelection: Bool = false) {
        projection = ResourceUsageProjection(snapshot: store.snapshot, scope: scope)
        ensureSelectedGroup(reset: resetSelection)
        if resetSelection {
            showsCompactDetail = false
        }
    }

    private func release(_ lease: LeaseRecord) {
        guard confirmLeaseRelease(lease) else { return }
        inlineMessage = nil
        store.releaseLease(lease) { success, error in
            if success {
                inlineMessage = "GPU 已释放。"
            } else {
                inlineMessage = error ?? "释放失败，请稍后重试。"
            }
        }
    }
}

private struct ResourceUsageGroupRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let group: ResourceUsageGroup
    let selected: Bool
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: 11) {
                Image(systemName: group.scope.icon)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .frame(width: 30, height: 30)
                    .background(
                        (selected ? DesignTokens.interaction : DesignTokens.mutedInk).opacity(DesignTokens.Alpha.fill),
                        in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
                    )
                VStack(alignment: .leading, spacing: 3) {
                    Text(group.title)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text(group.subtitle)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 8)
            }
            .padding(.horizontal, 10)
            .frame(height: 52)
            .background(
                selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.fill) : DesignTokens.ink.opacity(hovering ? 0.04 : 0),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .accessibilityLabel("\(group.scope.label) \(group.title)")
        .accessibilityValue("当前使用 \(combinedQuantities([group.assignedQuantities, group.runningQuantities]).compactLabel)")
    }
}

private struct ResourceUsageGroupDetail: View {
    @ObservedObject var store: BrokerStore
    let group: ResourceUsageGroup
    let inlineMessage: String?
    let release: (LeaseRecord) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                HStack(alignment: .top, spacing: 14) {
                    Image(systemName: group.scope.icon)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(DesignTokens.interaction)
                        .frame(width: 36, height: 36)
                        .background(
                            DesignTokens.interaction.opacity(DesignTokens.Alpha.fill),
                            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                        )
                    VStack(alignment: .leading, spacing: 2) {
                        Text(group.title)
                            .font(.title.weight(.semibold))
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(2)
                        Text(group.subtitle)
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    Spacer(minLength: 0)
                }

                if let inlineMessage {
                    Label(inlineMessage, systemImage: inlineMessage == "GPU 已释放。" ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(inlineMessage == "GPU 已释放。" ? DesignTokens.success : DesignTokens.danger)
                        .padding(11)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(
                            DesignTokens.surface,
                            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                                .strokeBorder(DesignTokens.surfaceStroke, lineWidth: 1)
                        )
                }

                // The header carried only a name and a task count, so a project
                // holding four tasks and a GPU read as emptier than it is.
                if group.assignedQuantities.hasResources
                    || group.runningQuantities.hasResources
                    || group.requestedQuantities.hasResources {
                    HomeCard(padding: 16) {
                        VStack(alignment: .leading, spacing: 10) {
                            CardSectionLabel(text: "资源合计")
                            ResourceStateSummary(
                                assigned: group.assignedQuantities,
                                running: group.runningQuantities,
                                requested: group.requestedQuantities
                            )
                        }
                    }
                }

                if !group.assignedClaims.isEmpty || !group.runningClaims.isEmpty || !group.leases.isEmpty {
                    ResourceUsageSection(title: "当前使用") {
                        VStack(spacing: 0) {
                            ForEach(Array(group.assignedClaims.enumerated()), id: \.element.id) { index, claim in
                                if index > 0 { ResourceUsageRowDivider() }
                                ResourceClaimDetailRow(claim: claim)
                            }
                            ForEach(Array(group.runningClaims.enumerated()), id: \.element.id) { index, claim in
                                if index > 0 || !group.assignedClaims.isEmpty { ResourceUsageRowDivider() }
                                ResourceClaimDetailRow(claim: claim)
                            }
                            ForEach(Array(group.leases.enumerated()), id: \.element.id) { index, lease in
                                if index > 0 || !group.assignedClaims.isEmpty || !group.runningClaims.isEmpty {
                                    ResourceUsageRowDivider()
                                }
                                ResourceLeaseDetailRow(
                                    store: store,
                                    lease: lease,
                                    release: { release(lease) }
                                )
                            }
                        }
                    }
                }

                if !group.actuals.isEmpty {
                    ResourceUsageSection(title: "任务记录") {
                        VStack(spacing: 0) {
                            ForEach(Array(group.actuals.enumerated()), id: \.element.id) { index, actual in
                                if index > 0 { ResourceUsageRowDivider() }
                                ResourceActualDetailRow(actual: actual)
                            }
                        }
                    }
                }

                if group.visibleActivityCount == 0 {
                    ContentUnavailableView("当前没有 GPU 使用记录", systemImage: "tray")
                }

            }
            .padding(20)
            .padding(.bottom, 40)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        // The page plane, so the section tiles above read as tiles.
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.ambientSmoke)
    }
}

private struct ResourceStateSummary: View {
    let assigned: ResourceQuantityRecord
    let running: ResourceQuantityRecord
    let requested: ResourceQuantityRecord

    var body: some View {
        HStack(spacing: 0) {
            if assigned.hasResources {
                ResourceStateSummaryItem(title: "已分配", quantities: assigned, icon: "checkmark.circle.fill")
            }
            if assigned.hasResources && (running.hasResources || requested.hasResources) {
                Divider().padding(.vertical, 9)
            }
            if running.hasResources {
                ResourceStateSummaryItem(title: "运行", quantities: running, icon: "play.circle.fill")
            }
            if running.hasResources && requested.hasResources {
                Divider().padding(.vertical, 9)
            }
            if requested.hasResources { EmptyView() }
            if !assigned.hasResources && !running.hasResources && !requested.hasResources {
                Text("暂无资源")
                    .font(Typography.metricLabel)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        // No fill or stroke of its own: this sits inside a HomeCard, and a
        // white box on a white card is a box in a box.
        .frame(height: 44)
    }
}

private struct ResourceStateSummaryItem: View {
    let title: String
    let quantities: ResourceQuantityRecord
    let icon: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(DesignTokens.mutedInk)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(Typography.metricLabel)
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(quantities.compactLabel)
                    .font(Typography.cardValue)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
            }
            Spacer(minLength: 0)
        }
        .padding(.trailing, 12)
        .frame(maxWidth: .infinity)
        .help(resourceUsageStatusHelp(title))
        // `.combine` concatenated each child's inherited AXHelp, so the help
        // sentence was announced once per descendant.  The label and value
        // below are explicit, so the children carry nothing extra.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(quantities.compactLabel)
    }
}

/// Rows of one section, stacked inside a single tile with hairline separators.
private struct ResourceUsageSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 10) {
                CardSectionLabel(text: title)
                content
            }
        }
    }
}

/// Hairline between two rows of a section; omitted before the first row.
private struct ResourceUsageRowDivider: View {
    var body: some View {
        Rectangle()
            .fill(DesignTokens.surfaceStroke)
            .frame(height: 1)
    }
}

private struct ResourceClaimDetailRow: View {
    let claim: ResourceClaimRecord

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "key.fill")
                VStack(alignment: .leading, spacing: 2) {
                    Text(claim.taskReference.isEmpty ? (claim.purpose ?? "未命名任务") : claim.taskReference)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(claim.projectID)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text(claim.quantities.compactLabel)
                    .font(Typography.annotation.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("当前使用 \(claim.taskReference)")
        .accessibilityValue("\(claim.projectID)，\(claim.quantities.compactLabel)")
    }
}

private struct ResourceLeaseDetailRow: View {
    @ObservedObject var store: BrokerStore
    @State private var showsReassignment = false
    let lease: LeaseRecord
    let release: () -> Void

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "square.stack.3d.up.fill")
                VStack(alignment: .leading, spacing: 2) {
                    Text(lease.taskReference ?? lease.purpose ?? "未命名任务")
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(lease.projectID)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text("\(lease.gpuIDs.count) GPU")
                    .font(Typography.annotation.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                Button {
                    showsReassignment = true
                } label: {
                    Label("改派", systemImage: "arrow.triangle.swap")
                        .font(.caption2.weight(.semibold))
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .disabled(
                    !store.allowsMutations
                        || store.reassigningLeaseIDs.contains(lease.id)
                        || lease.gpuIDs.isEmpty
                )
                .help(store.allowsMutations ? "选择同等数量的目标 GPU" : store.mutationUnavailableReason)
                Button(action: release) {
                    Label(store.releasingLeaseIDs.contains(lease.id) ? "释放中" : "释放", systemImage: "arrow.uturn.backward")
                        .font(.caption2.weight(.semibold))
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .disabled(!store.allowsMutations || store.releasingLeaseIDs.contains(lease.id))
                .help(store.allowsMutations ? "释放 GPU；不会停止任务" : store.mutationUnavailableReason)
            }
        }
        .accessibilityElement(children: .contain)
        .sheet(isPresented: $showsReassignment) {
            LeaseReassignmentSheet(store: store, lease: lease)
        }
    }
}

private struct LeaseReassignmentSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let lease: LeaseRecord
    @State private var selectedGPUIDs: Set<String>
    @State private var resultMessage: String?

    init(store: BrokerStore, lease: LeaseRecord) {
        self.store = store
        self.lease = lease
        _selectedGPUIDs = State(initialValue: Set(lease.gpuIDs))
    }

    private var candidates: [GPURecord] {
        store.snapshot.gpus
            .filter { lease.gpuIDs.contains($0.id) || $0.isPubliclyAvailable }
            .sorted { lhs, rhs in
                if lhs.endpointID == rhs.endpointID { return lhs.index < rhs.index }
                return lhs.endpointID < rhs.endpointID
            }
    }

    private var selectionIsComplete: Bool {
        selectedGPUIDs.count == lease.gpuIDs.count
    }

    private var selectionChanged: Bool {
        selectedGPUIDs != Set(lease.gpuIDs)
    }

    private var succeeded: Bool {
        resultMessage?.hasPrefix("分配已更新") == true
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "arrow.triangle.swap")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(DesignTokens.interaction)
                    .frame(width: 42, height: 42)
                    .background(DesignTokens.interaction.opacity(DesignTokens.Alpha.fill), in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text("改派 GPU")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("\(lease.projectID) · \(lease.taskReference ?? lease.purpose ?? "未命名任务")")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(2)
                }
            }

            Text("选择 \(lease.gpuIDs.count) 块目标 GPU。应用后，请让对应 Agent 按新分配的 CVD 重启任务。")
                .font(.callout.weight(.medium))
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: true)

            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(candidates) { gpu in
                        gpuOption(gpu)
                    }
                }
                .padding(1)
            }
            .frame(minHeight: 180, maxHeight: 360)

            if !selectionIsComplete {
                Text("还需选择 \(lease.gpuIDs.count - selectedGPUIDs.count) 块 GPU。")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(DesignTokens.warning)
            }

            if let resultMessage {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(succeeded ? DesignTokens.success : DesignTokens.danger)
                    Text(resultMessage)
                        .foregroundStyle(DesignTokens.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .font(.subheadline.weight(.medium))
                .padding(11)
                .background(
                    (succeeded ? DesignTokens.success : DesignTokens.danger).opacity(DesignTokens.Alpha.fill),
                    in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                )
            }

            HStack {
                Spacer()
                Button(succeeded ? "完成" : "取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                if !succeeded {
                    Button {
                        applyReassignment()
                    } label: {
                        Label(
                            store.reassigningLeaseIDs.contains(lease.id) ? "应用中" : "应用分配",
                            systemImage: "arrow.triangle.swap"
                        )
                    }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
                    .disabled(
                        !store.allowsMutations
                            || store.reassigningLeaseIDs.contains(lease.id)
                            || !selectionIsComplete
                            || !selectionChanged
                    )
                    .help(store.allowsMutations ? "应用选定的 GPU 分配" : store.mutationUnavailableReason)
                }
            }
        }
        .padding(28)
        .frame(width: 680, height: 620)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityLabel("改派 GPU")
    }

    private func gpuOption(_ gpu: GPURecord) -> some View {
        let selected = selectedGPUIDs.contains(gpu.id)
        let current = lease.gpuIDs.contains(gpu.id)
        return Button {
            resultMessage = nil
            if selected {
                selectedGPUIDs.remove(gpu.id)
            } else if selectedGPUIDs.count < lease.gpuIDs.count {
                selectedGPUIDs.insert(gpu.id)
            }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        Text("\(endpointName(for: gpu)) · GPU \(gpu.index)")
                            .font(.system(.callout, design: .monospaced).weight(.semibold))
                            .foregroundStyle(DesignTokens.ink)
                        if current {
                            Text("当前")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(DesignTokens.interaction)
                        }
                    }
                    Text(gpuPresentationLabel(gpu))
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                    Text(workspacePath(for: gpu))
                        .font(.system(.caption2, design: .monospaced).weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(11)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.fill) : DesignTokens.ink.opacity(DesignTokens.Alpha.hairline),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                    .stroke(selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.muted) : DesignTokens.surfaceStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(endpointName(for: gpu)) GPU \(gpu.index)")
        .accessibilityValue("\(gpuPresentationLabel(gpu))，工作区 \(workspacePath(for: gpu))，\(selected ? "已选择" : "未选择")")
    }

    private func endpointName(for gpu: GPURecord) -> String {
        store.snapshot.endpoint(id: gpu.endpointID)?.displayName ?? gpu.endpointID
    }

    private func workspacePath(for gpu: GPURecord) -> String {
        store.snapshot.endpoint(id: gpu.endpointID)?.workspacePath ?? "工作区未设置"
    }

    private func applyReassignment() {
        resultMessage = nil
        store.reassignLease(lease, gpuIDs: selectedGPUIDs.sorted()) { success, error in
            resultMessage = success
                ? "分配已更新；请让对应 Agent 按新 CVD 重启任务。"
                : (error ?? "分配更新失败。")
        }
    }
}

private struct ResourceActualDetailRow: View {
    let actual: ResourceRunActualRecord

    var body: some View {
        ResourceUsageRecordShell {
            HStack(spacing: 10) {
                ResourceRecordIcon(systemName: "checklist.checked")
                VStack(alignment: .leading, spacing: 2) {
                    Text(actual.taskReference.isEmpty ? actual.id : actual.taskReference)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    Text(actual.projectID)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 10)
                Text(actual.quantities.compactLabel)
                    .font(Typography.annotation.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                Text("时长 \(usageDuration(actual.actualDurationSeconds))")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("任务记录 \(actual.taskReference)")
        .accessibilityValue("\(actual.quantities.compactLabel)，时长 \(usageDuration(actual.actualDurationSeconds))")
    }
}

private struct ResourceUsageRecordShell<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        // Each row used to carry its own fill and stroke, so a section of six
        // read as six competing cards.  The rows now sit inside one HomeCard
        // and are separated by hairlines, the way a Home accessory list is.
        content
            .frame(minHeight: 40)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct ResourceRecordIcon: View {
    let systemName: String

    var body: some View {
        Image(systemName: systemName)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(DesignTokens.mutedInk)
            .frame(width: 26, height: 26)
            .background(
                DesignTokens.mutedInk.opacity(DesignTokens.Alpha.fill),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
    }
}

private struct ResourceUsageIdentity {
    let projectID: String
    let actorID: String
    let taskReference: String
}

private extension ResourceQuantityRecord {
    var hasResources: Bool {
        cpuCores > 0 || memoryMiB > 0 || gpuCount > 0 || nodeCount > 0 || schedulerUnits > 0
    }
}

private func resourceUsageStatusHelp(_ title: String) -> String {
    switch title {
    case "已分配": return "资源已归属，尚未检测到任务"
    case "运行": return "已检测到任务"
    default: return title
    }
}

private func resourceUsageRequestIsPending(_ request: AllocationRequestRecord) -> Bool {
    ["BLOCKED", "QUEUED", "PENDING_APPROVAL", "REQUESTED"].contains(request.state.uppercased())
}

private func resourceUsageReservationIsPending(_ reservation: ReservationRecord) -> Bool {
    reservation.state.uppercased() == "PENDING"
}

private func resourceUsageClaimStatus(_ claim: ResourceClaimRecord) -> String {
    if claim.runtimeState == "RUNNING" || claim.state == "RUNNING" { return "运行" }
    if ["HELD", "ACTIVE"].contains(claim.state) { return "已分配" }
    return claim.stateLabel
}

private func resourceUsageLeaseStatus(_ lease: LeaseRecord) -> String {
    if lease.runtimeState == "RUNNING" { return "运行" }
    if ["HELD", "ACTIVE"].contains(lease.state) { return "已分配" }
    return lease.stateLabel
}

private func resourceUsageActiveTaskCount(snapshot: BrokerSnapshot) -> Int {
    let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
    let activeClaims = snapshot.resourceClaims.filter {
        ["HELD", "ACTIVE", "RUNNING"].contains($0.state) || $0.runtimeState == "RUNNING"
    }
    let claimTasks = activeClaims.map {
        "\($0.projectID)\u{1F}\(normalizedTask($0.taskReference, purpose: $0.purpose))"
    }
    let legacyLeaseTasks = snapshot.leases
        .filter { !linkedLeaseIDs.contains($0.id) }
        .map {
            "\($0.projectID)\u{1F}\(normalizedTask($0.taskReference, purpose: $0.purpose))"
        }
    return Set(claimTasks + legacyLeaseTasks).count
}

private func resourceUsageIdentities(snapshot: BrokerSnapshot) -> [ResourceUsageIdentity] {
    var identities: [ResourceUsageIdentity] = []
    let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
    let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))
    identities.append(contentsOf: snapshot.resourceClaims.map {
        ResourceUsageIdentity(
            projectID: $0.projectID,
            actorID: $0.actorID,
            taskReference: normalizedTask($0.taskReference, purpose: $0.purpose)
        )
    })
    identities.append(contentsOf: snapshot.leases.filter { !linkedLeaseIDs.contains($0.id) }.map {
        ResourceUsageIdentity(
            projectID: $0.projectID,
            actorID: $0.actorID,
            taskReference: normalizedTask($0.taskReference, purpose: $0.purpose)
        )
    })
    identities.append(contentsOf: snapshot.requests.filter { !linkedRequestIDs.contains($0.id) }.map {
        ResourceUsageIdentity(
            projectID: $0.projectID,
            actorID: $0.actorID,
            taskReference: normalizedTask($0.taskReference)
        )
    })
    identities.append(contentsOf: snapshot.reservations.map {
        ResourceUsageIdentity(
            projectID: $0.projectID ?? "未标注项目",
            actorID: $0.actorID ?? "未标注 Agent",
            taskReference: normalizedTask($0.purpose)
        )
    })
    identities.append(contentsOf: snapshot.resourceRunActuals.map {
        ResourceUsageIdentity(projectID: $0.projectID, actorID: $0.actorID, taskReference: normalizedTask($0.taskReference))
    })
    return identities
}

private func makeResourceUsageGroups(snapshot: BrokerSnapshot, scope: ResourceUsageScope) -> [ResourceUsageGroup] {
    var buckets: [String: ResourceUsageBucket] = [:]
    let linkedLeaseIDs = Set(snapshot.resourceClaims.flatMap(\.nativeLeaseIDs))
    let linkedRequestIDs = Set(snapshot.resourceClaims.flatMap(\.nativeRequestIDs))

    func add(
        projectID: String,
        actorID: String,
        taskReference: String,
        update: (inout ResourceUsageBucket) -> Void
    ) {
        let key: String
        let title: String
        switch scope {
        case .project:
            key = projectID
            title = projectID
        case .agent:
            key = actorID
            title = actorID
        case .task:
            key = "\(projectID)\u{1F}\(taskReference)"
            title = taskReference
        }
        var bucket = buckets[key] ?? ResourceUsageBucket(key: key, title: title)
        bucket.projectIDs.insert(projectID)
        bucket.actorIDs.insert(actorID)
        bucket.taskReferences.insert(taskReference)
        update(&bucket)
        buckets[key] = bucket
    }

    for claim in snapshot.resourceClaims {
        add(
            projectID: claim.projectID,
            actorID: claim.actorID,
            taskReference: normalizedTask(claim.taskReference, purpose: claim.purpose)
        ) { $0.claims.append(claim) }
    }
    for lease in snapshot.leases where !linkedLeaseIDs.contains(lease.id) {
        add(
            projectID: lease.projectID,
            actorID: lease.actorID,
            taskReference: normalizedTask(lease.taskReference, purpose: lease.purpose)
        ) { $0.leases.append(lease) }
    }
    for request in snapshot.requests where !linkedRequestIDs.contains(request.id) {
        add(
            projectID: request.projectID,
            actorID: request.actorID,
            taskReference: normalizedTask(request.taskReference)
        ) { $0.requests.append(request) }
    }
    for reservation in snapshot.reservations {
        add(
            projectID: reservation.projectID ?? "未标注项目",
            actorID: reservation.actorID ?? "未标注 Agent",
            taskReference: normalizedTask(reservation.purpose)
        ) { $0.reservations.append(reservation) }
    }
    for actual in snapshot.resourceRunActuals {
        add(
            projectID: actual.projectID,
            actorID: actual.actorID,
            taskReference: normalizedTask(actual.taskReference)
        ) { $0.actuals.append(actual) }
    }

    return buckets.values.map { bucket in
        ResourceUsageGroup(
            id: "\(scope.rawValue):\(bucket.key)",
            scope: scope,
            title: bucket.title,
            projectIDs: bucket.projectIDs.sorted(),
            actorIDs: bucket.actorIDs.sorted(),
            taskReferences: bucket.taskReferences.sorted(),
            claims: bucket.claims.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") },
            leases: bucket.leases.sorted { ($0.issuedAt ?? "") > ($1.issuedAt ?? "") },
            requests: bucket.requests.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") },
            reservations: bucket.reservations.sorted { ($0.startsAt ?? "") > ($1.startsAt ?? "") },
            actuals: bucket.actuals.sorted { ($0.createdAt ?? "") > ($1.createdAt ?? "") }
        )
    }
    .filter { $0.visibleActivityCount > 0 }
    .sorted {
        if $0.visibleActivityCount != $1.visibleActivityCount {
            return $0.visibleActivityCount > $1.visibleActivityCount
        }
        return $0.title.localizedStandardCompare($1.title) == .orderedAscending
    }
}

private func combinedQuantities(_ values: [ResourceQuantityRecord]) -> ResourceQuantityRecord {
    ResourceQuantityRecord(
        cpuCores: values.reduce(0) { $0 + $1.cpuCores },
        memoryMiB: values.reduce(0) { $0 + $1.memoryMiB },
        gpuCount: values.reduce(0) { $0 + $1.gpuCount },
        nodeCount: values.reduce(0) { $0 + $1.nodeCount },
        schedulerUnits: values.reduce(0) { $0 + $1.schedulerUnits }
    )
}

private func normalizedTask(_ taskReference: String?, purpose: String? = nil) -> String {
    let task = taskReference?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    if !task.isEmpty { return task }
    let purposeText = purpose?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    return purposeText.isEmpty ? "未标注任务" : purposeText
}

private func usageCPUText(_ cores: Double) -> String {
    let rounded = (cores * 10).rounded() / 10
    return rounded == Double(Int(rounded)) ? "\(Int(rounded)) 核" : String(format: "%.1f 核", rounded)
}

private func usageMemoryText(_ mebibytes: Int) -> String {
    let gibibytes = Double(mebibytes) / 1024
    if gibibytes == Double(Int(gibibytes)) {
        return "\(Int(gibibytes)) GB"
    }
    return String(format: "%.1f GB", gibibytes)
}

private func usageTimestamp(_ value: String?) -> String {
    guard let value, !value.isEmpty else { return "未提供" }
    return value.replacingOccurrences(of: "T", with: " ").replacingOccurrences(of: "Z", with: "")
}

private func usageDuration(_ seconds: Int?) -> String {
    guard let seconds else { return "未记录" }
    if seconds < 60 { return "\(seconds) 秒" }
    if seconds < 3600 { return "\(seconds / 60) 分钟" }
    return String(format: "%.1f 小时", Double(seconds) / 3600)
}
