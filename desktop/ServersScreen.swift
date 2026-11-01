import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

enum EndpointFilter: String, CaseIterable, Identifiable {
    case all
    case available
    case taskOccupied
    case keepalive
    case connectionFailed

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "全部"
        case .available: return "有空闲"
        case .taskOccupied: return "使用中"
        case .keepalive: return "占卡中"
        case .connectionFailed: return "连接失败"
        }
    }
}


enum EndpointSort: String, CaseIterable, Identifiable {
    case attention
    case id
    case assignment
    case availableGPU
    case gpuModel
    case gpuUtilization
    case gpuMemory
    case cpuLoad
    case memory

    var id: String { rawValue }

    var label: String {
        switch self {
        case .attention: return "连接状态"
        case .id: return "服务器"
        case .assignment: return "任务"
        case .availableGPU: return "空闲"
        case .gpuModel: return "每张卡"
        case .gpuUtilization: return "GPU"
        case .gpuMemory: return "显存"
        case .cpuLoad: return "CPU"
        case .memory: return "内存"
        }
    }

    var defaultDirection: EndpointSortDirection {
        switch self {
        case .id, .assignment, .gpuModel: return .ascending
        default: return .descending
        }
    }
}


enum EndpointSortDirection: Equatable {
    case ascending
    case descending
}


struct ResourcesDashboard: View {
    @ObservedObject var store: BrokerStore
    /// nil is every server; "" is the servers outside any group.
    let groupID: String?
    @Binding var filter: EndpointFilter
    @State private var searchText = ""
    @State private var sort: EndpointSort = .id
    @State private var sortDirection: EndpointSortDirection = .ascending
    @State private var isSelectingForRemoval = false
    @State private var selectedEndpointIDs: Set<String> = []
    @State private var isRemovingEndpoints = false
    let claimEndpoint: (String) -> Void
    let manageGroups: () -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    private var endpoints: [EndpointRecord] { store.snapshot.operationalEndpoints }

    private var allocatableGPUCount: Int {
        guard store.freshness == .fresh else { return 0 }
        return scopedEndpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .flatMap { store.snapshot.gpus(for: $0) }
            .filter(\.isPubliclyAvailable)
            .count
    }

    private var scopedGPUCount: Int {
        scopedEndpoints.flatMap { store.snapshot.gpus(for: $0) }.count
    }

    private var attentionEndpoints: [EndpointRecord] {
        endpoints.filter { endpointRequiresAttention(endpoint: $0, gpus: store.snapshot.gpus(for: $0)) }
    }

    private var attentionGPUCount: Int {
        store.snapshot.operationalGPUs.filter { gpuNeedsAttention($0) }.count
    }

    private func matches(_ endpoint: EndpointRecord, _ filter: EndpointFilter) -> Bool {
        switch filter {
        case .all:
            true
        case .available:
            store.freshness == .fresh
                && endpoint.monitorStatus == "ONLINE"
                && (store.snapshot.gpus(for: endpoint).contains(where: \.isPubliclyAvailable)
                    || (endpoint.schedulerCapacity?.freeGPUCount ?? 0) > 0)
        case .taskOccupied:
            store.snapshot.gpus(for: endpoint).contains {
                ["RUNNING_MANAGED", "HELD", "LEASED_IDLE", "BUSY_UNMANAGED", "ORPHANED_BUSY", "CONFLICT"]
                    .contains($0.state)
            } || endpoint.nodes.contains { $0.allocatedGPUs > 0 }
        case .keepalive:
            endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning
        case .connectionFailed:
            ["ERROR", "STALE"].contains(endpoint.monitorStatus)
        }
    }

    private var scopedEndpoints: [EndpointRecord] {
        guard let groupID else { return endpoints }
        return endpoints.filter { (store.snapshot.serverGroup(for: $0)?.id ?? "") == groupID }
    }

    private var filteredEndpoints: [EndpointRecord] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return scopedEndpoints
            .filter { matches($0, filter) }
            .filter { endpoint in
                guard !query.isEmpty else { return true }
                let endpointLeases = leases(for: endpoint)
                return endpoint.id.lowercased().contains(query)
                    || endpoint.displayName.lowercased().contains(query)
                    || endpoint.host.lowercased().contains(query)
                    || endpoint.nodes.contains { $0.name.lowercased().contains(query) }
                    || endpoint.sshCommand.lowercased().contains(query)
                    || (endpoint.workspacePath?.lowercased().contains(query) ?? false)
                    || (store.snapshot.serverGroup(for: endpoint)?.displayName.lowercased().contains(query) ?? false)
                    || store.snapshot.gpus(for: endpoint).contains { $0.name.lowercased().contains(query) }
                    || endpointLeases.contains {
                        $0.projectID.lowercased().contains(query)
                            || ($0.taskReference ?? "").lowercased().contains(query)
                            || ($0.purpose ?? "").lowercased().contains(query)
                    }
            }
    }

    /// Selected endpoints still in the snapshot, so a server removed
    /// elsewhere drops out of the selection on the next refresh.
    private var selectedEndpoints: [EndpointRecord] {
        endpoints.filter { selectedEndpointIDs.contains($0.id) }
    }

    private var allFilteredSelected: Bool {
        !filteredEndpoints.isEmpty && filteredEndpoints.allSatisfy { selectedEndpointIDs.contains($0.id) }
    }

    private var tableSections: [EndpointOverviewSection] {
        let sorted = filteredEndpoints
        let groups = store.snapshot.serverGroups
        guard !groups.isEmpty else {
            return [EndpointOverviewSection(kind: .flat, endpoints: sorted)]
        }
        var sections: [EndpointOverviewSection] = []
        let orderedGroups = groups.sorted {
            $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending
        }
        for group in orderedGroups {
            let memberIDs = Set(store.snapshot.endpoints(inGroup: group.id).map(\.id))
            let members = sorted.filter { memberIDs.contains($0.id) }
            if !members.isEmpty {
                sections.append(EndpointOverviewSection(kind: .group(group), endpoints: members))
            }
        }
        let ungroupedIDs = Set(store.snapshot.ungroupedEndpoints.map(\.id))
        let ungrouped = sorted.filter { ungroupedIDs.contains($0.id) }
        if !ungrouped.isEmpty {
            sections.append(EndpointOverviewSection(kind: .ungrouped, endpoints: ungrouped))
        }
        return sections
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            controlBar
            ForEach(store.snapshot.pendingAllocations) { allocation in
                HStack {
                    Text("\(allocation.taskReference ?? "任务") · \(allocation.stateLabel)")
                        .font(Typography.annotation).foregroundStyle(DesignTokens.warning)
                    Spacer()
                    if let endpoint = store.snapshot.endpoint(id: allocation.endpointID) {
                        Button("查看申请") { openEndpoint(endpoint) }.buttonStyle(.link)
                    }
                }
            }
            if isSelectingForRemoval {
                removalSelectionBar
            }
            endpointTable
        }
        .padding(.horizontal, 20)
        .padding(.top, 2)
        .padding(.bottom, 12)
        .onChange(of: sort) { _, newSort in
            sortDirection = newSort.defaultDirection
        }
    }

    private var controlBar: some View {
        HStack(spacing: 8) {
            ForEach(EndpointFilter.allCases) { item in
                FilterChip(
                    label: item.label,
                    count: scopedEndpoints.filter { matches($0, item) }.count,
                    selected: filter == item
                ) { filter = item }
            }
            Spacer(minLength: 8)
            TextField("搜索服务器、GPU、项目或任务", text: $searchText)
                .textFieldStyle(.plain)
                .font(.body)
                .padding(.horizontal, 12)
                .frame(minWidth: 120, idealWidth: 230, maxWidth: 230, minHeight: 28, maxHeight: 28)
                .background(DesignTokens.ink.opacity(DesignTokens.Alpha.edge), in: Capsule())
                .accessibilityLabel("搜索服务器")
            Menu {
                Picker("排序", selection: $sort) {
                    ForEach(EndpointSort.allCases) { item in
                        Text(item.label).tag(item)
                    }
                }
            } label: {
                Image(systemName: "arrow.up.arrow.down")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("资源排序")
            .accessibilityLabel("资源排序")
            if store.supportsEndpointDelete {
                Button(action: toggleRemovalSelection) {
                    Image(systemName: isSelectingForRemoval ? "checkmark.circle.fill" : "checklist")
                }
                .buttonStyle(.borderless)
                .help(isSelectingForRemoval ? "退出批量移除" : "批量移除：勾选多台服务器，一次从 ServerPilot 移除")
                .accessibilityLabel(isSelectingForRemoval ? "完成批量移除" : "批量移除")
                .accessibilityIdentifier("endpoint-bulk-remove-mode")
            }
            Button(action: manageGroups) {
                Image(systemName: "rectangle.3.group")
            }
            .buttonStyle(.borderless)
            .help("管理服务器组")
            .accessibilityLabel("管理服务器组")
            .accessibilityIdentifier("manage-server-groups")
        }
    }

    private var endpointTable: some View {
        GeometryReader { proxy in
            // Columns fold from the right as the window narrows; the address
            // lane and all four pressure cells survive every tier.
            let tier = EndpointTableLayout.tier(width: proxy.size.width - EndpointTableLayout.scrollbarWidth)
            VStack(alignment: .leading, spacing: 0) {
                EndpointTableHeader(
                    tier: tier,
                    sort: sort,
                    direction: sortDirection,
                    selectSort: selectSort
                )
                .padding(.trailing, EndpointTableLayout.scrollbarWidth)
                EndpointTableDivider()
                if filteredEndpoints.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: endpoints.isEmpty ? "server.rack" : "magnifyingglass")
                            .font(.title2)
                            .foregroundStyle(DesignTokens.mutedInk)
                        Text(endpoints.isEmpty ? "暂无端点" : "没有匹配端点")
                            .font(Typography.sectionTitle)
                        Text(endpoints.isEmpty ? "添加服务器后会显示资源。" : "调整搜索或过滤条件。")
                            .font(Typography.secondary)
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityElement(children: .combine)
                } else {
                    ScrollView {
                        // One scroller for the whole fleet: a group is as long
                        // as it is, and the table fills the window.
                        LazyVStack(spacing: 0) {
                            ForEach(tableSections) { section in
                                if section.showsHeader {
                                    groupSectionHeader(section)
                                }
                                ForEach(sectionRows(section)) { row in
                                    switch row {
                                    case .endpoint(let endpoint):
                                        endpointRow(endpoint, tier: tier)
                                    case .node(let endpoint, let node):
                                        nodeRow(endpoint, node: node, tier: tier)
                                    }
                                    EndpointTableDivider()
                                }
                            }
                        }
                        .id("\(sort.rawValue)-\(sortDirection == .ascending)")
                        .background(TableScrollStyle(hideWhenUnused: true))
                    }
                }
                EndpointTableDivider()
                tableFooter
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .background(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous)
                    .fill(DesignTokens.surface)
            )
            // Increase Contrast asks for a drawn boundary; `tileStroke` is
            // fully transparent otherwise.
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous)
                    .strokeBorder(DesignTokens.tileStroke, lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous))
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        // Labelling this container turned it into one leaf element and hid
        // every row inside it.
        .accessibilityElement(children: .contain)
    }

    /// The totals sit where a Mac app keeps them, at the foot of the window's
    /// content, rather than taking a band above the table.
    private var tableFooter: some View {
        HStack(spacing: 14) {
            Text("\(scopedEndpoints.count) 台服务器")
            Text("\(scopedGPUCount) 张 GPU")
            Text("\(allocatableGPUCount) 张空闲")
            Text(fleetGPUModelSummary)
                .lineLimit(1)
                .truncationMode(.tail)
                .help(fleetGPUModelSummary)
            Spacer(minLength: 8)
            if store.freshness != .fresh {
                Label(snapshotTrustLabel, systemImage: "hand.raised.fill")
                    .foregroundStyle(DesignTokens.danger)
                    .help(attentionSummary)
            }
        }
        .font(Typography.annotation)
        .monospacedDigit()
        .foregroundStyle(DesignTokens.mutedInk)
        .padding(.horizontal, EndpointTableLayout.cardPadding)
        .frame(height: 30)
    }

    private var removalSelectionBar: some View {
        let selected = selectedEndpoints
        return HStack(spacing: 12) {
            Text("已选 \(selected.count) 台")
                .font(Typography.identity.weight(.semibold))
                .foregroundStyle(DesignTokens.ink)
                .monospacedDigit()
            Button(allFilteredSelected ? "取消全选" : "全选当前结果（\(filteredEndpoints.count) 台）") {
                if allFilteredSelected {
                    selectedEndpointIDs.subtract(filteredEndpoints.map(\.id))
                } else {
                    selectedEndpointIDs.formUnion(filteredEndpoints.map(\.id))
                }
            }
            .buttonStyle(.borderless)
            .disabled(filteredEndpoints.isEmpty)
            .accessibilityIdentifier("endpoint-bulk-select-all")
            Text("点击行勾选；可先用搜索或「连接失败」过滤再全选。")
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
            Spacer(minLength: 0)
            Button(selected.isEmpty ? "移除…" : "移除 \(selected.count) 台…", action: removeSelectedEndpoints)
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.danger, foreground: DesignTokens.onInteraction))
                .disabled(selected.isEmpty || isRemovingEndpoints || !store.allowsEndpointLifecycleMutations)
                .help(store.allowsEndpointLifecycleMutations
                    ? "从本机控制面移除所选服务器；不会停止远端进程"
                    : store.endpointLifecycleMutationUnavailableReason)
                .accessibilityIdentifier("endpoint-bulk-remove-action")
        }
    }

    private func toggleRemovalSelection() {
        isSelectingForRemoval.toggle()
        selectedEndpointIDs = []
    }

    private func toggleSelection(_ endpoint: EndpointRecord) {
        if selectedEndpointIDs.contains(endpoint.id) {
            selectedEndpointIDs.remove(endpoint.id)
        } else {
            selectedEndpointIDs.insert(endpoint.id)
        }
    }

    private func removeSelectedEndpoints() {
        let targets = selectedEndpoints
        guard !targets.isEmpty, confirmEndpointsDelete(targets) else { return }
        isRemovingEndpoints = true
        store.deleteEndpoints(targets) { result in
            isRemovingEndpoints = false
            selectedEndpointIDs.subtract(result.removedIDs)
            if result.failures.isEmpty {
                isSelectingForRemoval = false
            }
        }
    }

    private func sectionRows(_ section: EndpointOverviewSection) -> [EndpointOverviewRow] {
        section.endpoints.flatMap { endpoint -> [EndpointOverviewRow] in
            // Removal acts on servers, so selection shows one row per server
            // even where the table normally lists a cluster's nodes.
            if !endpoint.nodes.isEmpty && !isSelectingForRemoval {
                return visibleNodes(endpoint).map { .node(endpoint, $0) }
            }
            return [.endpoint(endpoint)]
        }.sorted { left, right in
            ResourceTableOrder.precedes(
                sortValue(left), sortValue(right),
                leftAddress: left.address, rightAddress: right.address,
                ascending: sortDirection == .ascending
            )
        }
    }

    private func visibleNodes(_ endpoint: EndpointRecord) -> [SchedulerNodeRecord] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let nodes = endpoint.nodes
        let matchingNode = !query.isEmpty && nodes.contains { $0.name.lowercased().contains(query) }
        return nodes.filter { node in
            (!matchingNode || node.name.lowercased().contains(query))
                && (filter != .available || (endpoint.monitorStatus == "ONLINE" && node.schedulable && node.freeGPUs > 0))
                && (filter != .taskOccupied || node.allocatedGPUs > 0)
        }
    }

    private func endpointRow(_ endpoint: EndpointRecord, tier: EndpointTableLayout.Tier) -> EndpointTableRow {
        EndpointTableRow(
            endpoint: endpoint, gpus: store.snapshot.gpus(for: endpoint),
            leases: leases(for: endpoint), group: store.snapshot.serverGroup(for: endpoint),
            isSnapshotFresh: store.freshness == .fresh, tier: tier,
            trend: store.utilizationTrends.servers[endpoint.id],
            removalMark: isSelectingForRemoval ? selectedEndpointIDs.contains(endpoint.id) : nil
        ) {
            if isSelectingForRemoval {
                toggleSelection(endpoint)
            } else {
                openEndpoint(endpoint)
            }
        }
    }

    private func nodeRow(_ endpoint: EndpointRecord, node: SchedulerNodeRecord, tier: EndpointTableLayout.Tier) -> SchedulerNodeTableRow {
        SchedulerNodeTableRow(
            openEndpoint: openEndpoint,
            endpoint: endpoint, node: node, gpuName: node.gpuName ?? "—",
            tier: tier, isSnapshotFresh: store.freshness == .fresh && endpoint.monitorStatus == "ONLINE" && endpoint.schedulerCapacity != nil,
            leases: leases(for: endpoint), store: store
        )
    }

    private func sortValue(_ row: EndpointOverviewRow) -> ResourceTableSortValue? {
        switch row {
        case .endpoint(let endpoint): return endpointRow(endpoint, tier: .compact).sortValue(for: sort)
        case .node(let endpoint, let node): return nodeRow(endpoint, node: node, tier: .compact).sortValue(for: sort)
        }
    }

    private func selectSort(_ newSort: EndpointSort) {
        if sort == newSort {
            sortDirection = sortDirection == .ascending ? .descending : .ascending
        } else {
            sortDirection = newSort.defaultDirection
            sort = newSort
        }
    }

    @ViewBuilder
    private func groupSectionHeader(_ section: EndpointOverviewSection) -> some View {
        switch section.kind {
        case .group(let group):
            EndpointGroupSectionHeader(
                title: group.displayName,
                summary: endpointGroupCapacitySummary(section.endpoints, group: group, store: store),
                connections: section.endpoints.filter { $0.connection.supported },
                setConnection: { store.setConnection($0, enabled: $1) }
            )
        case .ungrouped:
            EndpointGroupSectionHeader(
                title: "未分组的服务器",
                summary: endpointGroupCapacitySummary(section.endpoints, group: nil, store: store),
                actionTitle: "管理服务器组",
                action: manageGroups,
                connections: section.endpoints.filter { $0.connection.supported },
                setConnection: { store.setConnection($0, enabled: $1) }
            )
        case .flat:
            EmptyView()
        }
    }

    private func leases(for endpoint: EndpointRecord) -> [LeaseRecord] {
        let gpuIDs = Set(store.snapshot.gpus(for: endpoint).map(\.id))
        return store.snapshot.leases.filter { lease in
            !gpuIDs.isDisjoint(with: lease.gpuIDs) || lease.allocations.contains { $0.endpointID == endpoint.id }
                && !["RELEASED", "EXPIRED", "CANCELLED"].contains(lease.state)
        }
    }

    private var fleetGPUModelSummary: String {
        let groups = Dictionary(grouping: scopedEndpoints.flatMap { store.snapshot.gpus(for: $0) }, by: \.name)
        guard !groups.isEmpty else { return "未检测到 GPU" }
        let labels = groups.keys.sorted().map { name in
            "\(name) × \(groups[name]?.count ?? 0)"
        }
        if labels.count <= 3 { return labels.joined(separator: " · ") }
        return labels.prefix(3).joined(separator: " · ") + " · 另 \(labels.count - 3) 类"
    }

    private var snapshotTrustLabel: String {
        if store.freshness == .stale { return "连接已中断" }
        if store.freshness == .failed { return "暂无数据" }
        if store.snapshot.snapshotRevision != nil { return "数据已同步" }
        return "正在连接"
    }

    private var attentionSummary: String {
        if store.freshness != .fresh {
            return "当前显示上次数据。"
        }
        let attentionPrefix: String
        switch (attentionEndpoints.count, attentionGPUCount) {
        case (0, 0):
            attentionPrefix = "当前没有需要处理的资源"
        case (0, let gpuCount):
            attentionPrefix = "\(gpuCount) 块 GPU 需要处理"
        case (let endpointCount, 0):
            attentionPrefix = "\(endpointCount) 个端点需要处理"
        case (let endpointCount, let gpuCount):
            attentionPrefix = "\(endpointCount) 个端点、\(gpuCount) 块 GPU 需要处理"
        }
        return attentionPrefix
    }
}


/// A filter is a pill you can read the answer off: the count is already there.
private struct FilterChip: View {
    let label: String
    let count: Int
    let selected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 5) {
                Text(label)
                Text("\(count)").fontWeight(.semibold).monospacedDigit()
            }
            .font(Typography.identity.weight(.regular))
            .foregroundStyle(selected ? DesignTokens.onInteraction : DesignTokens.ink)
            .padding(.horizontal, 11)
            .frame(height: 28)
            .background(
                selected ? AnyShapeStyle(DesignTokens.ink) : AnyShapeStyle(DesignTokens.ink.opacity(DesignTokens.Alpha.edge)),
                in: Capsule()
            )
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .focusable()
        .fixedSize()
        .accessibilityLabel(label)
        .accessibilityValue("\(count) 台\(selected ? "，已选中" : "")")
    }
}

/// Column geometry for the server table, in three folding tiers.
///
/// A grid would buy width by giving up the one thing this page is for, comparing
/// four pressures down a column, so width is solved here instead: the address
/// lane keeps a hard floor at every viewport, and the two columns whose facts are
/// also in the row tooltip and the detail sheet fold away beneath it.  No tier
/// drops a pressure cell.

enum EndpointTableLayout {
    static let scrollbarWidth = NSScroller.scrollerWidth(for: .regular, scrollerStyle: .legacy)
    /// host:port at 13 pt.  The status word now has its own line beneath, so
    /// this no longer has to fit a whole SSH command.
    static let addressLane: CGFloat = 200
    static let cardPadding: CGFloat = 16
    /// Two lines: what it is, then what it is doing.
    static let rowHeight: CGFloat = 50
    static let headerHeight: CGFloat = 32

    enum Tier {
        /// Every column.
        case wide
        /// Without 每张卡.
        case medium
        /// Without 每张卡 and 任务.
        case compact

        var showsGPUModel: Bool { self == .wide }
        var showsAssignment: Bool { self != .compact }

        var columnSpacing: CGFloat { self == .wide ? 12 : 10 }
        var assignmentWidth: CGFloat { self == .wide ? 160 : 150 }
        /// Eight squares at 12 pt, and a vendor-qualified model name beneath.
        var gpuModelWidth: CGFloat { 170 }
        var availabilityWidth: CGFloat { 48 }

        /// A percentage above its bar.  The bar never disappears: a number that
        /// cannot be compared down a column is the defect this table exists to
        /// fix, and four metrics drawn two ways read as two classes of fact.
        var pressureWidth: CGFloat {
            switch self {
            case .wide: 72
            case .medium: 68
            case .compact: 64
            }
        }

        /// Content width this tier needs before the address lane would be squeezed.
        var minimumWidth: CGFloat {
            var total = EndpointTableLayout.addressLane + availabilityWidth + pressureWidth * 4
            var columns = 5
            if showsAssignment {
                total += assignmentWidth
                columns += 1
            }
            if showsGPUModel {
                total += gpuModelWidth
                columns += 1
            }
            return total + columnSpacing * CGFloat(columns - 1)
        }
    }

    static func tier(width: CGFloat) -> Tier {
        let content = width - cardPadding * 2
        if content >= Tier.wide.minimumWidth { return .wide }
        if content >= Tier.medium.minimumWidth { return .medium }
        return .compact
    }
}

/// Hairline between two rows of the table; omitted before the first row.

private struct EndpointTableDivider: View {
    var body: some View {
        Rectangle()
            .fill(DesignTokens.surfaceStroke)
            .frame(height: 1)
    }
}


private enum EndpointOverviewRow: Identifiable {
    case endpoint(EndpointRecord)
    case node(EndpointRecord, SchedulerNodeRecord)

    var id: String {
        switch self {
        case .endpoint(let endpoint): return "endpoint:\(endpoint.id)"
        case .node(let endpoint, let node): return "node:\(endpoint.id):\(node.name)"
        }
    }

    var address: String {
        switch self {
        case .endpoint(let endpoint): return endpoint.connectionAddress
        case .node(let endpoint, let node): return "\(endpoint.connectionAddress) / \(node.name)"
        }
    }
}

private struct EndpointOverviewSection: Identifiable {
    enum Kind {
        case flat
        case group(ServerGroupRecord)
        case ungrouped
    }

    let kind: Kind
    let endpoints: [EndpointRecord]

    var id: String {
        switch kind {
        case .flat: return "__flat__"
        case .group(let group): return group.id
        case .ungrouped: return "__ungrouped__"
        }
    }

    var showsHeader: Bool {
        switch kind {
        case .flat: return false
        case .group, .ungrouped: return true
        }
    }
}

/// A band that cuts the server table by group without turning rows into cards.

private struct EndpointGroupSectionHeader: View {
    let title: String
    let summary: String
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil

    let connections: [EndpointRecord]
    let setConnection: (EndpointRecord, Bool) -> Void

    var body: some View {
        HStack(spacing: 8) {
            Text(title)
                .font(Typography.label)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
            Text(summary)
                .font(Typography.annotation)
                .monospacedDigit()
                .foregroundStyle(DesignTokens.mutedInk)
            Spacer(minLength: 8)
            ForEach(connections) { endpoint in
                Menu(endpoint.connection.label) {
                    if let message = endpoint.connection.message { Text(message) }
                    Button("连接并自动维持") { setConnection(endpoint, true) }
                    if endpoint.connection.enabled {
                        Button("停止维持并断开连接") { setConnection(endpoint, false) }
                    }
                }
                .font(Typography.annotation)
                .fixedSize()
                .accessibilityLabel("\(endpoint.connectionAddress) 连接管理")
            }
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderless)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.interaction)
                    .fixedSize()
                    .focusable()
                    .accessibilityLabel(actionTitle)
            }
        }
        .padding(.horizontal, EndpointTableLayout.cardPadding)
        .padding(.top, 12)
        .padding(.bottom, 5)
        .accessibilityAddTraits(.isHeader)
        .modifier(EndpointGroupSectionHeaderAccessibility(
            title: title,
            summary: summary,
            combined: action == nil && connections.isEmpty
        ))
        .accessibilityIdentifier(action == nil ? "server-group-header" : "ungrouped-server-header")
    }
}


private struct EndpointGroupSectionHeaderAccessibility: ViewModifier {
    let title: String
    let summary: String
    let combined: Bool

    func body(content: Content) -> some View {
        if combined {
            content
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(title)
                .accessibilityValue(summary)
        } else {
            content.accessibilityElement(children: .contain)
        }
    }
}

/// The table's column headers, which are also its sort controls.

private struct EndpointTableHeader: View {
    let tier: EndpointTableLayout.Tier
    let sort: EndpointSort
    let direction: EndpointSortDirection
    let selectSort: (EndpointSort) -> Void

    var body: some View {
        HStack(spacing: tier.columnSpacing) {
            header(.id, trailing: false)
                .frame(minWidth: EndpointTableLayout.addressLane, maxWidth: .infinity, alignment: .leading)
            if tier.showsAssignment {
                header(.assignment, trailing: false)
                    .frame(width: tier.assignmentWidth, alignment: .leading)
            }
            if tier.showsGPUModel {
                header(.gpuModel, trailing: false)
                    .frame(width: tier.gpuModelWidth, alignment: .leading)
            }
            header(.availableGPU, trailing: true)
                .frame(width: tier.availabilityWidth, alignment: .trailing)
            header(.gpuUtilization, trailing: false)
                .frame(width: tier.pressureWidth, alignment: .leading)
            header(.gpuMemory, trailing: false)
                .frame(width: tier.pressureWidth, alignment: .leading)
            header(.cpuLoad, trailing: false)
                .frame(width: tier.pressureWidth, alignment: .leading)
            header(.memory, trailing: false)
                .frame(width: tier.pressureWidth, alignment: .leading)
        }
        .padding(.horizontal, EndpointTableLayout.cardPadding)
        .frame(height: EndpointTableLayout.headerHeight)
    }

    private func header(_ key: EndpointSort, trailing: Bool) -> some View {
        Button {
            selectSort(key)
        } label: {
            HStack(spacing: 4) {
                Text(key.label)
                    .font(Typography.annotation)
                    .lineLimit(1)
                if key == sort {
                    Image(systemName: direction == .ascending ? "chevron.up" : "chevron.down")
                        .font(Typography.annotation)
                }
            }
            .foregroundStyle(key == sort ? DesignTokens.ink : DesignTokens.mutedInk)
            .frame(
                maxWidth: .infinity,
                maxHeight: .infinity,
                alignment: trailing ? .trailing : .leading
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .help("按\(key.label)排序")
        // The headers this table used to have were unnamed to assistive
        // technology; the sort bar that stood in for them carried these names,
        // so they come back attached to the headers themselves.
        .accessibilityLabel("按\(key.label)排序")
        .accessibilityValue(key == sort ? (direction == .ascending ? "升序" : "降序") : "未使用")
    }
}

/// One pressure column: the percentage above its bar.  Where a recent line
/// exists it stands in for the bar, since it says the same thing and more.

struct TablePressureCell: View {
    let label: String
    let fraction: Double?
    let width: CGFloat
    var trend: UtilizationSeries? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(percentageLabel(fraction))
                .font(Typography.identity)
                .monospacedDigit()
                .foregroundStyle(pressureTextColor(fraction))
                .lineLimit(1)
            if let trend, trend.hasData {
                Sparkline(points: trend.points, color: pressureColor(fraction))
                    .frame(height: 14)
            } else {
                PressureMeter(fraction: fraction, color: pressureColor(fraction), height: 4)
            }
        }
        .frame(width: width, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(percentageLabel(fraction))
    }
}


private struct EndpointTableRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let endpoint: EndpointRecord
    let gpus: [GPURecord]
    let leases: [LeaseRecord]
    let group: ServerGroupRecord?
    let isSnapshotFresh: Bool
    let tier: EndpointTableLayout.Tier
    let trend: UtilizationSeries?
    /// nil outside bulk removal; otherwise whether this row is checked.
    let removalMark: Bool?
    let select: () -> Void

    func sortValue(for field: EndpointSort) -> ResourceTableSortValue? {
        switch field {
        case .id: return .text(endpoint.connectionAddress)
        case .assignment: return assignmentLine == "—" ? nil : .text(assignmentLine)
        case .gpuModel: return .text(gpuModelLine)
        case .availableGPU: return claimableLabel == nil ? nil : .number(Double(availableGPUCount))
        case .gpuUtilization: return gpuPressure.map(ResourceTableSortValue.number)
        case .gpuMemory: return gpuMemoryPressure.map(ResourceTableSortValue.number)
        case .cpuLoad: return cpuLoadPressure.map(ResourceTableSortValue.number)
        case .memory: return memoryPressure.map(ResourceTableSortValue.number)
        case .attention:
            let rank = (endpointNeedsAttention(endpoint) ? 3 : 0)
                + (gpus.contains { gpuNeedsAttention($0) } ? 2 : 0)
                + (endpointHighPressure(endpoint: endpoint, gpus: gpus) ? 1 : 0)
            return .number(Double(rank))
        }
    }

    var body: some View {
        Button(action: select) {
            HStack(spacing: tier.columnSpacing) {
                if let checked = removalMark {
                    Image(systemName: checked ? "checkmark.circle.fill" : "circle")
                        .font(.body)
                        .foregroundStyle(checked ? DesignTokens.danger : DesignTokens.mutedInk)
                }
                serverCell
                    .frame(minWidth: EndpointTableLayout.addressLane, maxWidth: .infinity, alignment: .leading)
                if tier.showsAssignment {
                    assignmentCell.frame(width: tier.assignmentWidth, alignment: .leading)
                }
                if tier.showsGPUModel {
                    gpuModelCell.frame(width: tier.gpuModelWidth, alignment: .leading)
                }
                availabilityCell.frame(width: tier.availabilityWidth, alignment: .trailing)
                TablePressureCell(label: "GPU 利用率", fraction: gpuPressure, width: tier.pressureWidth, trend: trend)
                TablePressureCell(label: "显存占用率", fraction: gpuMemoryPressure, width: tier.pressureWidth)
                TablePressureCell(label: "CPU 负载", fraction: cpuLoadPressure, width: tier.pressureWidth)
                TablePressureCell(label: "内存占用率", fraction: memoryPressure, width: tier.pressureWidth)
            }
            .padding(.horizontal, EndpointTableLayout.cardPadding)
            .frame(height: EndpointTableLayout.rowHeight)
            .background(DesignTokens.ink.opacity(hovering ? DesignTokens.Alpha.hairline : 0))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        // Everything a two-line row cannot print — the workspace path, per-card
        // VRAM, the full lease list — is one hover away, and all of it is in
        // the detail sheet a click away.
        .help(removalMark == nil ? tooltip : "点击勾选或取消勾选")
        .accessibilityElement(children: .ignore)
        // Without this the row reports as AXUnknown, so assistive technology
        // reads its label but cannot say it is actionable.
        .accessibilityAddTraits(removalMark == true ? [.isButton, .isSelected] : .isButton)
        // `.ignore` replaces the button's own element, press action included.
        .accessibilityAction { select() }
        .accessibilityIdentifier("endpoint-row-\(endpoint.id)")
        .accessibilityLabel("服务器 \(endpoint.sshCommand)")
        .accessibilityValue(accessibilityValue)
    }

    /// The row's identity: host:port, then the one word that answers "can I ask
    /// this machine for a GPU".
    private var serverCell: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(endpoint.connectionAddress)
                    .font(Typography.rowPrimary)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                if endpoint.workspacePath == nil {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.warning)
                        .help("工作区未设置")
                }
            }
            HStack(spacing: 5) {
                Circle()
                    .fill(status.color)
                    .frame(width: 7, height: 7)
                Text(status.word)
                    .font(Typography.annotation)
                    .foregroundStyle(status.color)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
    }

    private var assignmentCell: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(assignmentPrimary)
                .font(Typography.identity)
                .foregroundStyle(assignmentIsUnassigned ? DesignTokens.mutedInk : DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.tail)
            if let assignmentSecondary {
                Text(assignmentSecondary)
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(assignmentHelp)
    }

    private var gpuModelCell: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !gpus.isEmpty {
                CardSquares(gpus: gpus.sorted { $0.index < $1.index }, size: 11)
                    .fixedSize()
            }
            Text(gpuModelLine)
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
                .truncationMode(.tail)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .help(gpuModelDetail)
    }

    /// The count that decides whether this machine is worth clicking.  A
    /// percentage cannot play this role — a lightly loaded but leased card is
    /// not claimable, and the contract forbids deriving availability from
    /// capacity minus usage.
    private var availabilityCell: some View {
        HStack(alignment: .firstTextBaseline, spacing: 1) {
            Spacer(minLength: 0)
            if let claimable = claimableLabel {
                Text(claimable)
                    .font(Font.title3.weight(.semibold).monospacedDigit())
                    .foregroundStyle(availabilityTint)
                Text("/\(gpus.count)")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
            } else {
                Text(availabilityLabel)
                    .font(Typography.rowValue)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("空闲 GPU")
        .accessibilityValue(availabilityAccessibilityValue)
    }

    // MARK: - Values

    /// Non-nil only when the count is trustworthy.  "—" and "未确认" are words,
    /// not numbers, and must never be set in the number size.
    private var claimableLabel: String? {
        guard !gpus.isEmpty, isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return nil }
        return "\(availableGPUCount)"
    }

    /// A conflicted GPU used to earn a second line under the count; a 44 pt row
    /// has no second line, so the count itself carries the warning.
    private var availabilityTint: Color {
        if conflictedGPUCount > 0 { return DesignTokens.warning }
        return availableGPUCount > 0 ? DesignTokens.ink : DesignTokens.mutedInk
    }

    private var availabilityAccessibilityValue: String {
        if legacyWorkloadReviewGPUCount > 0 {
            return "\(availabilityLabel)，\(legacyWorkloadReviewGPUCount) 张任务归属待核对"
        }
        if conflictedGPUCount > 0 {
            return "\(availabilityLabel)，\(conflictedGPUCount) 张 GPU 状态需要处理"
        }
        return availabilityLabel
    }

    private var assignmentLine: String {
        assignmentTitle == "—" ? "—" : "\(assignmentTitle) · \(assignmentDetail)"
    }

    /// The task first: it is what a person looks for, and it is usually the
    /// only part of a long lease name that tells two rows apart.
    private var assignmentPrimary: String {
        primaryLease == nil ? assignmentTitle : assignmentDetail
    }

    private var assignmentSecondary: String? {
        if let primaryLease { return "\(primaryLease.holderLabel) · \(primaryLease.projectID)" }
        if assignmentTitle == "—" { return nil }
        return assignmentDetail
    }

    /// A GPU host names its hardware.  A scheduler endpoint names the pool
    /// model, not a local inventory count.  A CPU node names how much machine
    /// it is, because "无 GPU" would only repeat the status word beside the command.
    private var gpuModelLine: String {
        if !gpus.isEmpty { return "\(gpuModelSummary) × \(gpus.count)" }
        if let name = endpoint.schedulerCapacity?.gpuName { return name }
        return hostScale
    }

    private var status: ServerStatus {
        ServerStatus(endpoint: endpoint, gpus: gpus, group: group, isSnapshotFresh: isSnapshotFresh)
    }

    private var isOnDemandEndpoint: Bool {
        endpoint.schedulerCapacity != nil || group?.allocation == .delegated
    }

    private var primaryLease: LeaseRecord? {
        leases.first(where: { $0.runtimeState == "RUNNING" }) ?? leases.first
    }

    private var hasUnattributedWorkload: Bool {
        gpus.contains { ["BUSY_UNMANAGED", "CONFLICT", "ORPHANED_BUSY"].contains($0.state) }
    }

    private var assignmentIsUnassigned: Bool {
        primaryLease == nil && !endpoint.keepalive.isActive && !endpoint.keepalive.isTransitioning
    }

    private var assignmentTitle: String {
        if let primaryLease { return primaryLease.projectID }
        if endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning { return "空闲 · 占卡中" }
        if hasUnattributedWorkload { return "外部进程" }
        if !isSnapshotFresh { return "任务未确认" }
        return "—"
    }

    private var assignmentDetail: String {
        if let primaryLease {
            let task = primaryLease.taskReference ?? primaryLease.purpose ?? "未命名任务"
            let extra = leases.count > 1 ? " · 另 \(leases.count - 1) 项" : ""
            return "\(task)\(extra)"
        }
        if endpoint.keepalive.isActive {
            return "\(gpus.filter { $0.state == "KEEPALIVE" }.count) 张 GPU"
        }
        if hasUnattributedWorkload { return "服务器上检测到任务" }
        if !isSnapshotFresh { return "显示上次数据" }
        return gpus.isEmpty ? "无 GPU 任务" : "暂无运行任务"
    }

    private var assignmentHelp: String {
        if endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning {
            return "\(assignmentTitle) · \(assignmentDetail)"
        }
        guard !leases.isEmpty else { return "\(assignmentTitle) · \(assignmentDetail)" }
        return leases.map { lease in
            let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
            return "\(lease.projectID) · \(task) · \(lease.gpuCount) GPU"
        }.joined(separator: "\n")
    }

    private var availableGPUCount: Int {
        guard isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
    }

    private var gpuPressure: Double? {
        isSnapshotFresh ? endpoint.resourceNodes.first(where: { $0.id == "self" })?.gpuUtilizationFraction : nil
    }

    private var gpuMemoryPressure: Double? {
        isSnapshotFresh ? endpoint.resourceNodes.first(where: { $0.id == "self" })?.gpuMemoryFraction : nil
    }

    private var cpuLoadPressure: Double? {
        isSnapshotFresh ? endpoint.resourceNodes.first(where: { $0.id == "self" })?.cpuUtilizationFraction : nil
    }

    private var memoryPressure: Double? {
        isSnapshotFresh ? endpoint.resourceNodes.first(where: { $0.id == "self" })?.memoryFraction : nil
    }

    private var gpuCaption: String {
        if !gpus.isEmpty {
            return isSnapshotFresh ? "\(availableGPUCount)/\(gpus.count) 空闲" : "状态未确认"
        }
        return isOnDemandEndpoint ? "按需申请" : "无 GPU"
    }

    private var availabilityLabel: String {
        if gpus.isEmpty {
            return isOnDemandEndpoint ? "按需" : "—"
        }
        guard isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return "未确认" }
        return "\(availableGPUCount)/\(gpus.count)"
    }

    private var conflictedGPUCount: Int {
        gpus.filter { $0.state == "CONFLICT" }.count
    }

    private var legacyWorkloadReviewGPUCount: Int {
        gpus.filter(gpuHasLegacyWorkloadProcessReview).count
    }

    private var nonWorkloadConflictGPUCount: Int {
        conflictedGPUCount - legacyWorkloadReviewGPUCount
    }

    private var gpuModelSummary: String {
        endpointGPUModelSummary(gpus)
    }

    private var gpuModelDetail: String {
        if !gpus.isEmpty {
            let groups = Dictionary(grouping: gpus, by: \.name)
            return groups.keys.sorted().map { "\($0) × \(groups[$0]?.count ?? 0)" }.joined(separator: "\n")
        }
        if let name = endpoint.schedulerCapacity?.gpuName { return name }
        return "未检测到 GPU"
    }

    /// A CPU node's identity: how much machine this endpoint actually owns.
    private var hostScale: String {
        var parts: [String] = []
        if let cores = endpoint.cpuCores {
            parts.append(scopedFact(ResourceText.cores(cores), note: endpoint.cpuScopeNote))
        }
        if let total = endpoint.memoryTotalMiB, total > 0 {
            parts.append(scopedFact("\(ResourceText.memory(total)) 内存", note: endpoint.memoryScopeNote))
        }
        return parts.isEmpty ? "无主机遥测" : parts.joined(separator: " · ")
    }

    private var hostFacts: String {
        var facts: [String] = []
        if let cores = endpoint.cpuCores {
            facts.append(scopedFact(ResourceText.cores(cores), note: endpoint.cpuScopeNote))
        }
        if let total = endpoint.memoryTotalMiB, total > 0 {
            facts.append(scopedFact("\(ResourceText.memory(total)) 内存", note: endpoint.memoryScopeNote))
        }
        if let peak = gpus.compactMap(\.temperature).max() { facts.append("最高 \(peak) °C") }
        return facts.isEmpty ? "无主机遥测" : facts.joined(separator: " · ")
    }

    private var vramSummary: String? {
        let used = gpus.compactMap(\.memoryUsedMiB)
        guard !used.isEmpty else { return nil }
        let total = gpus.reduce(0) { $0 + $1.totalVRAMMiB }
        guard total > 0 else { return nil }
        return String(format: "%.0f / %.0f GB", Double(used.reduce(0, +)) / 1024, Double(total) / 1024)
    }

    private var attentionLabel: String {
        if !isSnapshotFresh { return "不可分配" }
        if endpointNeedsAttention(endpoint) { return endpoint.monitorLabel }
        if nonWorkloadConflictGPUCount > 0 { return "不可分配" }
        if availableGPUCount > 0, legacyWorkloadReviewGPUCount > 0 {
            return "\(availableGPUCount) 张可申请 · \(legacyWorkloadReviewGPUCount) 张任务待核对"
        }
        if gpus.contains(where: { ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains($0.state) }) { return "外部进程" }
        if legacyWorkloadReviewGPUCount > 0 { return "任务归属待核对" }
        if gpus.contains(where: gpuNeedsAttention) { return "不可分配" }
        if endpointHighPressure(endpoint: endpoint, gpus: gpus) { return "压力较高" }
        return endpoint.monitorLabel
    }

    private var tooltip: String {
        var lines = [endpoint.sshCommand, attentionLabel, gpuModelDetail]
        if let group {
            lines.append("服务器组 \(group.displayName)")
        }
        if let vramSummary { lines.append("显存 \(vramSummary)") }
        lines.append(hostFacts)
        lines.append(endpoint.workspacePath ?? "工作区未设置")
        lines.append(assignmentHelp)
        return lines.joined(separator: "\n")
    }

    private var accessibilityValue: String {
        "\(status.word)，\(assignmentHelp)，GPU 配置 \(gpuModelSummary)，\(gpuCaption)，资源指标为节点当前值：CPU 负载 \(percentageLabel(cpuLoadPressure))，内存占用率 \(percentageLabel(memoryPressure))，GPU 利用率 \(percentageLabel(gpuPressure))"
    }
}


private struct PressureMeter: View {
    let fraction: Double?
    let color: Color
    /// An outer `.frame(height:)` cannot thin this — the inner frame fixes the
    /// capsule and the bar simply overflows its slot — so the height is a
    /// parameter.  Table rows ask for 4; the detail sheet keeps the full 8.
    var height: CGFloat = 8

    private var normalizedFraction: CGFloat {
        CGFloat(min(max(fraction ?? 0, 0), 1))
    }

    var body: some View {
        GeometryReader { proxy in
            Capsule()
                .fill(DesignTokens.ink.opacity(DesignTokens.Alpha.edge))
                .overlay(alignment: .leading) {
                    Capsule()
                        .fill(color)
                        .frame(width: normalizedFraction > 0 ? max(proxy.size.width * normalizedFraction, 3) : 0)
                }
        }
        .frame(height: height)
        .accessibilityHidden(true)
    }
}


