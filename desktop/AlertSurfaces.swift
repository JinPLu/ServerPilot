import AppKit
import SwiftUI

// ---- feedback ----

extension Feedback {
    var color: Color {
        switch kind {
        case .success: DesignTokens.success
        case .refused, .pending: DesignTokens.warning
        case .failure: DesignTokens.danger
        }
    }

    var icon: String {
        switch kind {
        case .success: "checkmark.circle.fill"
        case .refused: "hand.raised.fill"
        case .failure: "exclamationmark.triangle.fill"
        case .pending: "clock.fill"
        }
    }
}

/// The one place the outcome of an action appears, until it is dismissed.
struct FeedbackBanner: View {
    @ObservedObject var store: BrokerStore

    var body: some View {
        if let feedback = store.feedback {
            HStack(spacing: 8) {
                NoticeBanner(message: feedback.message, color: feedback.color, icon: feedback.icon)
                Button {
                    store.feedback = nil
                    store.notice = nil
                    store.errorMessage = nil
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("关闭这条提示")
                .accessibilityLabel("关闭这条提示")
            }
            .padding(.horizontal, 24)
            .padding(.top, 12)
        }
    }
}

// ---- menu bar ----

/// The overview at the size of a menu: the same words and the same squares, so
/// a glance here and a look at the window never read differently.
private struct MenuBarPopover: View {
    @ObservedObject var store: BrokerStore
    let openWindow: () -> Void
    let requestClaim: () -> Void

    private var summary: ResourceSummary { store.snapshot.summary }
    private var servers: [EndpointRecord] { serverFleets(in: store.snapshot).flatMap(\.servers) }

    private var freeFraction: Double {
        summary.totalGPUs > 0 ? Double(summary.availableGPUs) / Double(summary.totalGPUs) : 0
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if store.isConnected {
                capacity
                if !servers.isEmpty { fleet }
            } else {
                Label("本机服务未响应", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(DesignTokens.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
                    .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous))
            }
            HStack(spacing: 8) {
                Button("申请 GPU", action: requestClaim)
                    .buttonStyle(PrimaryActionButtonStyle())
                    .disabled(!store.allowsMutations || store.snapshot.operationalEndpoints.isEmpty)
                Button("打开 ServerPilot", action: openWindow)
                    .buttonStyle(SecondaryActionButtonStyle())
                Spacer(minLength: 0)
                Button {
                    NSApp.terminate(nil)
                } label: {
                    Image(systemName: "power")
                }
                .buttonStyle(.borderless)
                .help("退出 ServerPilot")
                .accessibilityLabel("退出 ServerPilot")
            }
        }
        .padding(12)
        .frame(width: 340)
        .background(DesignTokens.page)
    }

    private var capacity: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle().stroke(DesignTokens.track, lineWidth: 7)
                Circle()
                    .trim(from: 0, to: freeFraction)
                    .stroke(DesignTokens.success, style: StrokeStyle(lineWidth: 7, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            .frame(width: 46, height: 46)
            .padding(4)
            .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 5) {
                    Text("\(summary.availableGPUs)")
                        .font(Typography.display)
                        .foregroundStyle(DesignTokens.ink)
                    Text("/ \(summary.totalGPUs) 空闲")
                        .font(Typography.body)
                        .monospacedDigit()
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                Text("\(store.snapshot.leases.count) 个任务")
                    .font(Typography.annotation)
                    .monospacedDigit()
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("空闲 GPU")
        .accessibilityValue("\(summary.availableGPUs) 张，共 \(summary.totalGPUs) 张")
    }

    private var fleet: some View {
        VStack(spacing: 6) {
            ForEach(servers.prefix(10)) { endpoint in
                let gpus = store.snapshot.gpus(for: endpoint).sorted { $0.index < $1.index }
                HStack(spacing: 8) {
                    Text(endpoint.connectionAddress)
                        .font(Typography.identity.weight(.regular))
                        .monospacedDigit()
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    if gpus.isEmpty, let capacity = endpoint.schedulerCapacity {
                        Text("\(capacity.freeGPUCount) 张空闲")
                            .font(Typography.annotation)
                            .monospacedDigit()
                            .foregroundStyle(DesignTokens.mutedInk)
                    } else {
                        CardSquares(gpus: gpus, size: 10)
                            .fixedSize()
                    }
                }
            }
            if servers.count > 10 {
                Text("另有 \(servers.count - 10) 台")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(12)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous))
    }
}

@MainActor
final class StatusItemController: NSObject {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let store: BrokerStore

    init(store: BrokerStore, openWindow: @escaping () -> Void) {
        self.store = store
        super.init()
        item.button?.image = NSImage(systemSymbolName: "server.rack", accessibilityDescription: "ServerPilot")
        item.button?.target = self
        item.button?.action = #selector(toggle(_:))
        popover.behavior = .transient
        // A popover takes the appearance of the view it hangs from, and a
        // status item is the menu bar's: dark in Dark Mode.  The app draws
        // light only, so `labelColor` would turn white on these white cards.
        popover.appearance = NSAppearance(named: .aqua)
        popover.contentViewController = NSHostingController(rootView: MenuBarPopover(
            store: store,
            openWindow: { [weak self] in
                self?.popover.performClose(nil)
                openWindow()
            },
            requestClaim: { [weak self] in
                self?.popover.performClose(nil)
                openWindow()
                NotificationCenter.default.post(name: .serverPilotRequestClaim, object: nil)
            }
        ))
    }

    @objc private func toggle(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
        } else if let button = item.button {
            store.reload()
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
