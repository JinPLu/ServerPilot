import AppKit
import Combine
import SwiftUI
import UserNotifications

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
            .padding(.bottom, 10)
        }
    }
}

// ---- reminders page ----

struct AlertsDashboard: View {
    @ObservedObject var store: BrokerStore

    private var open: [AlertRecord] { store.alerts.filter(\.isOpen) }
    private var earlier: [AlertRecord] { store.alerts.filter { !$0.isOpen } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if store.alerts.isEmpty {
                    HomeCard {
                        VStack(spacing: 10) {
                            Image(systemName: "bell.badge")
                                .font(.largeTitle)
                                .foregroundStyle(DesignTokens.mutedInk)
                            Text("没有提醒").font(Typography.label)
                            Text("任务受影响、服务器异常、资源闲置或有卡可用时，会出现在这里。")
                                .font(Typography.identity)
                                .foregroundStyle(DesignTokens.mutedInk)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 40)
                    }
                }
                if !open.isEmpty {
                    HomeCard {
                        VStack(alignment: .leading, spacing: 12) {
                            HStack {
                                Text("\(open.count) 条待处理").font(Typography.label)
                                Spacer()
                                Button("全部确认") {
                                    let ids = open.map(\.id)
                                    Task { await store.markAlerts(ids, acked: true) }
                                }
                            }
                            ForEach(open) { alert in
                                Divider().opacity(DesignTokens.Alpha.muted)
                                AlertRow(alert: alert) {
                                    Task { await store.markAlerts([alert.id], acked: true) }
                                }
                            }
                        }
                    }
                }
                if !earlier.isEmpty {
                    HomeCard {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("更早").font(Typography.label).foregroundStyle(DesignTokens.mutedInk)
                            ForEach(earlier) { alert in
                                Divider().opacity(DesignTokens.Alpha.muted)
                                AlertRow(alert: alert, acknowledge: nil)
                            }
                        }
                    }
                }
            }
            .padding(.horizontal, 28)
            .padding(.vertical, 16)
        }
    }
}

private struct AlertRow: View {
    let alert: AlertRecord
    let acknowledge: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(alert.category.title)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                Text(alert.message)
                    .foregroundStyle(DesignTokens.ink)
                    .textSelection(.enabled)
            }
            Spacer()
            if let acknowledge {
                Button("确认", action: acknowledge)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// ---- notifications ----

@MainActor
protocol AlertNotifier {
    func deliver(title: String, body: String) async
}

/// The banner is a courtesy: the menu bar badge and the reminders page work
/// without it, so a refused authorization only costs the banner.
@MainActor
final class SystemAlertNotifier: AlertNotifier {
    private var authorized: Bool?

    func deliver(title: String, body: String) async {
        let center = UNUserNotificationCenter.current()
        if authorized == nil {
            authorized = (try? await center.requestAuthorization(options: [.alert, .sound])) ?? false
        }
        guard authorized == true else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        try? await center.add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }
}

@MainActor
final class AlertDelivery {
    private static let intervalSeconds: UInt64 = 15
    /// Past this, a burst after the App was closed reads as one summary.
    private static let individualLimit = 3

    private let store: BrokerStore
    private let notifier: AlertNotifier
    private var task: Task<Void, Never>?

    init(store: BrokerStore, notifier: AlertNotifier) {
        self.store = store
        self.notifier = notifier
    }

    func start() {
        task?.cancel()
        task = Task { [weak self] in
            while !Task.isCancelled {
                await self?.deliverPending()
                try? await Task.sleep(nanoseconds: Self.intervalSeconds * 1_000_000_000)
            }
        }
    }

    private func deliverPending() async {
        let pending = await store.pendingNotifications()
        guard !pending.isEmpty else { return }
        if pending.count > Self.individualLimit {
            await notifier.deliver(title: "ServerPilot", body: "有 \(pending.count) 条新提醒")
        } else {
            for alert in pending {
                await notifier.deliver(title: alert.category.title, body: alert.message)
            }
        }
        await store.markAlerts(pending.flatMap(\.alertIDs), notified: true)
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
    private var openAlerts: [AlertRecord] { store.alerts.filter(\.isOpen) }
    private var servers: [EndpointRecord] { serverFleets(in: store.snapshot).flatMap(\.servers) }

    private var freeFraction: Double {
        summary.totalGPUs > 0 ? Double(summary.availableGPUs) / Double(summary.totalGPUs) : 0
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if store.isConnected {
                capacity
                if !openAlerts.isEmpty { attention }
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
        .background(DesignTokens.ambientSmoke)
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
                        .font(Typography.hero)
                        .foregroundStyle(DesignTokens.ink)
                    Text("/ \(summary.totalGPUs) 空闲")
                        .font(.body)
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

    private var attention: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("需要处理 · \(openAlerts.count)")
                .font(Typography.annotation.weight(.semibold))
                .foregroundStyle(DesignTokens.warning)
            ForEach(openAlerts.prefix(3)) { alert in
                Text(alert.message)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(alert.message)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel + 4, style: .continuous))
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
    private var subscription: AnyCancellable?

    init(store: BrokerStore, openWindow: @escaping () -> Void) {
        self.store = store
        super.init()
        item.button?.image = NSImage(systemSymbolName: "server.rack", accessibilityDescription: "ServerPilot")
        item.button?.target = self
        item.button?.action = #selector(toggle(_:))
        popover.behavior = .transient
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
        subscription = store.$openAlertCount.sink { [weak self] count in
            self?.item.button?.title = count > 0 ? " \(count)" : ""
        }
    }

    @objc private func toggle(_ sender: Any?) {
        if popover.isShown {
            popover.performClose(sender)
        } else if let button = item.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
