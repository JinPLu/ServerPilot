import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

private enum EndpointFilter: String, CaseIterable, Identifiable {
    case all
    case available
    case taskOccupied
    case keepalive
    case connectionFailed

    var id: String { rawValue }

    var label: String {
        switch self {
        case .all: return "全部"
        case .available: return "有空闲 GPU"
        case .taskOccupied: return "任务占用"
        case .keepalive: return "占卡"
        case .connectionFailed: return "连接失败"
        }
    }
}


private enum EndpointSort: String, CaseIterable, Identifiable {
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
        case .id: return "SSH 连接"
        case .assignment: return "项目 / 任务"
        case .availableGPU: return "空闲 GPU"
        case .gpuModel: return "GPU 配置"
        case .gpuUtilization: return "GPU 利用率"
        case .gpuMemory: return "显存占用率"
        case .cpuLoad: return "CPU 负载"
        case .memory: return "内存占用率"
        }
    }

    var defaultDirection: EndpointSortDirection {
        switch self {
        case .id, .assignment, .gpuModel: return .ascending
        default: return .descending
        }
    }
}


private enum EndpointSortDirection: Equatable {
    case ascending
    case descending
}


struct ResourcesDashboard: View {
    @ObservedObject var store: BrokerStore
    @State private var searchText = ""
    @State private var filter: EndpointFilter = .all
    @State private var sort: EndpointSort = .id
    @State private var sortDirection: EndpointSortDirection = .ascending
    let claimEndpoint: (String) -> Void
    let manageGroups: () -> Void
    let openEndpoint: (EndpointRecord) -> Void
    let selectGPU: (GPURecord) -> Void

    private var endpoints: [EndpointRecord] { store.snapshot.operationalEndpoints }

    private var onlineEndpointCount: Int {
        endpoints.filter { $0.monitorStatus == "ONLINE" }.count
    }

    private var allocatableGPUCount: Int {
        guard store.freshness == .fresh else { return 0 }
        return endpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .flatMap { store.snapshot.gpus(for: $0) }
            .filter(\.isPubliclyAvailable)
            .count
    }

    private var attentionEndpoints: [EndpointRecord] {
        endpoints.filter { endpointRequiresAttention(endpoint: $0, gpus: store.snapshot.gpus(for: $0)) }
    }

    private var attentionGPUCount: Int {
        store.snapshot.operationalGPUs.filter { gpuNeedsAttention($0) }.count
    }

    private var filteredEndpoints: [EndpointRecord] {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return endpoints
            .filter { endpoint in
                switch filter {
                case .all:
                    true
                case .available:
                    store.freshness == .fresh
                        && endpoint.monitorStatus == "ONLINE"
                        && store.snapshot.gpus(for: endpoint).contains(where: \.isPubliclyAvailable)
                case .taskOccupied:
                    store.snapshot.gpus(for: endpoint).contains {
                        ["BUSY_UNMANAGED", "ORPHANED_BUSY", "CONFLICT"].contains($0.state)
                    }
                case .keepalive:
                    endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning
                case .connectionFailed:
                    ["ERROR", "STALE"].contains(endpoint.monitorStatus)
                }
            }
            .filter { endpoint in
                guard !query.isEmpty else { return true }
                let endpointLeases = leases(for: endpoint)
                return endpoint.id.lowercased().contains(query)
                    || endpoint.displayName.lowercased().contains(query)
                    || endpoint.host.lowercased().contains(query)
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
            .sorted(by: endpointSort)
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
        VStack(spacing: 0) {
            resourceSummary
            Divider().opacity(DesignTokens.Alpha.strong)
            endpointTable
                .background(DesignTokens.surface)
        }
        .onChange(of: sort) { _, newSort in
            sortDirection = newSort.defaultDirection
        }
    }

    private var resourceSummary: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 16) {
                summaryStatus
                Divider().frame(height: 24)
                gpuInventorySummary
                Spacer(minLength: 12)
                if store.freshness != .fresh { snapshotTrustSummary }
            }
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    summaryStatus
                    Spacer(minLength: 8)
                    if store.freshness != .fresh { snapshotTrustSummary }
                }
                gpuInventorySummary
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(DesignTokens.surface)
    }

    private var summaryStatus: some View {
        HStack(spacing: 14) {
            ResourceInlineStat(value: "\(endpoints.count)", label: "台服务器", color: DesignTokens.ink)
            ResourceInlineStat(value: "\(store.snapshot.operationalGPUs.count)", label: "张 GPU", color: DesignTokens.ink)
            ResourceInlineStat(value: "\(allocatableGPUCount)", label: "张空闲", color: DesignTokens.success)
        }
    }

    private var gpuInventorySummary: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("GPU 型号")
                .font(Typography.metricLabel)
                .foregroundStyle(DesignTokens.mutedInk)
            Text(fleetGPUModelSummary)
                .font(Typography.metricValue)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.tail)
                .help(fleetGPUModelSummary)
        }
    }

    private var snapshotTrustSummary: some View {
        Label(snapshotTrustLabel, systemImage: store.freshness == .fresh ? "checkmark.circle.fill" : "hand.raised.fill")
            .font(Typography.annotation)
            .foregroundStyle(store.freshness == .fresh ? DesignTokens.mutedInk : DesignTokens.danger)
            .lineLimit(1)
            .help(attentionSummary)
    }

    private var endpointTable: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                TextField("搜索 SSH、GPU、项目、任务或服务器组", text: $searchText)
                    .textFieldStyle(.roundedBorder)
                    .font(Typography.identity)
                    .frame(maxWidth: 320)
                    .accessibilityLabel("搜索端点")
                Picker("过滤", selection: $filter) {
                    ForEach(EndpointFilter.allCases) { item in
                        Text(item.label).tag(item)
                    }
                }
                .pickerStyle(.segmented)
                // The visible label wrapped to two stacked glyphs at 1440; the
                // accessible name below carries it instead.
                .labelsHidden()
                .frame(maxWidth: 430)
                .accessibilityLabel("端点过滤")

                Label("资源指标：近 10 分钟均值", systemImage: "clock")
                    .font(Typography.metricLabel)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .help("GPU、显存、CPU 负载和内存占用率按最近 10 分钟的观测均值展示。点开服务器可查看当前观测。")

                Spacer(minLength: 0)

                Menu {
                    Picker("排序", selection: $sort) {
                        ForEach(EndpointSort.allCases) { item in
                            Text(item.label).tag(item)
                        }
                    }
                } label: {
                    Label("排序", systemImage: "arrow.up.arrow.down")
                        .font(Typography.identity)
                }
                .menuStyle(.borderlessButton)
                .help("资源排序")
                .accessibilityLabel("资源排序")

                Button(action: manageGroups) {
                    Label("服务器组", systemImage: "rectangle.3.group")
                        .font(Typography.identity)
                }
                .buttonStyle(.borderless)
                .help("管理服务器组")
                .accessibilityLabel("管理服务器组")
                .accessibilityIdentifier("manage-server-groups")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 11)

            GeometryReader { proxy in
                // Columns fold from the right as the window narrows; the SSH
                // lane and all four pressure bars survive every tier.
                let tier = EndpointTableLayout.tier(width: proxy.size.width)
                VStack(alignment: .leading, spacing: 0) {
                    EndpointTableHeader(
                        tier: tier,
                        sort: sort,
                        direction: sortDirection,
                        selectSort: selectSort
                    )
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
                            LazyVStack(spacing: 0) {
                                ForEach(Array(tableSections.enumerated()), id: \.element.id) { sectionIndex, section in
                                    if section.showsHeader {
                                        if sectionIndex > 0 { EndpointTableDivider() }
                                        groupSectionHeader(section)
                                        EndpointTableDivider()
                                    } else if sectionIndex > 0 {
                                        EndpointTableDivider()
                                    }
                                    ForEach(Array(section.endpoints.enumerated()), id: \.element.id) { index, endpoint in
                                        if index > 0 { EndpointTableDivider() }
                                        EndpointTableRow(
                                            endpoint: endpoint,
                                            gpus: store.snapshot.gpus(for: endpoint),
                                            leases: leases(for: endpoint),
                                            group: store.snapshot.serverGroup(for: endpoint),
                                            isSnapshotFresh: store.freshness == .fresh,
                                            tier: tier
                                        ) {
                                            openEndpoint(endpoint)
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .background(
                    RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous)
                        .fill(DesignTokens.surface)
                )
                // Increase Contrast asks for a drawn boundary; `tileStroke` is
                // fully transparent otherwise, so the resting card keeps its
                // outline-free Home look.
                .overlay(
                    RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous)
                        .strokeBorder(DesignTokens.tileStroke, lineWidth: 1)
                )
                .clipShape(RoundedRectangle(cornerRadius: DesignTokens.Radius.tile, style: .continuous))
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            // Labelling this container turned it into one leaf element and hid
            // every row inside it.  The page summary strip above is visible
            // text and already carries the same overview.
            .accessibilityElement(children: .contain)
            .padding(.horizontal, 20)
            .padding(.top, 12)
            .padding(.bottom, 12)
            // The page plane, painted here rather than inherited: the white
            // table only reads as a card against the one visible elevation step.
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .background(DesignTokens.ambientSmoke)
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
                summary: endpointGroupCapacitySummary(section.endpoints, group: group, store: store)
            )
        case .ungrouped:
            EndpointGroupSectionHeader(
                title: "未分组的服务器",
                summary: endpointGroupCapacitySummary(section.endpoints, group: nil, store: store),
                actionTitle: "管理服务器组",
                action: manageGroups
            )
        case .flat:
            EmptyView()
        }
    }

    private func leases(for endpoint: EndpointRecord) -> [LeaseRecord] {
        let gpuIDs = Set(store.snapshot.gpus(for: endpoint).map(\.id))
        guard !gpuIDs.isEmpty else { return [] }
        return store.snapshot.leases.filter { lease in
            !gpuIDs.isDisjoint(with: lease.gpuIDs)
                && !["RELEASED", "EXPIRED", "CANCELLED"].contains(lease.state)
        }
    }

    private func endpointSort(_ lhs: EndpointRecord, _ rhs: EndpointRecord) -> Bool {
        let comparison: ComparisonResult
        switch sort {
        case .attention:
            comparison = compare(endpointAttentionRank(lhs), endpointAttentionRank(rhs))
        case .id:
            comparison = lhs.sshCommand.localizedStandardCompare(rhs.sshCommand)
        case .assignment:
            comparison = assignmentSortLabel(lhs).localizedStandardCompare(assignmentSortLabel(rhs))
        case .availableGPU:
            comparison = compare(availableGPUCount(lhs), availableGPUCount(rhs))
        case .gpuModel:
            let left = endpointGPUModelSortLabel(lhs)
            let right = endpointGPUModelSortLabel(rhs)
            comparison = left.localizedStandardCompare(right)
        case .gpuUtilization:
            let left = endpointOverviewGPUUtilizationFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointOverviewGPUUtilizationFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .gpuMemory:
            let left = endpointOverviewGPUMemoryFraction(endpoint: lhs, gpus: store.snapshot.gpus(for: lhs)) ?? -1
            let right = endpointOverviewGPUMemoryFraction(endpoint: rhs, gpus: store.snapshot.gpus(for: rhs)) ?? -1
            comparison = compare(left, right)
        case .cpuLoad:
            comparison = compare(endpointOverviewCPULoadFraction(endpoint: lhs) ?? -1, endpointOverviewCPULoadFraction(endpoint: rhs) ?? -1)
        case .memory:
            comparison = compare(endpointOverviewMemoryFraction(endpoint: lhs) ?? -1, endpointOverviewMemoryFraction(endpoint: rhs) ?? -1)
        }
        if comparison == .orderedSame {
            return lhs.id.localizedStandardCompare(rhs.id) == .orderedAscending
        }
        return sortDirection == .ascending ? comparison == .orderedAscending : comparison == .orderedDescending
    }

    private func compare<T: Comparable>(_ lhs: T, _ rhs: T) -> ComparisonResult {
        if lhs < rhs { return .orderedAscending }
        if lhs > rhs { return .orderedDescending }
        return .orderedSame
    }

    private func assignmentSortLabel(_ endpoint: EndpointRecord) -> String {
        let endpointLeases = leases(for: endpoint)
        guard let lease = endpointLeases.first(where: { $0.runtimeState == "RUNNING" }) ?? endpointLeases.first else {
            return ""
        }
        return "\(lease.projectID) \(lease.taskReference ?? lease.purpose ?? "")"
    }

    private func endpointAttentionRank(_ endpoint: EndpointRecord) -> Int {
        let endpointGPUs = store.snapshot.gpus(for: endpoint)
        let gpuRank = endpointGPUs.contains { gpuNeedsAttention($0) } ? 2 : 0
        let pressureRank = endpointHighPressure(endpoint: endpoint, gpus: endpointGPUs) ? 1 : 0
        return (endpointNeedsAttention(endpoint) ? 3 : 0) + gpuRank + pressureRank
    }

    /// Sort key only.  Must not feed “N 张空闲” copy — pool `free_gpu_count`
    /// is not one-apply capacity, and the page total uses `allocatableGPUCount`.
    private func availableGPUCount(_ endpoint: EndpointRecord) -> Int {
        guard store.freshness == .fresh, endpoint.monitorStatus == "ONLINE" else { return 0 }
        let local = store.snapshot.gpus(for: endpoint).filter(\.isPubliclyAvailable).count
        if local > 0 { return local }
        return endpoint.schedulerCapacity?.freeGPUCount ?? 0
    }

    private func endpointGPUModelSortLabel(_ endpoint: EndpointRecord) -> String {
        let gpus = store.snapshot.gpus(for: endpoint)
        if !gpus.isEmpty { return endpointGPUModelSummary(gpus) }
        return endpoint.schedulerCapacity?.gpuName ?? "无 GPU"
    }

    private var allocatableGPUSummary: String {
        guard store.freshness == .fresh else { return "未确认" }
        return "\(allocatableGPUCount)/\(store.snapshot.operationalGPUs.count)"
    }

    private var fleetGPUModelSummary: String {
        let groups = Dictionary(grouping: store.snapshot.operationalGPUs, by: \.name)
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


private struct ResourceInlineStat: View {
    let value: String
    let label: String
    let color: Color

    var body: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(value)
                .font(Typography.label)
                .foregroundStyle(DesignTokens.ink)
            Text(label)
                .font(.caption2.weight(.medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }
}

/// Column geometry for the server table, in three folding tiers.
///
/// The eight-column table needs about 1080 pt of content width and only about
/// 1010 pt exists at the 1280-wide acceptance viewport.  That is what sent this
/// page to a card grid — but a grid bought the width by giving up the one thing
/// the page is for, comparing four pressures down a column.  So the width is
/// solved here instead: the SSH lane keeps a hard floor at every viewport, and
/// the two columns whose facts are also in the row tooltip and the detail sheet
/// fold away beneath it.  No tier drops a pressure bar.

private enum EndpointTableLayout {
    /// 46 characters at 11 pt SF Mono, the longest SSH command a fixture
    /// carries.  Below this the command truncates, which is the failure the
    /// card grid was built to escape.
    static let sshLane: CGFloat = 304
    static let cardPadding: CGFloat = 16
    static let rowHeight: CGFloat = 44
    static let headerHeight: CGFloat = 34
    /// Percentage lane inside a pressure cell; the bar takes what is left.
    static let percentageWidth: CGFloat = 30

    enum Tier {
        /// Every column.
        case wide
        /// Without GPU 配置.
        case medium
        /// Without GPU 配置 and 项目 / 任务.
        case compact

        var showsGPUModel: Bool { self == .wide }
        var showsAssignment: Bool { self != .compact }

        var columnSpacing: CGFloat { self == .wide ? 12 : 10 }
        var assignmentWidth: CGFloat { self == .wide ? 160 : 140 }
        /// Wide enough for a full vendor-qualified name and its count —
        /// "NVIDIA A100-SXM4-80GB × 8" — because a model truncated to
        /// "NVIDIA A100-SXM4-80G…" cannot be told from its siblings.
        var gpuModelWidth: CGFloat { 180 }
        var availabilityWidth: CGFloat { self == .wide ? 60 : 56 }

        /// A percentage and its bar.  The bar never disappears: a number that
        /// cannot be compared down a column is the defect this table exists to
        /// fix, and four metrics drawn two ways read as two classes of fact.
        var pressureWidth: CGFloat {
            switch self {
            case .wide: 80
            case .medium: 76
            case .compact: 68
            }
        }

        /// Content width this tier needs before the SSH lane would be squeezed.
        var minimumWidth: CGFloat {
            var total = EndpointTableLayout.sshLane + availabilityWidth + pressureWidth * 4
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

    var body: some View {
        HStack(spacing: 8) {
            Text(title)
                .font(Typography.label)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
            Spacer(minLength: 8)
            Text(summary)
                .font(Typography.rowValue)
                .foregroundStyle(DesignTokens.mutedInk)
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
        .frame(height: 32)
        .background(DesignTokens.ink.opacity(DesignTokens.Alpha.hairline))
        .accessibilityAddTraits(.isHeader)
        .modifier(EndpointGroupSectionHeaderAccessibility(
            title: title,
            summary: summary,
            combined: action == nil
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
                .frame(minWidth: EndpointTableLayout.sshLane, maxWidth: .infinity, alignment: .leading)
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
            header(.gpuUtilization, trailing: true)
                .frame(width: tier.pressureWidth, alignment: .trailing)
            header(.gpuMemory, trailing: true)
                .frame(width: tier.pressureWidth, alignment: .trailing)
            header(.cpuLoad, trailing: true)
                .frame(width: tier.pressureWidth, alignment: .trailing)
            header(.memory, trailing: true)
                .frame(width: tier.pressureWidth, alignment: .trailing)
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

/// One pressure column: the percentage, then its bar, on one baseline.

private struct TablePressureCell: View {
    let label: String
    let fraction: Double?
    let width: CGFloat

    var body: some View {
        HStack(spacing: 6) {
            Text(percentageLabel(fraction))
                .font(Typography.rowValue)
                .foregroundStyle(fraction == nil ? DesignTokens.mutedInk : DesignTokens.ink)
                .lineLimit(1)
                .frame(width: EndpointTableLayout.percentageWidth, alignment: .trailing)
            PressureMeter(fraction: fraction, color: pressureColor(fraction), height: 4)
        }
        .frame(width: width, alignment: .trailing)
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
    let select: () -> Void

    var body: some View {
        Button(action: select) {
            HStack(spacing: tier.columnSpacing) {
                serverCell
                    .frame(minWidth: EndpointTableLayout.sshLane, maxWidth: .infinity, alignment: .leading)
                if tier.showsAssignment {
                    assignmentCell.frame(width: tier.assignmentWidth, alignment: .leading)
                }
                if tier.showsGPUModel {
                    gpuModelCell.frame(width: tier.gpuModelWidth, alignment: .leading)
                }
                availabilityCell.frame(width: tier.availabilityWidth, alignment: .trailing)
                TablePressureCell(label: "GPU 利用率", fraction: gpuPressure, width: tier.pressureWidth)
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
        // Everything a 44 pt row cannot print — the workspace path, per-card
        // VRAM, the full lease list — is one hover away, and all of it is in
        // the detail sheet a click away.
        .help(tooltip)
        .accessibilityElement(children: .ignore)
        // Without this the row reports as AXUnknown, so assistive technology
        // reads its label but cannot say it is actionable.
        .accessibilityAddTraits(.isButton)
        .accessibilityIdentifier("endpoint-row-\(endpoint.id)")
        .accessibilityLabel("服务器 \(endpoint.sshCommand)")
        .accessibilityValue(accessibilityValue)
    }

    /// The row's identity: a status pip, the SSH command in full, and the one
    /// word that answers "can I ask this machine for a GPU".
    private var serverCell: some View {
        HStack(spacing: 9) {
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            Text(endpoint.sshCommand)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
            Text(statusWord)
                .font(Typography.annotation)
                .foregroundStyle(statusColor)
                .lineLimit(1)
                .fixedSize()
            if endpoint.workspacePath == nil {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.warning)
                    .help("工作区未设置")
            }
            Spacer(minLength: 0)
        }
    }

    private var assignmentCell: some View {
        Text(assignmentLine)
            .font(Typography.identity)
            .foregroundStyle(assignmentIsUnassigned ? DesignTokens.mutedInk : DesignTokens.ink)
            .lineLimit(1)
            .truncationMode(.tail)
            .frame(maxWidth: .infinity, alignment: .leading)
            .help(assignmentHelp)
    }

    private var gpuModelCell: some View {
        Text(gpuModelLine)
            .font(Typography.identity)
            .foregroundStyle(gpus.isEmpty && endpoint.schedulerCapacity == nil ? DesignTokens.mutedInk : DesignTokens.ink)
            .lineLimit(1)
            .truncationMode(.tail)
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
                    .font(Typography.cardValue)
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

    /// A GPU host names its hardware.  A scheduler endpoint names the pool
    /// model, not a local inventory count.  A CPU node names how much machine
    /// it is, because "无 GPU" would only repeat the status word beside the command.
    private var gpuModelLine: String {
        if !gpus.isEmpty { return "\(gpuModelSummary) × \(gpus.count)" }
        if let name = endpoint.schedulerCapacity?.gpuName { return name }
        return hostScale
    }

    /// First match wins.  A free card outranks a busy sibling: the contract's
    /// fault-isolation rule says one conflicted GPU must never hide the rest.
    /// Scheduler-backed endpoints have no local GPU inventory; they are not
    /// CPU nodes.
    ///
    /// Unreachable is not one thing: a stale local snapshot is this machine's
    /// loopback hop, not the remote server, and monitorLabel already carries
    /// the distinct words for connection trouble versus a human-set
    /// disabled/draining state — collapsing either into "无响应" would relabel
    /// a deliberate pause as a failure.
    private var statusWord: String {
        if !isSnapshotFresh { return "本机未更新" }
        if endpoint.monitorStatus != "ONLINE" { return endpoint.monitorLabel }
        if isOnDemandEndpoint {
            if let limit = onDemandApplyLimit {
                return "按需申请 · 一次最多 \(limit) 卡"
            }
            return "按需申请"
        }
        if gpus.isEmpty { return "CPU 节点" }
        if availableGPUCount > 0 { return "空闲" }
        if gpus.contains(where: { $0.state == "BUSY_UNMANAGED" || $0.state == "ORPHANED_BUSY" }) { return "未归属占用" }
        if gpus.contains(where: { $0.state == "HELD" || $0.state == "LEASED_IDLE" || $0.state == "KEEPALIVE" }) { return "占卡" }
        return "任务占用"
    }

    private var statusColor: Color {
        if !isSnapshotFresh { return DesignTokens.danger }
        if endpoint.monitorStatus != "ONLINE" { return endpointMonitorStatusColor(endpoint.monitorStatus) }
        if isOnDemandEndpoint {
            return onDemandHasCapacity ? DesignTokens.success : DesignTokens.mutedInk
        }
        if gpus.isEmpty { return DesignTokens.mutedInk }
        if availableGPUCount > 0 { return DesignTokens.success }
        if gpus.contains(where: { $0.state == "HELD" || $0.state == "LEASED_IDLE" || $0.state == "KEEPALIVE" }) { return DesignTokens.hold }
        return DesignTokens.warning
    }

    private var isOnDemandEndpoint: Bool {
        endpoint.schedulerCapacity != nil || group?.allocation == .delegated
    }

    /// One-apply cap, never the pool's remaining total.
    private var onDemandApplyLimit: Int? {
        group?.largestAllocatableBlock
            ?? group?.limits?.maxGPUsPerLease
            ?? endpoint.schedulerCapacity?.maxGPUsPerLease
            ?? endpoint.schedulerCapacity?.largestFreeBlock
    }

    private var onDemandHasCapacity: Bool {
        if let free = endpoint.schedulerCapacity?.freeGPUCount { return free > 0 }
        if let block = group?.largestAllocatableBlock { return block > 0 }
        return false
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
        if endpoint.keepalive.isActive || endpoint.keepalive.isTransitioning { return "可用 · 空闲占卡" }
        if hasUnattributedWorkload { return "任务占用" }
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
            return "\(lease.projectID) · \(task) · \(lease.gpuIDs.count) GPU"
        }.joined(separator: "\n")
    }

    private var availableGPUCount: Int {
        guard isSnapshotFresh, endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
    }

    private var gpuPressure: Double? {
        endpointOverviewGPUUtilizationFraction(endpoint: endpoint, gpus: gpus)
    }

    private var gpuMemoryPressure: Double? {
        endpointOverviewGPUMemoryFraction(endpoint: endpoint, gpus: gpus)
    }

    private var cpuLoadPressure: Double? {
        endpointOverviewCPULoadFraction(endpoint: endpoint)
    }

    private var memoryPressure: Double? {
        endpointOverviewMemoryFraction(endpoint: endpoint)
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
        if gpus.contains(where: { ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains($0.state) }) { return "任务占用" }
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
        "\(assignmentHelp)，GPU 配置 \(gpuModelSummary)，\(gpuCaption)，资源指标为近 10 分钟均值：CPU 负载 \(percentageLabel(cpuLoadPressure))，内存占用率 \(percentageLabel(memoryPressure))，GPU 利用率 \(percentageLabel(gpuPressure))"
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

