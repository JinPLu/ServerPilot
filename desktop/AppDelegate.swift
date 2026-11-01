import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

@MainActor
final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let port = 8787
    private let brokerStore = BrokerStore()
    private var window: NSWindow?
    private var statusItem: StatusItemController?
    private var alertDelivery: AlertDelivery?
    private var isStarting = false

    private lazy var projectRoot: URL? = {
        if let configured = ProcessInfo.processInfo.environment["SERVERPILOT_ROOT"], !configured.isEmpty {
            return URL(fileURLWithPath: configured, isDirectory: true)
        }
        if let bundledRoot = Bundle.main.resourceURL,
           FileManager.default.fileExists(
               atPath: bundledRoot.appendingPathComponent("configs/inventory.yaml").path
           ) {
            return bundledRoot
        }
        let bundleParent = Bundle.main.bundleURL.deletingLastPathComponent()
        return findProjectRoot(startingAt: bundleParent)
    }()

    private var baseURL: URL {
        URL(string: "http://127.0.0.1:\(port)/")!
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.appearance = NSAppearance(named: .aqua)
        // Hairlines and secondary text read the Increase Contrast flag when
        // they resolve, so a change only needs to force a redraw.
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil,
            queue: .main
        ) { _ in
            for window in NSApp.windows {
                window.contentView?.needsDisplay = true
            }
        }
        configureMainMenu()

        let visibleSize = NSScreen.main?.visibleFrame.size ?? NSSize(width: 1440, height: 820)
        var initialSize = NSSize(
            width: max(1024, min(1440, visibleSize.width - 48)),
            height: max(640, min(820, visibleSize.height - 48))
        )
#if DEBUG || DESKTOP_FIXTURES
        if let fixtureViewport = fixtureViewportIfRequested() {
            initialSize = fixtureViewport
        }
#endif
        let contentRect = NSRect(origin: .zero, size: initialSize)
        let createdWindow = NSWindow(
            contentRect: contentRect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        createdWindow.title = "ServerPilot"
        createdWindow.titleVisibility = .hidden
        createdWindow.titlebarAppearsTransparent = true
        createdWindow.toolbarStyle = .unifiedCompact
        createdWindow.titlebarSeparatorStyle = .none
        createdWindow.backgroundColor = .windowBackgroundColor
        createdWindow.isOpaque = true
        createdWindow.minSize = NSSize(width: 900, height: 640)
        createdWindow.center()
        createdWindow.delegate = self
        // Closing leaves the App in the menu bar, and reopening shows this one.
        createdWindow.isReleasedWhenClosed = false

        let view = NSHostingView(rootView: NativeBrokerRoot(store: brokerStore))
        view.frame = contentRect
        view.autoresizingMask = [.width, .height]
        createdWindow.contentView = view
        window = createdWindow
        createdWindow.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
#if DEBUG || DESKTOP_FIXTURES
        if configureFixtureModeIfRequested() {
            captureFixtureScreenshotIfRequested(from: view)
            return
        }
#endif
        ensureDaemon()
        statusItem = StatusItemController(store: brokerStore) { [weak self] in self?.showMainWindow() }
        alertDelivery = AlertDelivery(store: brokerStore, notifier: SystemAlertNotifier())
        alertDelivery?.start()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func windowWillClose(_ notification: Notification) {
        brokerStore.windowVisible = false
        NSApp.setActivationPolicy(.accessory)
    }

    private func showMainWindow() {
        NSApp.setActivationPolicy(.regular)
        brokerStore.windowVisible = true
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func configureMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "退出 ServerPilot", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appMenuItem.submenu = appMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(withTitle: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "复制", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "全选", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editMenuItem.submenu = editMenu

        NSApp.mainMenu = mainMenu
    }

    private func findProjectRoot(startingAt url: URL) -> URL? {
        var candidate = url.standardizedFileURL
        let fileManager = FileManager.default
        while candidate.path != "/" {
            let projectFile = candidate.appendingPathComponent("pyproject.toml")
            let inventory = candidate.appendingPathComponent("configs/inventory.yaml")
            if fileManager.fileExists(atPath: projectFile.path) && fileManager.fileExists(atPath: inventory.path) {
                return candidate
            }
            candidate.deleteLastPathComponent()
        }
        return nil
    }

    private func brokerExecutable() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        let home = environment["HOME"] ?? NSHomeDirectory()
        var candidates: [String] = []
        if let configured = environment["SERVERPILOT_CLI"], !configured.isEmpty {
            candidates.append(configured)
        }
        candidates.append("\(home)/.local/share/uv/tools/serverpilot/bin/serverpilot")
        if let path = environment["PATH"] {
            for directory in path.split(separator: ":") {
                candidates.append("\(directory)/serverpilot")
            }
        }
        candidates.append(contentsOf: [
            "/opt/homebrew/bin/serverpilot",
            "/usr/local/bin/serverpilot",
        ])
        return candidates
            .map { URL(fileURLWithPath: $0) }
            .first(where: { FileManager.default.isExecutableFile(atPath: $0.path) })
    }

    private func processEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        if let root = projectRoot {
            environment["SERVERPILOT_PROJECT_ROOT"] = root.path
        }
        return environment
    }

    private func connectOrStartServer(attempt: Int = 0) {
        healthCheck { [weak self] result in
            DispatchQueue.main.async {
                guard let self else { return }
                switch result {
                case .compatible(let info):
                    self.brokerStore.connect(to: self.baseURL, serviceInfo: info)
                    return
                case .incompatible(let reason):
                    self.showFatalError(reason)
                    return
                case .unavailable:
                    break
                }
                if !self.isStarting {
                    self.ensureDaemon()
                    return
                }
                if attempt < 80 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
                        self.connectOrStartServer(attempt: attempt + 1)
                    }
                } else {
                    self.showFatalError("本机 ServerPilot 服务未能在规定时间内启动。请检查项目依赖和 state 目录。")
                }
            }
        }
    }

    private func healthCheck(completion: @escaping (ServiceProbeResult) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("health/live"))
        request.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: request) { [port] data, response, error in
            guard error == nil, let response = response as? HTTPURLResponse else {
                completion(.unavailable)
                return
            }
            guard response.statusCode == 200 else {
                completion(.unavailable)
                return
            }
            guard
                let data,
                let object = try? JSONSerialization.jsonObject(with: data),
                let payload = object as? [String: Any],
                let info = ServiceInfo(health: payload)
            else {
                completion(.incompatible("127.0.0.1:\(port) 上有服务响应，但它不是当前 ServerPilot 服务。桌面应用不会关闭或替换这个外部服务。"))
                return
            }
            // A healthy but stale daemon must go through ensureDaemon so the
            // owned LaunchAgent can be restarted onto this app's runtime.
            // Do not surface a false "incompatible service" dialog for a
            // ServerPilot process that simply predates the current runtime
            // capability floor.
            guard info.schemaVersion == "v1", info.capabilities.contains("instant_claims") else {
                completion(.incompatible("127.0.0.1:\(port) 上有服务响应，但它不是当前 ServerPilot 服务。桌面应用不会关闭或替换这个外部服务。"))
                return
            }
            guard info.capabilities.contains("endpoint_conflict_cleanup"),
                  info.capabilities.contains("endpoint_delete"),
                  info.capabilities.contains("operator_lease_release"),
                  info.capabilities.contains("telemetry_recent_averages") else {
                completion(.unavailable)
                return
            }
            completion(.compatible(info))
        }.resume()
    }

    private func ensureDaemon() {
        guard let root = projectRoot else {
            showFatalError(DesktopError.projectRootMissing.localizedDescription)
            return
        }
        guard let broker = brokerExecutable() else {
            showFatalError(DesktopError.brokerExecutableMissing.localizedDescription)
            return
        }
        isStarting = true
        let environment = processEnvironment()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                _ = try Self.runCommand(
                    executable: broker,
                    arguments: [
                        "daemon", "ensure", "--source-root", root.path
                    ],
                    root: root,
                    environment: environment
                )
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.connectOrStartServer()
                }
            } catch {
                DispatchQueue.main.async {
                    self.isStarting = false
                    self.showFatalError(error.localizedDescription)
                }
            }
        }
    }

#if DEBUG || DESKTOP_FIXTURES
    private func fixtureViewportIfRequested() -> NSSize? {
        guard let rawValue = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_VIEWPORT"] else {
            return nil
        }
        let components = rawValue.lowercased().split(separator: "x", maxSplits: 1)
        guard
            components.count == 2,
            let width = Double(components[0]),
            let height = Double(components[1]),
            width >= 900,
            height >= 640,
            width <= 1440,
            height <= 820
        else {
            return nil
        }
        return NSSize(width: width, height: height)
    }

    private func configureFixtureModeIfRequested() -> Bool {
        let environment = ProcessInfo.processInfo.environment
        guard let fixture = environment["SERVERPILOT_DESKTOP_FIXTURE"], !fixture.isEmpty else {
            return false
        }
        do {
            let fixturesRoot = desktopFixturesRoot()
            let fixtureURL = try FixtureSnapshots.resolve(
                fixture,
                fixturesRoot: fixturesRoot,
                projectRoot: projectRoot
            )
            let snapshot = try FixtureSnapshots.load(from: fixtureURL)
            if let historyFixture = environment["SERVERPILOT_DESKTOP_HISTORY_FIXTURE"], !historyFixture.isEmpty {
                let historyURL = try FixtureSnapshots.resolve(
                    historyFixture,
                    fixturesRoot: fixturesRoot,
                    projectRoot: projectRoot
                )
                let history = try FixtureSnapshots.loadEndpointTelemetryHistory(from: historyURL)
                guard snapshot.endpoints.contains(where: { $0.id == history.endpointID }) else {
                    throw FixtureSnapshotError.invalid(historyURL)
                }
                let serviceInfo = ServiceInfo(
                    schemaVersion: ServiceInfo.fixture.schemaVersion,
                    version: ServiceInfo.fixture.version,
                    capabilities: ServiceInfo.fixture.capabilities.union(["endpoint_telemetry_history"])
                )
                brokerStore.useFixture(
                    snapshot: snapshot,
                    serviceInfo: serviceInfo,
                    endpointTelemetryHistoryClient: FixtureEndpointTelemetryHistoryClient(history: history)
                )
            } else if let extrasFixture = environment["SERVERPILOT_DESKTOP_EXTRAS_FIXTURE"], !extrasFixture.isEmpty {
                let extras = try FixtureSnapshots.loadDashboardExtras(from: FixtureSnapshots.resolve(
                    extrasFixture,
                    fixturesRoot: fixturesRoot,
                    projectRoot: projectRoot
                ))
                brokerStore.useFixture(snapshot: snapshot, utilizationTrends: extras.trends, alerts: extras.alerts)
            } else {
                brokerStore.useFixture(snapshot: snapshot)
            }
            return true
        } catch {
            showFatalError(error.localizedDescription)
            return true
        }
    }

    private func desktopFixturesRoot() -> URL {
        if let resourceURL = Bundle.main.resourceURL?.appendingPathComponent("Fixtures", isDirectory: true),
           FileManager.default.fileExists(atPath: resourceURL.path) {
            return resourceURL
        }
        if let root = projectRoot {
            return root
                .appendingPathComponent("desktop", isDirectory: true)
                .appendingPathComponent("Fixtures", isDirectory: true)
        }
        return Bundle.main.bundleURL
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures", isDirectory: true)
    }

    private func captureFixtureScreenshotIfRequested(from view: NSView) {
        let environment = ProcessInfo.processInfo.environment
        guard let outputPath = environment["SERVERPILOT_DESKTOP_SCREENSHOT"], !outputPath.isEmpty else {
            return
        }
        let captureDelay = TimeInterval(environment["SERVERPILOT_DESKTOP_SCREENSHOT_DELAY"] ?? "") ?? 0.5
        DispatchQueue.main.asyncAfter(deadline: .now() + max(captureDelay, 0)) {
            // A sheet presents in its own attached window, so capturing the
            // main content view alone left the detail sheet — one of the
            // largest surfaces in the app — with no visual evidence at all.
            let view = view.window?.attachedSheet?.contentView ?? view
            let bounds = view.bounds
            guard
                let representation = NSBitmapImageRep(
                    bitmapDataPlanes: nil,
                    pixelsWide: Int(bounds.width),
                    pixelsHigh: Int(bounds.height),
                    bitsPerSample: 8,
                    samplesPerPixel: 4,
                    hasAlpha: true,
                    isPlanar: false,
                    colorSpaceName: .deviceRGB,
                    bytesPerRow: 0,
                    bitsPerPixel: 0
                )
            else {
                return
            }
            representation.size = bounds.size
            view.cacheDisplay(in: bounds, to: representation)
            guard let data = representation.representation(using: .png, properties: [:]) else {
                return
            }
            do {
                try data.write(to: URL(fileURLWithPath: outputPath), options: .atomic)
                if environment["SERVERPILOT_DESKTOP_EXIT_AFTER_SCREENSHOT"] == "1" {
                    NSApp.terminate(nil)
                }
            } catch {
                fputs("Unable to write fixture screenshot: \(error)\n", stderr)
            }
        }
    }
#endif

    nonisolated private static func runCommand(
        executable: URL,
        arguments: [String],
        root: URL,
        environment: [String: String]
    ) throws -> String {
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.currentDirectoryURL = root
        process.environment = environment
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        let details = String(data: data, encoding: .utf8) ?? ""
        guard process.terminationStatus == 0 else {
            throw DesktopError.commandFailed("启动本机后台服务失败：\(details)")
        }
        return details
    }

    private func showFatalError(_ message: String) {
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "无法启动 ServerPilot"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.runModal()
        NSApp.terminate(nil)
    }
}
