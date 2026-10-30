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
            .padding(.top, 12)
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
                HStack {
                    Text(open.isEmpty ? "没有待处理的提醒" : "\(open.count) 条待处理")
                        .font(.headline)
                    Spacer()
                    if !open.isEmpty {
                        Button("全部确认") {
                            let ids = open.map(\.id)
                            Task { await store.markAlerts(ids, acked: true) }
                        }
                    }
                }
                ForEach(open) { alert in
                    AlertRow(alert: alert) {
                        Task { await store.markAlerts([alert.id], acked: true) }
                    }
                }
                if !earlier.isEmpty {
                    Text("更早").font(.subheadline.weight(.semibold)).foregroundStyle(DesignTokens.mutedInk)
                    ForEach(earlier) { alert in
                        AlertRow(alert: alert, acknowledge: nil)
                    }
                }
            }
            .padding(24)
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

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let store: BrokerStore
    private let openWindow: () -> Void
    private var subscription: AnyCancellable?

    init(store: BrokerStore, openWindow: @escaping () -> Void) {
        self.store = store
        self.openWindow = openWindow
        super.init()
        item.button?.image = NSImage(systemSymbolName: "server.rack", accessibilityDescription: "ServerPilot")
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        subscription = store.$openAlertCount.sink { [weak self] count in
            self?.item.button?.title = count > 0 ? " \(count)" : ""
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let summary = store.snapshot.summary
        menu.addItem(disabled: store.isConnected
            ? "\(summary.availableGPUs) / \(summary.totalGPUs) 张 GPU 空闲"
            : "本机服务未响应")
        let open = store.alerts.filter(\.isOpen)
        if !open.isEmpty {
            menu.addItem(.separator())
            for alert in open.prefix(5) {
                menu.addItem(disabled: alert.message)
            }
        }
        menu.addItem(.separator())
        let opener = NSMenuItem(title: "打开 ServerPilot", action: #selector(open(_:)), keyEquivalent: "")
        opener.target = self
        menu.addItem(opener)
        menu.addItem(withTitle: "退出 ServerPilot", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    }

    @objc private func open(_ sender: Any?) {
        openWindow()
    }
}

private extension NSMenu {
    func addItem(disabled title: String) {
        let row = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        row.isEnabled = false
        addItem(row)
    }
}
