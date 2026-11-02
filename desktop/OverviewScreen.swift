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
    private static let tileMinimumWidth: CGFloat = 228

    @ObservedObject var store: BrokerStore
    let openEndpoint: (EndpointRecord) -> Void
    let navigate: (DashboardSection, EndpointFilter) -> Void

    private var openAlerts: [AlertRecord] { store.alerts.filter(\.isOpen) }
    private var summary: ResourceSummary { store.snapshot.summary }
    private var fleets: [ServerFleet] { serverFleets(in: store.snapshot) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                statusChips
                ViewThatFits(in: .horizontal) {
                    HStack(alignment: .top, spacing: 14) {
                        capacity.frame(width: 300)
                        trend.frame(minWidth: 280)
                        attention.frame(width: 320)
                    }
                    VStack(spacing: 14) {
                        HStack(alignment: .top, spacing: 14) {
                            capacity.frame(width: 300)
                            attention
                        }
                        trend
                    }
                }
                ForEach(fleets) { fleet in
                    VStack(alignment: .leading, spacing: 10) {
                        Rectangle()
                            .fill(DesignTokens.surfaceStroke)
                            .frame(height: 1)
                            .padding(.bottom, 4)
                            .accessibilityHidden(true)
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(fleet.name)
                                .font(.title3.weight(.semibold))
                                .foregroundStyle(DesignTokens.ink)
                                .accessibilityAddTraits(.isHeader)
                            Text(fleetHeadline(fleet))
                                .font(Typography.identity)
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
                                .font(.title2)
                                .foregroundStyle(DesignTokens.mutedInk)
                            Text("还没有服务器").font(Typography.label)
                            Text("添加服务器后，这里会显示每张卡的状态。")
                                .font(Typography.identity)
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 28)
                    }
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 6)
            .padding(.bottom, 20)
        }
    }

    // ---- the summary a person reads first ----

    private struct StatusChip: Identifiable {
        let load: CardLoad
        let count: Int
        var id: String { load.rawValue }

        var filter: EndpointFilter {
            switch load {
            case .free: .available
            case .low, .nearFull, .unmeasured: .taskOccupied
            case .fault: .all
            case .stale: .connectionFailed
            }
        }
    }

    /// One chip per thing a square can say, so the row doubles as the squares' legend.
    private var chips: [StatusChip] {
        let gpus = store.snapshot.operationalGPUs
        return CardLoad.allCases
            .map { load in StatusChip(load: load, count: gpus.filter { $0.load == load }.count) }
            .filter { $0.count > 0 }
    }

    private var statusChips: some View {
        HStack(spacing: 8) {
            ForEach(chips) { chip in
                Button { navigate(.resources, chip.filter) } label: {
                    HStack(spacing: 7) {
                        CardPip(load: chip.load, size: 10)
                        Text(chip.load.label).foregroundStyle(DesignTokens.ink)
                        Text("\(chip.count)")
                            .fontWeight(.semibold)
                            .monospacedDigit()
                            .foregroundStyle(DesignTokens.ink)
                    }
                    .font(.body)
                    .padding(.horizontal, 12)
                    .frame(height: 30)
                    .background(DesignTokens.surface, in: Capsule())
                    .overlay(Capsule().strokeBorder(DesignTokens.tileStroke, lineWidth: 1))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .help("在服务器页查看")
                .accessibilityLabel("\(chip.load.label) \(chip.count) 张，在服务器页查看")
            }
        }
    }

    private var largestSingleApply: Int? {
        let direct = store.snapshot.operationalEndpoints.map { endpoint -> Int in
            guard endpoint.monitorStatus == "ONLINE" else { return 0 }
            return store.snapshot.gpus(for: endpoint).filter(\.isPubliclyAvailable).count
        }
        let delegated = store.snapshot.serverGroups.compactMap(\.largestAllocatableBlock)
        return (direct + delegated).max()
    }

    private var freeFraction: Double {
        summary.totalGPUs > 0 ? Double(summary.availableGPUs) / Double(summary.totalGPUs) : 0
    }

    private var capacity: some View {
        Button { navigate(.resources, .available) } label: {
            HomeCard(padding: 16) {
                HStack(spacing: 14) {
                    ZStack {
                        Circle().stroke(DesignTokens.track, lineWidth: 12)
                        Circle()
                            .trim(from: 0, to: freeFraction)
                            .stroke(DesignTokens.success, style: StrokeStyle(lineWidth: 12, lineCap: .round))
                            .rotationEffect(.degrees(-90))
                    }
                    .frame(width: 92, height: 92)
                    .padding(6)
                    .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("空闲 GPU")
                            .font(Typography.identity.weight(.semibold))
                            .foregroundStyle(DesignTokens.success)
                        Text("\(summary.availableGPUs)")
                            .font(Typography.display)
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(1)
                        Text(capacityCaption)
                            .font(Typography.annotation)
                            .foregroundStyle(DesignTokens.mutedInk)
                            .lineLimit(2)
                    }
                    Spacer(minLength: 0)
                }
                .frame(height: 124)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("空闲 GPU")
        .accessibilityValue("\(summary.availableGPUs) 张，共 \(summary.totalGPUs) 张")
        .accessibilityAddTraits(.isButton)
    }

    private var capacityCaption: String {
        var parts = ["共 \(summary.totalGPUs) 张"]
        if let largest = largestSingleApply, largest > 0 { parts.append("一次最多 \(largest) 张") }
        return parts.joined(separator: " · ")
    }

    private var trend: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("GPU 利用率")
                        .font(Typography.identity.weight(.semibold))
                        .foregroundStyle(DesignTokens.interaction)
                    Text(percentageLabel(store.utilizationTrends.fleet?.latest))
                        .font(Typography.pageTitle)
                        .monospacedDigit()
                        .foregroundStyle(DesignTokens.ink)
                    Spacer(minLength: 8)
                    ViewThatFits(in: .horizontal) {
                        trendCaption("近 24 小时\(uncollectedCaption.map { " · \($0)" } ?? "")")
                        trendCaption("近 24 小时")
                    }
                }
                FleetTrendChart(series: store.utilizationTrends.fleet)
                    .frame(maxHeight: .infinity)
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

    private var attention: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(openAlerts.isEmpty ? "需要处理" : "需要处理 · \(openAlerts.count)")
                        .font(Typography.identity.weight(.semibold))
                        .foregroundStyle(openAlerts.isEmpty ? DesignTokens.mutedInk : DesignTokens.warning)
                    Spacer()
                    if openAlerts.count > Self.shownAlerts {
                        Button("全部") { navigate(.alerts, .all) }
                            .buttonStyle(.link)
                            .font(Typography.annotation)
                    }
                }
                if openAlerts.isEmpty {
                    Spacer(minLength: 0)
                    Label("现在没有要处理的事", systemImage: "checkmark.circle")
                        .font(.body)
                        .foregroundStyle(DesignTokens.mutedInk)
                    Spacer(minLength: 0)
                } else {
                    ForEach(openAlerts.prefix(Self.shownAlerts)) { alert in
                        Button {
                            navigate(alert.category == .serverAbnormal ? .resources : .leases, .all)
                        } label: {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Circle()
                                    .fill(alert.category == .serverAbnormal ? DesignTokens.danger : DesignTokens.warning)
                                    .frame(width: 8, height: 8)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(alert.category.title)
                                        .font(Typography.annotation)
                                        .foregroundStyle(DesignTokens.mutedInk)
                                    Text(alert.message)
                                        .font(Typography.identity)
                                        .foregroundStyle(DesignTokens.ink)
                                        .lineLimit(1)
                                        .truncationMode(.tail)
                                }
                                Spacer(minLength: 0)
                            }
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .help(alert.message)
                    }
                }
                Spacer(minLength: 0)
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
                        .foregroundStyle(
                            LinearGradient(
                                colors: [DesignTokens.interaction.opacity(0.18), DesignTokens.interaction.opacity(0.02)],
                                startPoint: .top,
                                endPoint: .bottom
                            )
                        )
                    LineMark(x: .value("时间", sample.id), y: .value("利用率", sample.value), series: .value("段", "\(sample.run)"))
                        .interpolationMethod(.monotone)
                        .foregroundStyle(DesignTokens.interaction)
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
            .chartYAxis {
                AxisMarks(position: .trailing, values: [0, 50, 100]) { value in
                    AxisGridLine().foregroundStyle(DesignTokens.surfaceStroke)
                    AxisValueLabel {
                        if let percent = value.as(Int.self) {
                            Text("\(percent)%")
                                .font(Typography.annotation)
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                    }
                }
            }
            .accessibilityLabel("近 24 小时全部 GPU 的平均利用率")
#else
            Sparkline(points: series?.points ?? [], color: DesignTokens.interaction, lineWidth: 2)
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

    /// What the machine is doing, in the words of whoever is doing it.
    private var note: String {
        if let lease = leases.first(where: { $0.runtimeState == "RUNNING" }) ?? leases.first {
            let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
            return "\(task) · \(lease.holderLabel)"
        }
        if let capacity = endpoint.schedulerCapacity { return "\(capacity.freeGPUCount) 张空闲" }
        if store.freshness != .fresh || endpoint.monitorStatus != "ONLINE" {
            return endpoint.monitorStatus == "ONLINE" ? "显示上次数据" : endpointFooterMessage(endpoint)
        }
        if gpus.contains(where: { $0.state == "BUSY_UNMANAGED" }) { return "不归 ServerPilot 管理的进程" }
        return endpointGPUModelSummary(gpus)
    }

    private var background: Color {
        switch status.tone {
        case .fault: DesignTokens.dangerWash
        case .attention: DesignTokens.warningWash
        default: DesignTokens.surface
        }
    }

    var body: some View {
        Button(action: open) {
            VStack(alignment: .leading, spacing: 3) {
                Text(endpoint.connectionAddress)
                    .font(Typography.rowPrimary.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text(status.word)
                    .font(Typography.annotation)
                    .foregroundStyle(status.color)
                    .lineLimit(1)
                Text(note)
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 0)
                HStack(spacing: 10) {
                    if gpus.isEmpty, let capacity = endpoint.schedulerCapacity {
                        ProgressView(value: Double(capacity.freeGPUCount), total: Double(max(endpoint.nodes.reduce(0) { $0 + $1.totalGPUs }, capacity.freeGPUCount, 1)))
                            .tint(DesignTokens.success)
                            .accessibilityHidden(true)
                    } else {
                        CardSquares(gpus: gpus, size: 13)
                            .fixedSize()
                    }
                    if let line = store.utilizationTrends.servers[endpoint.id], line.hasData {
                        Sparkline(points: line.points, color: status.tone == .working ? DesignTokens.interaction : DesignTokens.quietBar)
                            .frame(minWidth: 20, maxWidth: .infinity)
                            .frame(height: 20)
                    } else {
                        Spacer(minLength: 0)
                    }
                }
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 11)
            .frame(height: 104)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous)
                    .strokeBorder(hovering ? DesignTokens.surfaceStroke : DesignTokens.tileStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
        .accessibilityElement(children: .ignore)
        .accessibilityAddTraits(.isButton)
        .accessibilityAction { open() }
        .accessibilityIdentifier("overview-server-\(endpoint.id)")
        .accessibilityLabel("服务器 \(endpoint.connectionAddress)")
        .accessibilityValue("\(status.word)，\(note)，\(gpus.filter(\.isPubliclyAvailable).count) 张空闲，共 \(gpus.count) 张")
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
                shape.fill(DesignTokens.nearFullCard)
                    .overlay(shape.strokeBorder(DesignTokens.nearFullCardEdge, lineWidth: 1))
            case .low:
                shape.fill(DesignTokens.interaction)
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
    guard gpu.load == .low || gpu.load == .nearFull else { return gpu.load.label }
    let utilization = gpu.utilization.map { percentageLabel(Double($0) / 100) } ?? "—"
    let memory = gpu.memoryUsedMiB == nil ? "—" : percentageLabel(gpu.memoryFraction)
    return "\(gpu.load.label)（利用率 \(utilization)，显存 \(memory)）"
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
