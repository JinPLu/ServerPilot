import AppKit
import SwiftUI
#if canImport(Charts)
import Charts
#endif

/// The first thing a person sees answers one question: is there anything I
/// need to do, and how much is free?  Every part links to the page that can act
/// on it.  Servers are tiles here because this page is for the whole fleet at a
/// glance; comparing machines is the server table's job.
struct OverviewDashboard: View {
    private static let shownAlerts = 3
    private static let tileMinimumWidth: CGFloat = 320

    @ObservedObject var store: BrokerStore
    let openEndpoint: (EndpointRecord) -> Void
    let navigate: (DashboardSection, EndpointFilter) -> Void

    private var openAlerts: [AlertRecord] { store.alerts.filter(\.isOpen) }
    private var summary: ResourceSummary { store.snapshot.summary }
    private var fleets: [ServerFleet] { serverFleets(in: store.snapshot) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                // Side by side at every width the window allows.  Measuring whether
                // they fit read a long alert's full length, however it truncates.
                HStack(alignment: .top, spacing: 12) {
                    capacity.frame(maxWidth: .infinity)
                    trend.frame(maxWidth: .infinity)
                    attention.frame(maxWidth: .infinity)
                }
                ForEach(fleets) { fleet in
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(fleet.name)
                                .font(Typography.sectionTitle)
                                .foregroundStyle(DesignTokens.ink)
                                .accessibilityAddTraits(.isHeader)
                            Text(fleetHeadline(fleet))
                                .font(Typography.annotation)
                                .monospacedDigit()
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        LazyVGrid(
                            columns: [GridItem(.adaptive(minimum: Self.tileMinimumWidth), spacing: 10, alignment: .top)],
                            alignment: .leading,
                            spacing: 10
                        ) {
                            ForEach(fleet.servers) { endpoint in
                                ServerTile(store: store, endpoint: endpoint) { openEndpoint(endpoint) }
                            }
                        }
                    }
                }
                if fleets.isEmpty {
                    HomeCard {
                        VStack(spacing: 8) {
                            Image(systemName: "server.rack")
                                .font(Typography.icon)
                                .foregroundStyle(DesignTokens.mutedInk)
                            Text("还没有服务器").font(Typography.label)
                            Text("添加服务器后，这里会显示每张卡的状态。")
                                .font(Typography.annotation)
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 28)
                    }
                } else {
                    HStack {
                        Spacer(minLength: 0)
                        CardLegend()
                            .font(Typography.annotation)
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 20)
            .padding(.bottom, 20)
        }
    }

    // ---- the summary a person reads first ----

    private var largestSingleApply: Int? {
        let direct = store.snapshot.operationalEndpoints.map { endpoint -> Int in
            guard endpoint.monitorStatus == "ONLINE" else { return 0 }
            return store.snapshot.gpus(for: endpoint).filter(\.isPubliclyAvailable).count
        }
        let delegated = store.snapshot.serverGroups.compactMap(\.largestAllocatableBlock)
        return (direct + delegated).max()
    }

    /// Whether the numbers on this page say anything about now.
    private var isCurrent: Bool { store.freshness == .fresh }

    /// The share of cards that are free, from the figures printed inside the ring.
    private var freeShare: Double {
        guard isCurrent, summary.totalGPUs > 0 else { return 0 }
        return Double(summary.availableGPUs) / Double(summary.totalGPUs)
    }

    /// The rest of the fleet in words, so nothing has to be read off a colour.
    private var capacityFacts: [(label: String, value: String)] {
        guard isCurrent else { return [] }
        let cards = store.snapshot.operationalGPUs
        let offline = cards.filter { $0.load == .stale }.count
        let faulted = cards.filter { $0.load == .fault }.count
        let inUse = max(0, summary.totalGPUs - summary.availableGPUs - offline - faulted)
        var facts = [(label: "在用", value: "\(inUse)"), (label: "离线", value: "\(offline)")]
        if faulted > 0 { facts.append((label: "异常", value: "\(faulted)")) }
        if let largest = largestSingleApply, largest > 0 {
            facts.append((label: "单次最多", value: "\(largest)"))
        }
        return facts
    }

    private var capacityDetail: String {
        isCurrent ? cardSquaresSummary(store.snapshot.operationalGPUs) : "暂时无法确认"
    }

    private var capacity: some View {
        Button { navigate(.resources, .available) } label: {
            HomeCard(padding: 16) {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 0) {
                        Text("空闲 GPU")
                            .font(Typography.label)
                            .foregroundStyle(DesignTokens.mutedInk)
                        Spacer(minLength: 6)
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(capacityFacts, id: \.label) { fact in
                                HStack(spacing: 0) {
                                    Text(fact.label)
                                        .font(Typography.body)
                                        .foregroundStyle(DesignTokens.mutedInk)
                                        .frame(width: 60, alignment: .leading)
                                    Text(fact.value)
                                        .font(Typography.rowValue)
                                        .foregroundStyle(DesignTokens.ink)
                                }
                                .lineLimit(1)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    CapacityRing(
                        freeShare: freeShare,
                        figure: isCurrent ? "\(summary.availableGPUs)" : "—",
                        total: summary.totalGPUs
                    )
                    .help(capacityDetail)
                    .accessibilityHidden(true)
                }
                .frame(height: 124)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("空闲 GPU")
        .accessibilityValue(isCurrent ? "\(summary.availableGPUs) 张，共 \(summary.totalGPUs) 张：\(capacityDetail)" : capacityDetail)
        .accessibilityAddTraits(.isButton)
    }

    private var trend: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("GPU 利用率")
                        .font(Typography.label)
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize()
                    Spacer(minLength: 8)
                    ViewThatFits(in: .horizontal) {
                        trendCaption("近 24 小时\(uncollectedCaption.map { " · \($0)" } ?? "")")
                        trendCaption("近 24 小时")
                    }
                }
                Text(percentageLabel(isCurrent ? store.utilizationTrends.fleet?.current : nil))
                    .font(Typography.display)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .padding(.top, 6)
                FleetTrendChart(series: store.utilizationTrends.fleet)
                    .frame(maxHeight: .infinity)
                    .padding(.top, 6)
            }
            .frame(height: 124)
        }
    }

    private func trendCaption(_ text: String) -> some View {
        Text(text)
            .font(Typography.annotation)
            .foregroundStyle(DesignTokens.mutedInk)
            .lineLimit(1)
            .fixedSize()
    }

    /// Words belong up here, not on the line: several gaps' labels collide over
    /// the plot and over the figure above it.
    private var uncollectedCaption: String? {
        let seconds = store.utilizationTrends.fleet?.gaps.map(\.seconds).reduce(0, +) ?? 0
        guard seconds > 0 else { return nil }
        if seconds < 3600 { return "未采集 \(seconds / 60) 分钟" }
        let hours = Double(seconds) / 3600
        return "未采集 \(hours.formatted(.number.precision(.fractionLength(0...1)))) 小时"
    }

    /// Only what a person has to act on.  A card coming free is good news: the
    /// notification tells it, and the alerts page keeps it.
    private var problems: [AlertRecord] {
        openAlerts.filter { $0.category != .capacityAvailable }
    }

    private var attention: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("需要处理")
                        .font(Typography.label)
                        .foregroundStyle(DesignTokens.mutedInk)
                    Spacer()
                    if problems.count > Self.shownAlerts {
                        Button("全部 \(problems.count)") { navigate(.alerts, .all) }
                            .buttonStyle(.link)
                            .font(Typography.annotation)
                    }
                }
                if problems.isEmpty {
                    Spacer(minLength: 0)
                    Label(
                        isCurrent ? "现在没有要处理的事" : "暂时无法确认",
                        systemImage: isCurrent ? "checkmark.circle" : "questionmark.circle"
                    )
                        .font(Typography.body)
                        .foregroundStyle(DesignTokens.mutedInk)
                    Spacer(minLength: 0)
                } else {
                    ForEach(problems.prefix(Self.shownAlerts)) { alert in
                        Button {
                            navigate(alert.category == .serverAbnormal ? .resources : .leases, .all)
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Circle()
                                    .fill(alert.category == .serverAbnormal ? DesignTokens.danger : DesignTokens.nearFullCard)
                                    .frame(width: 8, height: 8)
                                Text(alert.message)
                                    .font(Typography.body)
                                    .foregroundStyle(DesignTokens.ink)
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .help(alert.message)
                    }
                    Spacer(minLength: 0)
                }
            }
            .frame(height: 124)
        }
    }

    private func fleetHeadline(_ fleet: ServerFleet) -> String {
        let gpus = fleet.servers.flatMap { store.snapshot.gpus(for: $0) }
        let free = fleet.freeCardCount(in: store.snapshot)
        return gpus.isEmpty ? "\(free) 张空闲" : "\(free) / \(gpus.count) 空闲"
    }
}

/// The whole fleet over a day.  A gap in collection carries no reading: a faint
/// dotted line joins its two sides, unfilled, and the header says how long.
private struct FleetTrendChart: View {
    let series: UtilizationSeries?

    private struct Sample: Identifiable {
        let id: Int
        let value: Double
        let run: Int
    }

    /// Consecutive readings share a run, and each run draws its own line.
    private var samples: [Sample] {
        var run = 0
        var answer: [Sample] = []
        for (index, value) in (series?.points ?? []).enumerated() {
            guard let value else {
                run += 1
                continue
            }
            answer.append(Sample(id: index, value: value * 100, run: run))
        }
        return answer
    }

    private var gaps: [UtilizationSeries.Gap] { series?.gaps ?? [] }

    private static let gapLine = StrokeStyle(lineWidth: 1, lineCap: .round, dash: [1, 4])

    var body: some View {
        if series?.hasData == true {
#if canImport(Charts)
            Chart {
                ForEach(samples) { sample in
                    AreaMark(x: .value("时间", sample.id), y: .value("利用率", sample.value), series: .value("段", "\(sample.run)"))
                        .interpolationMethod(.monotone)
                        .foregroundStyle(DesignTokens.chartLine.opacity(DesignTokens.Alpha.fill))
                    LineMark(x: .value("时间", sample.id), y: .value("利用率", sample.value), series: .value("段", "\(sample.run)"))
                        .interpolationMethod(.monotone)
                        .foregroundStyle(DesignTokens.chartLine)
                        .lineStyle(StrokeStyle(lineWidth: 1.75, lineCap: .round, lineJoin: .round))
                }
                ForEach(gaps, id: \.beforeIndex) { gap in
                    LineMark(x: .value("时间", gap.beforeIndex), y: .value("利用率", gap.before * 100), series: .value("段", "gap-\(gap.beforeIndex)"))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineStyle(Self.gapLine)
                    LineMark(x: .value("时间", gap.afterIndex), y: .value("利用率", gap.after * 100), series: .value("段", "gap-\(gap.beforeIndex)"))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineStyle(Self.gapLine)
                }
            }
            .chartXAxis(.hidden)
            .chartXScale(domain: 0...max((series?.points.count ?? 1) - 1, 1))
            .chartYScale(domain: 0...100)
            .chartYAxis(.hidden)
            .accessibilityLabel("近 24 小时全部 GPU 的平均利用率")
#else
            Sparkline(points: series?.points ?? [], color: DesignTokens.chartLine, lineWidth: 2)
#endif
        } else {
            Text("暂无历史数据")
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// One server at a glance: who it is, what it is doing, every card, and whether
/// it has been busy lately.
private struct ServerTile: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    let open: () -> Void

    private var gpus: [GPURecord] {
        store.snapshot.gpus(for: endpoint).sorted { $0.index < $1.index }
    }

    private var status: ServerStatus {
        ServerStatus(
            endpoint: endpoint,
            gpus: gpus,
            group: store.snapshot.serverGroup(for: endpoint),
            isSnapshotFresh: store.freshness == .fresh
        )
    }

    private var leases: [LeaseRecord] {
        let ids = Set(gpus.map(\.id))
        return store.snapshot.leases.filter { !ids.isDisjoint(with: $0.gpuIDs) }
    }

    private var activity: ServerActivity {
        ServerActivity(
            endpoint: endpoint,
            gpus: gpus,
            leases: leases,
            status: status,
            isSnapshotFresh: store.freshness == .fresh
        )
    }

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 9) {
                    Circle()
                        .fill(endpointConnectionColor(endpoint, isSnapshotFresh: store.freshness == .fresh))
                        .frame(width: 8, height: 8)
                        .accessibilityHidden(true)
                    Text(endpoint.connectionAddress)
                        .font(Typography.rowPrimary)
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .layoutPriority(1)
                    Spacer(minLength: 8)
                    Text(activity.primary)
                        .font(Typography.annotation)
                        .foregroundStyle(activity.color)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                HStack(spacing: 10) {
                    if gpus.isEmpty, let capacity = endpoint.schedulerCapacity {
                        ProgressView(value: Double(capacity.freeGPUCount), total: Double(max(endpoint.nodes.reduce(0) { $0 + $1.totalGPUs }, capacity.freeGPUCount, 1)))
                            .tint(DesignTokens.quietBar)
                            .accessibilityHidden(true)
                    } else {
                        CardSquares(gpus: gpus, size: 13)
                            .fixedSize()
                    }
                    if let line = store.utilizationTrends.servers[endpoint.id], line.hasData {
                        Sparkline(points: line.points, color: DesignTokens.quietBar)
                            .frame(minWidth: 20, maxWidth: .infinity)
                            .frame(height: 16)
                    } else {
                        Spacer(minLength: 0)
                    }
                }
                .frame(height: 16)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                    .strokeBorder(hovering ? DesignTokens.tileStrokeStrong : DesignTokens.tileStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { open() }
        .accessibilityIdentifier("overview-server-\(endpoint.id)")
        .accessibilityLabel("服务器 \(endpoint.connectionAddress)")
        .accessibilityValue("\(status.word)，\(activity.primary)，\(gpus.filter(\.isPubliclyAvailable).count) 张空闲，共 \(gpus.count) 张")
    }
}

/// One GPU as one square, the same on every page.  A free card is an outline so
/// a fleet at rest stays quiet; a filled square means something is on the card.
struct CardPip: View {
    let load: CardLoad
    var size: CGFloat

    var body: some View {
        let shape = RoundedRectangle(cornerRadius: DesignTokens.Radius.pip, style: .continuous)
        Group {
            switch load {
            case .free:
                shape.strokeBorder(DesignTokens.freeCardStroke, lineWidth: 1.5)
            case .nearFull:
                // Yellow is too light to hold its own edge on white.
                shape.fill(pressureColor(.warning))
                    .overlay(shape.strokeBorder(DesignTokens.nearFullCardEdge, lineWidth: 1))
            case .low:
                shape.fill(pressureColor(.normal))
            case .full:
                shape.fill(pressureColor(.critical))
            case .unmeasured:
                shape.fill(DesignTokens.unmeasuredCard)
            case .fault:
                shape.fill(DesignTokens.danger)
            case .stale:
                shape.fill(DesignTokens.staleCard)
            }
        }
        .frame(width: size, height: size)
    }
}

/// How much of the fleet is free, read the way a phone shows its battery: the arc
/// is the free share, and the figure naming it sits inside.  Each card's own state
/// belongs to the tiles below.
struct CapacityRing: View {
    let freeShare: Double
    let figure: String
    let total: Int

    private static let diameter: CGFloat = 106
    private static let lineWidth: CGFloat = 17

    var body: some View {
        ZStack {
            Circle()
                .stroke(DesignTokens.track, lineWidth: Self.lineWidth)
            if freeShare > 0 {
                Circle()
                    .trim(from: 0, to: freeShare)
                    .stroke(DesignTokens.success, style: StrokeStyle(lineWidth: Self.lineWidth, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            VStack(spacing: -1) {
                Text(figure)
                    .font(Typography.display)
                    .foregroundStyle(DesignTokens.ink)
                Text("/ \(total) 张")
                    .font(Typography.annotation)
                    .monospacedDigit()
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .lineLimit(1)
        }
        .padding(Self.lineWidth / 2)
        .frame(width: Self.diameter, height: Self.diameter)
    }
}

/// Every card of a server, in index order.
struct CardSquares: View {
    let gpus: [GPURecord]
    var size: CGFloat

    var body: some View {
        HStack(spacing: 3) {
            ForEach(gpus) { gpu in
                CardPip(load: gpu.load, size: size)
                    .help("GPU \(gpu.index) · \(gpuPresentationLabel(gpu)) · \(cardLoadDetail(gpu))")
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(cardSquaresSummary(gpus))
    }
}

/// The readings behind a square's colour, where there are any.
private func cardLoadDetail(_ gpu: GPURecord) -> String {
    guard gpu.load.pressure != nil, let average = gpu.shortTelemetryAverage else { return gpu.load.label }
    let utilization = average.utilizationFraction.map(percentageLabel) ?? "—"
    let memory = average.memoryFraction.map(percentageLabel) ?? "—"
    return "\(gpu.load.label)（最近 \(average.windowSeconds) 秒平均：利用率 \(utilization)，显存 \(memory)）"
}

/// "6 张利用率低，2 张空闲" rather than one element per square.
func cardSquaresSummary(_ gpus: [GPURecord]) -> String {
    CardLoad.allCases.compactMap { load in
        let count = gpus.filter { $0.load == load }.count
        return count > 0 ? "\(count) 张\(load.label)" : nil
    }
    .joined(separator: "，")
}

/// What the squares mean, for a page without the overview's chips.
struct CardLegend: View {
    var body: some View {
        HStack(spacing: 10) {
            ForEach(CardLoad.allCases, id: \.self) { load in
                HStack(spacing: 4) {
                    CardPip(load: load, size: 9)
                    Text(load.label).lineLimit(1)
                }
                .fixedSize()
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// A command is only useful if it reaches a terminal intact.
struct CopyTextButton: View {
    let text: String

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } label: {
            Image(systemName: "doc.on.doc")
        }
        .buttonStyle(.borderless)
        .help("复制")
        .accessibilityLabel("复制")
    }
}
