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

    private var servers: [EndpointRecord] {
        store.snapshot.endpoints.filter { !store.snapshot.gpus(for: $0).isEmpty }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                attention
                fleet
            }
            .padding(24)
        }
    }

    @ViewBuilder private var attention: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("需要处理").font(.headline)
                Spacer()
                if openAlerts.count > Self.shownAlerts {
                    Button("全部 \(openAlerts.count) 条") { navigate(.alerts) }
                        .buttonStyle(.plain)
                        .foregroundStyle(DesignTokens.interaction)
                }
            }
            if openAlerts.isEmpty {
                Text("现在没有需要你处理的事。")
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            ForEach(openAlerts.prefix(Self.shownAlerts)) { alert in
                Button {
                    navigate(alert.category == .serverAbnormal ? .resources : .leases)
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(alert.category.title)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(DesignTokens.mutedInk)
                        Text(alert.message).foregroundStyle(DesignTokens.ink)
                        Spacer()
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var fleet: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text("全部 GPU").font(.headline)
                Text("\(store.snapshot.summary.availableGPUs) / \(store.snapshot.summary.totalGPUs) 空闲")
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            ForEach(servers) { endpoint in
                OverviewServerRow(
                    name: endpoint.id,
                    gpus: store.snapshot.gpus(for: endpoint),
                    open: { openEndpoint(endpoint) }
                )
            }
            OverviewLegend()
        }
    }
}

private struct OverviewServerRow: View {
    let name: String
    let gpus: [GPURecord]
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 12) {
                Text(name)
                    .frame(width: 200, alignment: .leading)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .foregroundStyle(DesignTokens.ink)
                HStack(spacing: 4) {
                    ForEach(gpus) { gpu in
                        RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .fill(cardStatusColor(gpu.state))
                            .frame(width: 16, height: 16)
                            .help("GPU \(gpu.index) · \(gpuPresentationLabel(gpu))")
                    }
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(name)，\(gpus.filter(\.isPubliclyAvailable).count) 张空闲，共 \(gpus.count) 张")
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
