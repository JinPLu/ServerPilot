import AppKit
import SwiftUI

/// The first thing a person sees answers one question: is there anything I
/// need to do? Everything here links to the page that can act on it.
struct OverviewDashboard: View {
    private static let shownAlerts = 5

    @ObservedObject var store: BrokerStore
    let openEndpoint: (EndpointRecord) -> Void
    let navigate: (DashboardSection) -> Void

    private var openAlerts: [AlertRecord] { store.alerts.filter(\.isOpen) }
    private var summary: ResourceSummary { store.snapshot.summary }

    private struct Fleet: Identifiable {
        let id: String
        let name: String
        let servers: [EndpointRecord]
    }

    /// Groups in the order the server page shows them. A server with neither
    /// cards nor a scheduler has nothing to say here.
    private var fleets: [Fleet] {
        let shown = store.snapshot.endpoints.filter {
            !store.snapshot.gpus(for: $0).isEmpty || $0.schedulerCapacity != nil || !$0.nodes.isEmpty
        }
        let grouped = Dictionary(grouping: shown) { store.snapshot.serverGroup(for: $0)?.id ?? "" }
        var answer = store.snapshot.serverGroups.compactMap { group in
            grouped[group.id].map { Fleet(id: group.id, name: group.displayName, servers: $0) }
        }
        if let loose = grouped[""] {
            answer.append(Fleet(id: "", name: "未分组", servers: loose))
        }
        return answer
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                metrics
                if !openAlerts.isEmpty { attention }
                LazyVGrid(
                    columns: [GridItem(.adaptive(minimum: 380), spacing: 16, alignment: .top)],
                    alignment: .leading,
                    spacing: 16
                ) {
                    ForEach(fleets) { fleet in
                        FleetCard(store: store, name: fleet.name, servers: fleet.servers, open: openEndpoint)
                    }
                }
                OverviewLegend()
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 16)
        }
    }

    private var metrics: some View {
        HStack(spacing: 16) {
            MetricTile(value: "\(summary.availableGPUs)", unit: "/ \(summary.totalGPUs)", label: "空闲 GPU") {
                navigate(.resources)
            }
            MetricTile(value: "\(store.snapshot.leases.count)", unit: nil, label: "进行中的任务") {
                navigate(.leases)
            }
            MetricTile(value: "\(summary.onlineServers)", unit: "/ \(summary.totalServers)", label: "在线服务器") {
                navigate(.resources)
            }
            MetricTile(value: "\(openAlerts.count)", unit: nil, label: "待处理提醒") {
                navigate(.alerts)
            }
        }
    }

    private var attention: some View {
        HomeCard {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("需要处理").font(Typography.label)
                    Spacer()
                    if openAlerts.count > Self.shownAlerts {
                        Button("全部 \(openAlerts.count) 条") { navigate(.alerts) }
                            .buttonStyle(.link)
                    }
                }
                ForEach(openAlerts.prefix(Self.shownAlerts)) { alert in
                    Button {
                        navigate(alert.category == .serverAbnormal ? .resources : .leases)
                    } label: {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(alert.category.title)
                                .font(Typography.annotation)
                                .foregroundStyle(DesignTokens.mutedInk)
                            Text(alert.message)
                                .font(Typography.identity)
                                .foregroundStyle(DesignTokens.ink)
                            Spacer()
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private struct MetricTile: View {
    let value: String
    let unit: String?
    let label: String
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HomeCard(padding: 16) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Text(value).font(Typography.hero).foregroundStyle(DesignTokens.ink)
                        if let unit {
                            Text(unit).font(Typography.cardValue).foregroundStyle(DesignTokens.mutedInk)
                        }
                    }
                    Text(label).font(Typography.identity).foregroundStyle(DesignTokens.mutedInk)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
    }
}

private struct FleetCard: View {
    @ObservedObject var store: BrokerStore
    let name: String
    let servers: [EndpointRecord]
    let open: (EndpointRecord) -> Void

    private var headline: String {
        let gpus = servers.flatMap { store.snapshot.gpus(for: $0) }
        let schedulerFree = servers.compactMap(\.schedulerCapacity).reduce(0) { $0 + $1.freeGPUCount }
        if gpus.isEmpty { return "\(schedulerFree) 张空闲" }
        return "\(gpus.filter(\.isPubliclyAvailable).count) / \(gpus.count) 空闲"
    }

    var body: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text(name).font(Typography.label).foregroundStyle(DesignTokens.ink)
                    Spacer()
                    Text(headline).font(Typography.identity).foregroundStyle(DesignTokens.mutedInk)
                }
                ForEach(servers) { endpoint in
                    Button { open(endpoint) } label: {
                        row(endpoint).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder private func row(_ endpoint: EndpointRecord) -> some View {
        if let capacity = endpoint.schedulerCapacity {
            let total = endpoint.nodes.reduce(0) { $0 + $1.totalGPUs }
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(endpoint.connectionAddress).font(Typography.command).foregroundStyle(DesignTokens.ink)
                    Spacer()
                    if let limit = capacity.maxGPUsPerLease {
                        Text("单次最多 \(limit) 张").font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
                    }
                }
                ProgressView(value: Double(capacity.freeGPUCount), total: Double(max(total, capacity.freeGPUCount, 1)))
                    .tint(DesignTokens.success)
                    .accessibilityLabel("\(capacity.freeGPUCount) 张空闲")
            }
        } else {
            let gpus = store.snapshot.gpus(for: endpoint).sorted { $0.index < $1.index }
            HStack(spacing: 12) {
                Text(endpoint.connectionAddress)
                    .font(Typography.command)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                Spacer(minLength: 8)
                CardSquares(gpus: gpus, size: 16)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(endpoint.connectionAddress)，\(gpus.filter(\.isPubliclyAvailable).count) 张空闲，共 \(gpus.count) 张")
        }
    }
}

/// Every card of a server as one coloured square, the same on every page.
struct CardSquares: View {
    let gpus: [GPURecord]
    var size: CGFloat

    var body: some View {
        HStack(spacing: 3) {
            ForEach(gpus) { gpu in
                RoundedRectangle(cornerRadius: DesignTokens.Radius.pip, style: .continuous)
                    .fill(cardStatusColor(gpu.state))
                    .frame(width: size, height: size)
                    .help("GPU \(gpu.index) · \(gpuPresentationLabel(gpu))")
            }
        }
    }
}

private struct OverviewLegend: View {
    private static let entries = ["AVAILABLE", "RUNNING_MANAGED", "HELD", "BUSY_UNMANAGED", "CONFLICT", "UNKNOWN_STALE"]

    var body: some View {
        HStack(spacing: 14) {
            ForEach(Self.entries, id: \.self) { state in
                HStack(spacing: 5) {
                    RoundedRectangle(cornerRadius: 2, style: .continuous)
                        .fill(cardStatusColor(state))
                        .frame(width: 10, height: 10)
                    Text(CardVocabulary.label(forState: state))
                }
            }
        }
        .font(Typography.annotation)
        .foregroundStyle(DesignTokens.mutedInk)
    }
}

func cardStatusColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE", "KEEPALIVE": DesignTokens.success
    case "RUNNING_MANAGED": DesignTokens.interaction
    case "HELD", "LEASED_IDLE": DesignTokens.hold
    case "BUSY_UNMANAGED": DesignTokens.warning
    case "CONFLICT", "ORPHANED_BUSY", "UNHEALTHY", "ERROR", "MAINTENANCE": DesignTokens.danger
    default: DesignTokens.mutedInk
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
