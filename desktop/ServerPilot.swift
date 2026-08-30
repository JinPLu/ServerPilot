import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

private enum DesktopError: LocalizedError {
    case projectRootMissing
    case brokerExecutableMissing
    case commandFailed(String)

    var errorDescription: String? {
        switch self {
        case .projectRootMissing:
            return "应用内置运行资源不完整。请重新安装 ServerPilot.app，或为开发构建设置 SERVERPILOT_ROOT。"
        case .brokerExecutableMissing:
            return "未找到已安装的 ServerPilot CLI。请在仓库根目录执行 `uv tool install --force .` 安装或升级命令行工具后重新打开应用。开发或测试可设置 SERVERPILOT_CLI 指向可执行文件。"
        case .commandFailed(let details):
            return details
        }
    }
}

// MARK: - Native desktop shell

@MainActor
final class DesktopAppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let port = 8787
    private let brokerStore = BrokerStore()
    private var window: NSWindow?
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
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
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

// MARK: - ServerPilot API model

private enum ServiceProbeResult {
    case compatible(ServiceInfo)
    case incompatible(String)
    case unavailable
}

@discardableResult
func confirmLeaseRelease(_ lease: LeaseRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放 \(lease.gpuIDs.count) 张 GPU？"
    alert.informativeText = "请先确认任务已结束。释放不会停止任务。"
    alert.addButton(withTitle: "释放")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmKeepaliveEnd() -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "结束占卡？"
    alert.informativeText = "将让出这台服务器上正在空闲占卡的 GPU；正在运行的任务不会被停止。"
    alert.addButton(withTitle: "结束占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

/// Describe when this lease last ran something, for the person about to clear it.
///
/// "No process right now" is what the release already proves, and it is not the
/// same as "this lease is finished": a job between two batches looks exactly
/// like one that ended. Clearing the first wedges its cards, because the work
/// comes back to a card that no longer belongs to anyone. Deliberately no
/// "recent = dangerous" threshold — that would rebuild a release standard in
/// the client, and this path exists to rescue exactly the leases no standard
/// can judge. The elapsed time is the fact; the judgement stays with the person.
private func processObservationSummary(_ isoTimestamp: String?) -> String {
    guard let isoTimestamp, let observedAt = endpointTelemetryHistoryDate(isoTimestamp) else {
        return "自这笔租约生效以来，ServerPilot 从未在这些 GPU 上观测到计算进程。"
    }
    let elapsed = Date().timeIntervalSince(observedAt)
    guard elapsed >= 0 else {
        return "最近一次在这些 GPU 上观测到计算进程：\(historyDateTime(observedAt))。"
    }
    return "最近一次在这些 GPU 上观测到计算进程：\(historyElapsedDescription(elapsed))前。"
}

@discardableResult
private func confirmEmptyLeaseCleanup(_ lease: LeaseRecord, conflict: Bool) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = conflict ? "确认任务已结束后清理记录？" : "释放这笔空闲占用？"
    alert.informativeText = """
    ServerPilot 会先重新采集这台服务器；只有确认这笔租约覆盖的 GPU 都没有运行中的进程时才会释放。此操作不会停止远端任务。

    \(processObservationSummary(lease.lastProcessObservedAt))
    """
    alert.addButton(withTitle: conflict ? "确认任务已结束后清理" : "释放空闲占用")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmEmptyKeepaliveCleanup(gpuCount: Int) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "释放遗留占卡？"
    alert.informativeText = "ServerPilot 会先重新采集这台服务器；只有确认 \(gpuCount) 张占卡 GPU 都没有运行中的进程时才会释放。"
    alert.addButton(withTitle: "释放遗留占卡")
    alert.addButton(withTitle: "取消")
    return alert.runModal() == .alertFirstButtonReturn
}

@discardableResult
private func confirmEndpointDelete(_ endpoint: EndpointRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "从 ServerPilot 移除这台服务器？"
    alert.informativeText = """
    \(endpoint.sshCommand)

    这会停止对本机的监控与协调，并移除本机关联记录，不会停止远端进程。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "从 ServerPilot 移除")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}

private func confirmServerGroupDelete(_ group: ServerGroupRecord) -> Bool {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "删除服务器组「\(group.displayName)」？"
    alert.informativeText = """
    只会删除分组，不会停止远端进程，也不会移除组内服务器。组内仍有服务器时删除会失败。
    """
    alert.addButton(withTitle: "取消")
    alert.addButton(withTitle: "删除服务器组")
    alert.buttons.last?.hasDestructiveAction = true
    return alert.runModal() == .alertSecondButtonReturn
}

// MARK: - Apple Home inspired native interface

private struct StableRecordSelection: Identifiable, Equatable {
    let id: String
}

private struct NativeBrokerRoot: View {
    @ObservedObject var store: BrokerStore
    @ObservedObject private var contrast = ContrastState.shared
    @State private var showAddServer = false
    @State private var showClaim = false
    @State private var showGroupManagement = false
    @State private var claimInitialEndpointID = ""
    @State private var selectedGPUID: String?
    @State private var selectedEndpointDetailID: String?
    @State private var editingEndpointID: String?
    @State private var selectedDashboardSection: DashboardSection

    init(store: BrokerStore) {
        self.store = store
#if DEBUG || DESKTOP_FIXTURES
        let requested = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_SECTION"]
        let initialSection: DashboardSection = switch requested {
        case "server-pool": .resources
        case "resource-usage", "leases": .leases
        case "settings": .settings
        default: .resources
        }
        _selectedDashboardSection = State(initialValue: initialSection)
#else
        _selectedDashboardSection = State(initialValue: .resources)
#endif
    }

    private var selectedGPUSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { selectedGPUID.map(StableRecordSelection.init(id:)) },
            set: { selectedGPUID = $0?.id }
        )
    }

    private var selectedEndpointSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { selectedEndpointDetailID.map(StableRecordSelection.init(id:)) },
            set: { selectedEndpointDetailID = $0?.id }
        )
    }

    private var editingEndpointSelection: Binding<StableRecordSelection?> {
        Binding(
            get: { editingEndpointID.map(StableRecordSelection.init(id:)) },
            set: { editingEndpointID = $0?.id }
        )
    }

    private func openFixtureDetailIfRequested(endpointIDs: [String]) {
#if DEBUG || DESKTOP_FIXTURES
        guard
            selectedEndpointDetailID == nil,
            let requestedID = ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_SELECTED_ENDPOINT"],
            endpointIDs.contains(requestedID)
        else { return }
        selectedEndpointDetailID = requestedID
#endif
    }

    var body: some View {
        // Keyed on the contrast generation: SwiftUI holds a resolved colour for
        // a view body's lifetime, so toggling Increase Contrast has to rebuild
        // the tree for the redrawn tokens to be read.
        rootBody.id(contrast.generation)
    }

    private var rootBody: some View {
        GeometryReader { proxy in
            let compactNavigation = proxy.size.width < 1180
            let sidebarWidth: CGFloat = compactNavigation ? 72 : 224

            ZStack {
                AmbientBackground()

                HStack(spacing: 0) {
                    AppSidebar(
                        store: store,
                        selectedSection: selectedDashboardSection,
                        compact: compactNavigation,
                        navigate: { selectedDashboardSection = $0 }
                    )
                    .frame(width: sidebarWidth)

                    Divider().opacity(DesignTokens.Alpha.muted)

                    VStack(spacing: 0) {
                        AppToolbar(
                            store: store,
                            selectedSection: selectedDashboardSection,
                            addServer: { showAddServer = true },
                            claimGPU: {
                                claimInitialEndpointID = ""
                                showClaim = true
                            },
                            refresh: store.reload
                        )
                        .fixedSize(horizontal: false, vertical: true)
                        DashboardView(
                            store: store,
                            addServer: { showAddServer = true },
                            claimGPU: {
                                claimInitialEndpointID = ""
                                showClaim = true
                            },
                            claimEndpoint: { endpointID in
                                claimInitialEndpointID = endpointID
                                showClaim = true
                            },
                            manageGroups: { showGroupManagement = true },
                            openEndpoint: { endpoint in
                                selectedEndpointDetailID = endpoint.id
                            },
                            selectedSection: $selectedDashboardSection,
                            selectGPU: { gpu in
                                selectedGPUID = gpu.id
                            }
                        )
                    }
                    .frame(
                        width: max(0, proxy.size.width - sidebarWidth - 1),
                        height: proxy.size.height
                    )
                    .clipped()
                    .background(Color.clear)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(DesignTokens.ambientSmoke)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .tint(DesignTokens.interaction)
        .sheet(isPresented: $showAddServer) {
            AddServerSheet(store: store)
        }
        .sheet(isPresented: $showClaim) {
            ClaimSheet(store: store, initialEndpointID: claimInitialEndpointID)
        }
        .sheet(isPresented: $showGroupManagement) {
            ManageServerGroupsSheet(store: store)
        }
        .sheet(item: selectedEndpointSelection) { selection in
            ServerDetailSheet(
                store: store,
                endpointID: selection.id,
                claim: {
                    selectedEndpointDetailID = nil
                    claimInitialEndpointID = selection.id
                    showClaim = true
                },
                edit: {
                    selectedEndpointDetailID = nil
                    DispatchQueue.main.async {
                        editingEndpointID = selection.id
                    }
                }
            )
        }
        .sheet(item: editingEndpointSelection) { selection in
            if let endpoint = store.snapshot.endpoint(id: selection.id) {
                EditServerSheet(store: store, endpoint: endpoint) {
                    selectedEndpointDetailID = nil
                    editingEndpointID = nil
                }
            }
        }
        .sheet(item: selectedGPUSelection) { selection in
            if let gpu = store.snapshot.gpu(id: selection.id) {
                GPUDetailSheet(gpu: gpu)
            }
        }
        .onAppear {
            openFixtureDetailIfRequested(endpointIDs: store.snapshot.endpoints.map(\.id))
        }
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            openFixtureDetailIfRequested(endpointIDs: endpointIDs)
            if let selectedEndpointDetailID, !endpointIDs.contains(selectedEndpointDetailID) {
                self.selectedEndpointDetailID = nil
            }
            if let editingEndpointID, !endpointIDs.contains(editingEndpointID) {
                self.editingEndpointID = nil
            }
        }
        .onChange(of: store.snapshot.gpus.map(\.id)) { _, gpuIDs in
            if let selectedGPUID, !gpuIDs.contains(selectedGPUID) {
                self.selectedGPUID = nil
            }
        }
    }
}

private struct AppSidebar: View {
    @ObservedObject var store: BrokerStore
    let selectedSection: DashboardSection
    let compact: Bool
    let navigate: (DashboardSection) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                        .fill(DesignTokens.interaction)
                    Image(systemName: "server.rack")
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(DesignTokens.onInteraction)
                }
                .frame(width: 36, height: 36)
                if !compact {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("ServerPilot")
                            .font(.title3.weight(.bold))
                            .foregroundStyle(DesignTokens.ink)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: compact ? .center : .leading)
            .padding(.horizontal, compact ? 10 : 18)
            .padding(.top, 30)
            .padding(.bottom, 25)

            SidebarSelection(title: "服务器", systemImage: "server.rack", color: DesignTokens.interaction, selected: selectedSection == .resources, compact: compact) {
                navigate(.resources)
            }
            SidebarSelection(title: "使用情况", systemImage: "chart.bar.xaxis", color: DesignTokens.interaction, selected: selectedSection == .leases, compact: compact) {
                navigate(.leases)
            }
            SidebarSelection(title: "设置", systemImage: "gearshape.fill", color: DesignTokens.interaction, selected: selectedSection == .settings, compact: compact) {
                navigate(.settings)
            }

            Spacer(minLength: 22)

            if !store.isConnected, let error = store.errorMessage {
                VStack(alignment: compact ? .center : .leading, spacing: 6) {
                HStack(spacing: 7) {
                    Circle()
                        .fill(DesignTokens.danger)
                        .frame(width: 7, height: 7)
                    if !compact {
                        Text(error)
                            .font(.callout.weight(.medium))
                            .foregroundStyle(DesignTokens.ink)
                            .lineLimit(2)
                    }
                }
                }
                .padding(.horizontal, compact ? 10 : 18)
                .padding(.vertical, 16)
                .overlay(alignment: .top) {
                    Divider().padding(.horizontal, 18)
                }
            }
        }
        .frame(maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.surface)
    }
}

private struct SidebarSelection: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let title: String
    let systemImage: String
    let color: Color
    let selected: Bool
    let compact: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 11) {
                Image(systemName: systemImage)
                    .font(.body.weight(.semibold))
                    .symbolRenderingMode(.hierarchical)
                    .foregroundStyle(selected ? color : DesignTokens.mutedInk)
                    .frame(width: 18)
                if !compact {
                    Text(title)
                        .font(Font.body.weight(selected ? .semibold : .medium))
                        .foregroundStyle(DesignTokens.ink)
                    Spacer()
                }
            }
            .padding(.horizontal, compact ? 0 : 15)
            .frame(height: 38)
            .frame(maxWidth: .infinity)
            .background(
                selected ? color.opacity(DesignTokens.Alpha.fill) : DesignTokens.ink.opacity(hovering ? 0.045 : 0),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
            )
        }
        .buttonStyle(.plain)
        .focusable()
        .padding(.horizontal, compact ? 12 : 10)
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(selected ? "当前页面" : "未选中")
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }
}

private struct AppToolbar: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    let selectedSection: DashboardSection
    let addServer: () -> Void
    let claimGPU: () -> Void
    let refresh: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.title.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(statusText)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 14)
            VStack(alignment: .trailing, spacing: 6) {
                HStack(spacing: 8) {
                    Button(action: addServer) {
                        Label("添加服务器", systemImage: "plus")
                            .font(.callout.weight(.semibold))
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .focusable()
                    .disabled(!store.allowsMutations)
                    .help(store.allowsMutations ? "添加服务器到本机资源池" : store.mutationUnavailableReason)
                    .accessibilityLabel("添加服务器")
                    Button(action: claimGPU) {
                        Label("申请 GPU", systemImage: "key.fill")
                            .font(.callout.weight(.semibold))
                    }
                    .buttonStyle(PrimaryActionButtonStyle())
                    .focusable()
                    .disabled(!store.allowsMutations || store.snapshot.operationalEndpoints.isEmpty)
                    .help(claimHelp)
                    .accessibilityLabel("申请 GPU")
                    .accessibilityHint(claimHint)
                    .accessibilityValue(claimHint)
                    Button(action: refresh) {
                        Label(store.isRefreshing ? "刷新中" : "刷新", systemImage: "arrow.clockwise")
                            .font(.callout.weight(.semibold))
                            .rotationEffect(.degrees(store.isRefreshing && !reduceMotion ? 360 : 0))
                            .animation(
                                store.isRefreshing && !reduceMotion ? .linear(duration: 0.8).repeatForever(autoreverses: false) : .easeOut(duration: 0.15),
                                value: store.isRefreshing
                            )
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .focusable()
                    .disabled(store.isRefreshing || !store.canRefresh)
                    .keyboardShortcut("r", modifiers: [.command])
                    .help(store.canRefresh ? "更新资源数据" : "测试数据不能刷新")
                    .accessibilityLabel("更新资源数据")
                    .accessibilityValue(store.isRefreshing ? "正在更新" : (store.canRefresh ? "可以更新" : "测试数据"))
                }
                if let error = store.errorMessage {
                    Label(error, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.danger)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .accessibilityLabel("当前刷新错误")
                        .accessibilityValue(error)
                }
            }
        }
        .padding(.horizontal, 28)
        .padding(.vertical, 14)
        .background(DesignTokens.surface)
    }

    private var statusText: String {
        if store.errorMessage != nil, store.lastGoodSnapshot != nil {
            return "连接已中断 · 显示上次数据"
        }
        if store.snapshot.snapshotRevision != nil {
            return "更新于 \(lastUpdatedText)"
        }
        return store.isConnected ? "正在读取资源" : "正在连接"
    }

    private var lastSuccessText: String {
        guard let lastUpdated = store.lastUpdated else { return "等待首次更新" }
        let elapsed = max(0, Int(Date().timeIntervalSince(lastUpdated)))
        return elapsed < 5 ? "刚刚" : "\(elapsed) 秒前"
    }

    private var lastUpdatedText: String {
        guard let lastUpdated = store.lastUpdated else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M 月 d 日 HH:mm"
        return formatter.string(from: lastUpdated)
    }

    private var title: String {
        switch selectedSection {
        case .resources: return "服务器"
        case .leases: return "使用情况"
        case .settings: return "设置"
        }
    }

    private var claimHelp: String {
        if !store.allowsMutations { return store.mutationUnavailableReason }
        if store.snapshot.operationalEndpoints.isEmpty { return "请先添加服务器" }
        if store.snapshot.serverGroups.isEmpty { return "申请空闲 GPU" }
        return "先选择服务器组，由控制面在组内选择服务器"
    }

    private var claimHint: String {
        store.snapshot.serverGroups.isEmpty
            ? "申请空闲 GPU"
            : "先选择服务器组，由控制面在组内选择服务器"
    }
}

private struct DashboardView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    let addServer: () -> Void
    let claimGPU: () -> Void
    let claimEndpoint: (String) -> Void
    let manageGroups: () -> Void
    let openEndpoint: (EndpointRecord) -> Void
    @Binding var selectedSection: DashboardSection
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let error = store.errorMessage {
                NoticeBanner(message: error, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if store.freshness == .stale {
                NoticeBanner(message: "连接已中断，显示上次数据。", color: DesignTokens.danger, icon: "wifi.exclamationmark")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if let notice = displayedNotice {
                NoticeBanner(message: notice, color: DesignTokens.success, icon: "checkmark.circle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            }

            Group {
                switch selectedSection {
                case .resources:
                    ResourcesDashboard(
                        store: store,
                        claimEndpoint: claimEndpoint,
                        manageGroups: manageGroups,
                        openEndpoint: openEndpoint,
                        selectGPU: selectGPU
                    )
                case .leases:
                    ResourceUsageDashboard(store: store, claimGPU: claimGPU)
                case .settings:
                    SettingsDashboard(store: store)
                }
            }
            .id(selectedSection)
            .transition(.opacity.combined(with: .offset(y: reduceMotion ? 0 : 6)))
            .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: selectedSection)
        }
        .background(Color.clear)
    }

    private var displayedNotice: String? {
        guard let notice = store.notice,
              notice.hasPrefix("已申领，待使用：")
        else { return store.notice }

        guard let runningLease = store.snapshot.leases.first(where: { lease in
            notice.contains(lease.id) && lease.runtimeState == "RUNNING"
        }) else { return notice }

        let task = runningLease.taskReference ?? runningLease.purpose ?? "未命名任务"
        return "任务占用：\(runningLease.projectID) · \(task) · \(runningLease.gpuIDs.count) GPU。"
    }
}

private struct SettingsDashboard: View {
    @ObservedObject var store: BrokerStore

    var body: some View {
        ScrollView {
            // The sidebar item and the page title already say "设置"; a third
            // section header repeats one piece of information three times,
            // which DESIGN_SYSTEM 4 forbids.
            VStack(alignment: .leading, spacing: 16) {
                HomeCard {
                    VStack(alignment: .leading, spacing: 14) {
                        CardSectionLabel(text: "本机服务")
                        SettingsFact(label: "服务地址", value: store.serviceAddress, icon: "network")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "版本", value: store.serviceInfo?.version ?? "未知", icon: "number")
                    }
                }

                HomeCard {
                    VStack(alignment: .leading, spacing: 14) {
                        CardSectionLabel(text: "数据状态")
                        SettingsFact(label: "连接", value: connectionValue, icon: "bolt.horizontal.circle")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "快照", value: snapshotValue, icon: "clock")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "清单", value: inventoryValue, icon: "server.rack")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "资源变更", value: store.allowsMutations ? "可执行" : store.mutationUnavailableReason, icon: "hand.raised")
                    }
                }

                if store.supportsCollectorSettings {
                    HomeCard {
                        VStack(alignment: .leading, spacing: 14) {
                            CardSectionLabel(text: "数据采集")
                            HStack(spacing: 12) {
                                SettingsIcon(icon: "clock.arrow.circlepath")
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("采集间隔")
                                        .font(Typography.identity)
                                        .foregroundStyle(DesignTokens.ink)
                                    Text("每台服务器的只读探针执行频率。")
                                        .font(Typography.metricLabel)
                                        .foregroundStyle(DesignTokens.mutedInk)
                                }
                                Spacer(minLength: 16)
                                Picker(
                                    "数据采集间隔",
                                    selection: Binding(
                                        get: { store.collectorSettings?.intervalSeconds ?? 10 },
                                        set: { store.updateCollectorInterval($0) { _, _ in } }
                                    )
                                ) {
                                    ForEach(store.collectorSettings?.allowedIntervals ?? [5, 10, 30], id: \.self) { seconds in
                                        Text("\(seconds) 秒").tag(seconds)
                                    }
                                }
                                .pickerStyle(.segmented)
                                .labelsHidden()
                                .frame(width: 210)
                                .accessibilityLabel("数据采集间隔")
                                .accessibilityValue("\(store.collectorSettings?.intervalSeconds ?? 10) 秒")
                                .disabled(
                                    store.collectorSettingsLoading
                                        || store.collectorSettings == nil
                                        || !store.canUpdateCollectorSettings
                                )
                            }
                        }
                    }
                }

                if store.supportsMcpEntry {
                    MCPEntryPanel(entry: store.mcpEntry, loading: store.mcpEntryLoading)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 24)
            .frame(maxWidth: 680, alignment: .leading)
            // Without this the ScrollView centres the capped column, which put
            // the settings cards in the middle of a wide window while every
            // other page starts at the left margin.
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.ambientSmoke)
    }

    private var connectionValue: String {
        // The fixture provider reports connected, so the read-only case has to
        // be tested first or it claims a live local service that is not there.
        guard store.canRefresh else { return "只读测试夹具" }
        guard store.isConnected else { return "未连接" }
        let address = store.serviceAddress
        return address.isEmpty || address == "—" ? "已连接" : "已连接 \(address)"
    }

    private var snapshotValue: String {
        let state = switch store.freshness {
        case .fresh: "最新"
        case .stale: "已过期"
        case .failed: "获取失败"
        case .waiting: "等待中"
        }
        guard let revision = store.snapshot.snapshotRevision else { return state }
        return "\(state) · 修订 \(revision)"
    }

    private var inventoryValue: String {
        let endpoints = store.snapshot.endpoints.count
        let gpus = store.snapshot.gpus.count
        let leases = store.snapshot.leases.count
        return "\(endpoints) 台服务器 · \(gpus) 张 GPU · \(leases) 个租约"
    }
}

/// Tinted square that carries a settings row's symbol.
private struct SettingsIcon: View {
    let icon: String

    var body: some View {
        Image(systemName: icon)
            .font(.callout.weight(.semibold))
            .foregroundStyle(DesignTokens.interaction)
            .frame(width: 30, height: 30)
            .background(
                DesignTokens.interaction.opacity(DesignTokens.Alpha.fill),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .accessibilityHidden(true)
    }
}

private struct SettingsFact: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        HStack(spacing: 12) {
            SettingsIcon(icon: icon)
            Text(label)
                .font(Typography.identity)
                .foregroundStyle(DesignTokens.ink)
            Spacer(minLength: 16)
            Text(value)
                .font(Typography.identity.weight(.semibold))
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .accessibilityElement(children: .combine)
    }
}

private struct MCPEntryPanel: View {
    let entry: MCPEntryRecord?
    let loading: Bool
    @State private var copiedToken: String?

    var body: some View {
        HomeCard {
            VStack(alignment: .leading, spacing: 14) {
                CardSectionLabel(text: "Agent MCP")
                if let entry, entry.available, let command = entry.command, let configJSON = entry.configJSON {
                    copyRow(label: "入口路径", value: command, token: "path")
                    Divider().opacity(DesignTokens.Alpha.strong)
                    copyBlock(label: "mcpServers 配置", value: configJSON, token: "config")
                } else if loading && entry == nil {
                    Text("正在读取 MCP 入口。")
                        .font(Typography.identity)
                        .foregroundStyle(DesignTokens.mutedInk)
                } else {
                    Text("未找到 MCP 入口。")
                        .font(Typography.identity)
                        .foregroundStyle(DesignTokens.ink)
                    if let hint = entry?.hint, !hint.isEmpty {
                        Text(hint)
                            .font(Typography.command)
                            .foregroundStyle(DesignTokens.mutedInk)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func copyRow(label: String, value: String, token: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                SettingsIcon(icon: "terminal")
                Text(label)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 16)
                copyButton(value: value, token: token, accessibilityLabel: "复制\(label)")
            }
            Text(value)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .lineLimit(2)
                .truncationMode(.middle)
        }
    }

    private func copyBlock(label: String, value: String, token: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                SettingsIcon(icon: "doc.on.clipboard")
                Text(label)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 16)
                copyButton(value: value, token: token, accessibilityLabel: "复制\(label)")
            }
            Text(value)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func copyButton(value: String, token: String, accessibilityLabel: String) -> some View {
        Button(copiedToken == token ? "已复制" : "复制") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
            copiedToken = token
        }
        .font(Typography.identity)
        .foregroundStyle(DesignTokens.interaction)
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
    }
}

private struct NoticeBanner: View {
    let message: String
    let color: Color
    let icon: String

    var body: some View {
        Label(message, systemImage: icon)
            .font(.body.weight(.medium))
            .foregroundStyle(DesignTokens.ink)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .background(color.opacity(DesignTokens.Alpha.fill), in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                    .stroke(color.opacity(DesignTokens.Alpha.muted), lineWidth: 1)
            )
    }
}

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

private struct ResourcesDashboard: View {
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

private struct EndpointTelemetryHistoryPanel: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    @State private var range: EndpointTelemetryRange = .oneHour

    private var history: EndpointTelemetryHistory? {
        store.endpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text("历史")
                    .font(.body.weight(.semibold))
                Spacer()
                Picker("时间范围", selection: $range) {
                    Text("1h").tag(EndpointTelemetryRange.oneHour)
                    Text("6h").tag(EndpointTelemetryRange.sixHours)
                    Text("24h").tag(EndpointTelemetryRange.twentyFourHours)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 126)
                .accessibilityLabel("资源历史时间范围")
            }

            if !store.supportsEndpointTelemetryHistory {
                DetailCallout(
                    icon: "chart.line.uptrend.xyaxis",
                    color: DesignTokens.warning,
                    message: "当前服务不支持历史数据。"
                )
            } else if store.endpointTelemetryHistoryLoading.contains(endpoint.id) {
                Label("正在载入 \(range.rawValue)", systemImage: "arrow.clockwise")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
                    .padding(.horizontal, 12)
                    .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            } else if let error = store.endpointTelemetryHistoryErrors[endpoint.id] {
                DetailCallout(icon: "exclamationmark.triangle.fill", color: DesignTokens.danger, message: error)
            } else if let history, !history.samples.isEmpty {
                if history.endpointID == endpoint.id {
                    EndpointTelemetryHistoryChart(history: history).equatable()
                } else {
                    DetailCallout(
                        icon: "exclamationmark.shield.fill",
                        color: DesignTokens.danger,
                        message: "历史数据校验失败。"
                    )
                }
            } else {
                DetailCallout(icon: "clock", color: DesignTokens.mutedInk, message: "所选时段暂无数据。")
            }
        }
        // History persists at a 60-second cadence. Refresh only while this
        // selected detail is visible, so the overview never fans out into one
        // request per endpoint and hidden charts cannot become a render driver.
        .task(id: "\(endpoint.id):\(range.rawValue)") {
            requestIfSupported()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                guard !Task.isCancelled else { return }
                requestIfSupported()
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("资源历史")
    }

    private func requestIfSupported() {
        guard store.supportsEndpointTelemetryHistory else { return }
        store.requestEndpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }
}

private struct EndpointTelemetryHistoryChart: View, Equatable {
    let history: EndpointTelemetryHistory

    static func == (lhs: Self, rhs: Self) -> Bool { lhs.history == rhs.history }

    var body: some View {
        let prepared = EndpointTelemetryPreparedHistory(history: history)
        VStack(alignment: .leading, spacing: 8) {
            EndpointTelemetryHistoryContext(prepared: prepared)
            if prepared.hostSamples.isEmpty {
                Label(
                    "所选时段暂无数据。",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption2.weight(.semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(maxWidth: .infinity, minHeight: 112, alignment: .leading)
            } else {
                LazyVGrid(
                    columns: [
                        GridItem(.flexible(minimum: 300), spacing: 12),
                        GridItem(.flexible(minimum: 300), spacing: 12),
                    ],
                    alignment: .leading,
                    spacing: 12
                ) {
                    EndpointTelemetryMetricChart(
                        title: "CPU 使用率",
                        subtitle: "",
                        series: prepared.cpuSeries,
                        hoverItems: prepared.cpuHoverItems,
                        emptyMessage: "无 CPU 历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "内存占用率",
                        subtitle: "",
                        series: prepared.memorySeries,
                        hoverItems: prepared.memoryHoverItems,
                        emptyMessage: "无内存历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "GPU 利用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuUtilizationSeries,
                        hoverItems: prepared.gpuUtilizationHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无 GPU 利用率数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "显存占用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuMemorySeries,
                        hoverItems: prepared.gpuMemoryHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无显存历史数据"
                    )
                }
                if prepared.gpuUtilizationSeries.count > 1 {
                    EndpointTelemetryGPULegend(series: prepared.gpuUtilizationSeries)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("端点资源历史")
        .accessibilityValue(prepared.accessibilityValue)
    }
}

private struct EndpointTelemetryHistoryContext: View {
    let prepared: EndpointTelemetryPreparedHistory

    var body: some View {
        HStack(spacing: 8) {
            Label(prepared.lastObservationLabel, systemImage: "clock")
            if let warning = prepared.visualWarningLabel {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(DesignTokens.warning)
            }
            Spacer(minLength: 0)
        }
        .font(Typography.annotation.weight(.semibold))
        .foregroundStyle(DesignTokens.mutedInk)
        .lineLimit(1)
    }
}

private struct EndpointTelemetryMetricChart: View, Equatable {
    let title: String
    let subtitle: String
    let series: [EndpointTelemetryLineSeries]
    let hoverItems: [EndpointTelemetryHoverItem]
    let emptyMessage: String

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.title == rhs.title
            && lhs.subtitle == rhs.subtitle
            && lhs.series == rhs.series
            && lhs.hoverItems == rhs.hoverItems
            && lhs.emptyMessage == rhs.emptyMessage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(title).font(.callout.weight(.semibold))
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }

            if series.allSatisfy({ $0.points.isEmpty }) {
                Label(emptyMessage, systemImage: "chart.line.flattrend.xyaxis")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
            } else {
#if canImport(Charts)
                chart
#else
                Text("当前系统没有 Swift Charts，使用文本降级。")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
#endif
            }
        }
        .padding(7)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 0.8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
    }

    private var accessibilitySummary: String {
        let summaries = series.compactMap { line -> String? in
            guard
                let latest = line.points.max(by: { $0.timestamp < $1.timestamp }),
                let minimum = line.points.map(\.value).min(),
                let maximum = line.points.map(\.value).max()
            else { return nil }
            return "\(line.label)：最新 \(historyPercent(latest.value))，最低 \(historyPercent(minimum))，最高 \(historyPercent(maximum))"
        }
        return summaries.isEmpty ? emptyMessage : summaries.joined(separator: "；")
    }

#if canImport(Charts)
    private var chart: some View {
        Chart {
            ForEach(series) { line in
                ForEach(line.points) { point in
                    LineMark(
                        x: .value("观测时间", point.timestamp),
                        y: .value("使用率", point.value),
                        series: .value("连续片段", point.segmentID)
                    )
                    .foregroundStyle(line.color)
                    .interpolationMethod(.linear)
                    .lineStyle(StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                }
            }
        }
        .chartYScale(domain: 0...1)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: [0, 0.5, 1]) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel {
                    if let fraction = value.as(Double.self) { Text(historyPercent(fraction)) }
                }
            }
        }
        .chartLegend(.hidden)
        .chartOverlay { proxy in
            EndpointTelemetryChartHoverOverlay(proxy: proxy, items: hoverItems)
        }
        .frame(height: 160)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
        .accessibilityHint("将指针悬停在图表上可检查最近的观测样本。")
    }
#endif
}

private struct EndpointTelemetryGPULegend: View {
    let series: [EndpointTelemetryLineSeries]

    private static let visibleLimit = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 86), spacing: 8)], alignment: .leading, spacing: 6) {
                ForEach(Array(series.prefix(Self.visibleLimit))) { line in
                    HStack(spacing: 4) {
                        Capsule().fill(line.color).frame(width: 12, height: 3)
                        Text(line.label).lineLimit(1)
                    }
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                }
            }
            if series.count > Self.visibleLimit {
                Text("另 \(series.count - Self.visibleLimit) 张未列出")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityHidden(true)
    }
}

#if canImport(Charts)
private struct EndpointTelemetryChartHoverOverlay: View {
    let proxy: ChartProxy
    let items: [EndpointTelemetryHoverItem]
    @State private var selectedIndex: Int?

    var body: some View {
        GeometryReader { geometry in
            if let anchor = proxy.plotFrame {
                let frame = geometry[anchor]
                Rectangle()
                    .fill(.clear)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location):
                            let x = location.x - frame.origin.x
                            guard x >= 0, x <= frame.width, let date = proxy.value(atX: x, as: Date.self) else { return }
                            updateSelection(for: date)
                        case .ended:
                            updateSelection(to: nil)
                        }
                    }
                    .overlay {
                        if let selected = selectedItem, let position = proxy.position(forX: selected.timestamp) {
                            let x = frame.minX + position
                            let cardWidth = min(
                                EndpointTelemetryHoverCard.preferredWidth(for: selected.entries.count),
                                max(184, geometry.size.width - 8)
                            )
                            let halfCardWidth = cardWidth / 2
                            Path { path in
                                path.move(to: CGPoint(x: x, y: frame.minY))
                                path.addLine(to: CGPoint(x: x, y: frame.maxY))
                            }
                            .stroke(DesignTokens.ink.opacity(DesignTokens.Alpha.strong), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                            .allowsHitTesting(false)
                            EndpointTelemetryHoverCard(item: selected, width: cardWidth)
                                .position(
                                    x: min(max(4 + halfCardWidth, x), geometry.size.width - 4 - halfCardWidth),
                                    y: frame.midY
                                )
                                .allowsHitTesting(false)
                        }
                    }
            } else {
                Color.clear
            }
        }
    }

    private var selectedItem: EndpointTelemetryHoverItem? {
        guard let selectedIndex, items.indices.contains(selectedIndex) else { return nil }
        return items[selectedIndex]
    }

    private func updateSelection(for date: Date) {
        guard !items.isEmpty else { return }
        var lowerBound = 0
        var upperBound = items.count
        while lowerBound < upperBound {
            let middle = (lowerBound + upperBound) / 2
            if items[middle].timestamp < date { lowerBound = middle + 1 } else { upperBound = middle }
        }
        let candidate: Int
        if lowerBound == 0 {
            candidate = 0
        } else if lowerBound == items.count {
            candidate = items.count - 1
        } else {
            let earlier = items[lowerBound - 1]
            let later = items[lowerBound]
            candidate = abs(earlier.timestamp.timeIntervalSince(date)) <= abs(later.timestamp.timeIntervalSince(date))
                ? lowerBound - 1 : lowerBound
        }
        updateSelection(to: candidate)
    }

    private func updateSelection(to index: Int?) {
        guard selectedIndex != index else { return }
        var transaction = Transaction()
        transaction.animation = nil
        withTransaction(transaction) { selectedIndex = index }
    }
}

private struct EndpointTelemetryHoverCard: View {
    let item: EndpointTelemetryHoverItem
    let width: CGFloat

    static func preferredWidth(for entryCount: Int) -> CGFloat {
        entryCount > 4 ? 316 : 184
    }

    private var columns: [GridItem] {
        Array(
            repeating: GridItem(.flexible(minimum: 116), spacing: 8, alignment: .leading),
            count: item.entries.count > 4 ? 2 : 1
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(historyDateTime(item.timestamp))
                .font(.system(.caption2, design: .monospaced).weight(.bold))
                .foregroundStyle(DesignTokens.ink)
            ScrollView(.vertical) {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                    ForEach(item.entries) { entry in
                        HStack(spacing: 5) {
                            Capsule()
                                .fill(entry.color)
                                .frame(width: 12, height: 3)
                            Text(entry.label)
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            Text(entry.value)
                                .fontWeight(.bold)
                        }
                        .font(.system(.caption2, design: .monospaced).weight(.medium))
                        .foregroundStyle(DesignTokens.ink)
                    }
                }
            }
            .scrollIndicators(.hidden)
            .frame(maxHeight: 146)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .frame(width: width, alignment: .leading)
        .frame(maxHeight: 176)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .shadow(color: .black.opacity(DesignTokens.Alpha.fill), radius: 5, y: 2)
    }
}
#endif

private struct EndpointTelemetryPreparedHistory: Equatable {
    let range: EndpointTelemetryRange
    let hostSamples: [EndpointTelemetryPreparedHostSample]
    let gpuSeries: [EndpointTelemetryPreparedGPUSeries]
    let cpuSeries: [EndpointTelemetryLineSeries]
    let memorySeries: [EndpointTelemetryLineSeries]
    let gpuUtilizationSeries: [EndpointTelemetryLineSeries]
    let gpuMemorySeries: [EndpointTelemetryLineSeries]
    let cpuHoverItems: [EndpointTelemetryHoverItem]
    let memoryHoverItems: [EndpointTelemetryHoverItem]
    let gpuUtilizationHoverItems: [EndpointTelemetryHoverItem]
    let gpuMemoryHoverItems: [EndpointTelemetryHoverItem]
    let rejectedHostSampleCount: Int
    let generatedAt: Date?
    let hostSamplingGapCount: Int

    init(history: EndpointTelemetryHistory) {
        range = history.range
        generatedAt = history.generatedAt.flatMap(endpointTelemetryHistoryDate)
        var seenHostIDs = Set<String>()
        let decodedHost = history.samples.compactMap(EndpointTelemetryPreparedHostSample.init).sorted { $0.timestamp < $1.timestamp }
        hostSamples = decodedHost.filter { seenHostIDs.insert($0.id).inserted }
        rejectedHostSampleCount = history.samples.count - hostSamples.count
        hostSamplingGapCount = EndpointTelemetryPreparedHistory.gapCount(in: hostSamples.map(\.timestamp))

        cpuSeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "cpu", label: "CPU", color: DesignTokens.chartSeries[0],
            samples: hostSamples.map { ($0.timestamp, $0.cpuFraction) }
        )]
        memorySeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "memory", label: "内存", color: DesignTokens.chartSeries[2],
            samples: hostSamples.map { ($0.timestamp, $0.memoryFraction) }
        )]
        cpuHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "CPU 占用",
                entries: [EndpointTelemetryHoverEntry(id: "cpu", label: "CPU", value: historyPercent($0.cpuFraction), color: DesignTokens.chartSeries[0])]
            )
        }
        memoryHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "内存占用",
                entries: [EndpointTelemetryHoverEntry(id: "memory", label: "内存", value: historyPercent($0.memoryFraction), color: DesignTokens.chartSeries[2])]
            )
        }

        let preparedGPUs = history.gpuSeries
            .map(EndpointTelemetryPreparedGPUSeries.init)
            .sorted { $0.index < $1.index }
        gpuSeries = preparedGPUs
        // Stale device rows linger after a container rebuild: same gpu_index,
        // empty sample arrays in this window.  They must not occupy a chart
        // series or a legend slot.  Color stays aligned by visible GPU.
        let visibleGPUs = preparedGPUs.filter { !$0.samples.isEmpty }
        gpuUtilizationSeries = visibleGPUs.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: gpu.id, label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.gpuUtilizationFraction) }
            )
        }
        gpuMemorySeries = visibleGPUs.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: "\(gpu.id)-memory", label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.memoryFraction) }
            )
        }
        gpuUtilizationHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuUtilizationSeries,
            prefix: "GPU 利用率"
        )
        gpuMemoryHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuMemorySeries,
            prefix: "GPU 显存"
        )
    }

    var lastObservationLabel: String {
        guard let latest = hostSamples.last else { return "时间未知" }
        return "更新于 \(historyShortTime(latest.timestamp))"
    }

    var visualWarningLabel: String? {
        if rejectedHostSampleCount > 0, hostSamplingGapCount > 0 {
            return "\(rejectedHostSampleCount) 异常 · \(hostSamplingGapCount) 断点"
        }
        if rejectedHostSampleCount > 0 { return "\(rejectedHostSampleCount) 异常" }
        if hostSamplingGapCount > 0 { return "\(hostSamplingGapCount) 断点" }
        if hostSamples.count > 1, hostSamples.count < 3 { return "样本不足" }
        return nil
    }

    var freshnessAndTrustLabel: String {
        var contexts = [freshnessLabel]
        if rejectedHostSampleCount > 0 { contexts.append("已省略 \(rejectedHostSampleCount) 个无法验证的主机样本。") }
        if hostSamples.count < 3, hostSamples.count > 1 {
            contexts.append("样本不足 3 个，未假设连续采样。")
        } else if hostSamplingGapCount > 0 {
            contexts.append("发现 \(hostSamplingGapCount) 段采样间隔，趋势已在间隔处断开。")
        }
        contexts.append("历史趋势只供检查；资源申请仍以当前快照和端点状态为准。")
        return contexts.joined(separator: " ")
    }

    var accessibilityValue: String {
        "范围 \(range.rawValue)，已验证主机样本 \(hostSamples.count) 个，GPU 序列 \(gpuUtilizationSeries.count) 条。\(freshnessAndTrustLabel)"
    }

    private var freshnessLabel: String {
        guard let generatedAt else { return "服务未提供历史响应生成时间，无法判定历史数据新鲜度。" }
        guard let latest = hostSamples.last else { return "响应生成于 \(historyDateTime(generatedAt))，但没有可验证的观测样本。" }
        let lag = generatedAt.timeIntervalSince(latest.timestamp)
        guard lag >= 0 else { return "响应生成时间早于最后观测时间，无法判定历史数据新鲜度。" }
        return "响应生成于 \(historyDateTime(generatedAt))；最后观测落后 \(historyElapsedDescription(lag))。"
    }

    private static func lineSeries(
        id: String, label: String, color: Color, samples: [(Date, Double?)]
    ) -> EndpointTelemetryLineSeries {
        let threshold = gapThreshold(samples.map(\.0))
        var points: [EndpointTelemetryChartPoint] = []
        var segment = 0
        var previousTimestamp: Date?
        for (timestamp, value) in samples {
            if let previousTimestamp, let threshold, timestamp.timeIntervalSince(previousTimestamp) > threshold { segment += 1 }
            guard let value else {
                segment += 1
                previousTimestamp = timestamp
                continue
            }
            points.append(EndpointTelemetryChartPoint(
                id: "\(id)-\(timestamp.timeIntervalSince1970)-\(segment)", timestamp: timestamp, value: value, segmentID: "\(id)-\(segment)"
            ))
            previousTimestamp = timestamp
        }
        return EndpointTelemetryLineSeries(id: id, label: label, color: color, points: points)
    }

    private static func hoverItems(series: [EndpointTelemetryLineSeries], prefix: String) -> [EndpointTelemetryHoverItem] {
        var values = [Date: [EndpointTelemetryHoverEntry]]()
        for line in series {
            for point in line.points {
                values[point.timestamp, default: []].append(
                    EndpointTelemetryHoverEntry(
                        id: line.id,
                        label: line.label,
                        value: historyPercent(point.value),
                        color: line.color
                    )
                )
            }
        }
        return values.map { timestamp, entries in
            EndpointTelemetryHoverItem(timestamp: timestamp, title: prefix, entries: entries)
        }.sorted { $0.timestamp < $1.timestamp }
    }

    private static func gapCount(in timestamps: [Date]) -> Int {
        guard let threshold = gapThreshold(timestamps) else { return 0 }
        return zip(timestamps, timestamps.dropFirst()).filter { $1.timeIntervalSince($0) > threshold }.count
    }

    private static func gapThreshold(_ timestamps: [Date]) -> TimeInterval? {
        let intervals = zip(timestamps, timestamps.dropFirst()).map { $1.timeIntervalSince($0) }.filter { $0 > 0 }
        guard intervals.count >= 2 else { return nil }
        let sorted = intervals.sorted()
        let middle = sorted.count / 2
        let median = sorted.count.isMultiple(of: 2) ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
        return median * 2.5
    }

    private static func gpuColor(_ index: Int) -> Color {
        DesignTokens.chartSeries[index % DesignTokens.chartSeries.count]
    }
}

private struct EndpointTelemetryPreparedHostSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let cpuFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointTelemetrySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp), sample.status.map({ ["ONLINE", "OK"].contains($0) }) ?? true else { return nil }
        let cpu = sample.cpuLoadFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard cpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        cpuFraction = cpu
        memoryFraction = memory
    }
}

private struct EndpointTelemetryPreparedGPUSeries: Identifiable, Equatable {
    let id: String
    let index: Int
    let label: String
    let samples: [EndpointTelemetryPreparedGPUSample]

    init(_ series: EndpointGPUHistorySeries) {
        let samples = series.samples.compactMap(EndpointTelemetryPreparedGPUSample.init).sorted { $0.timestamp < $1.timestamp }
        id = series.id
        index = series.index
        label = series.label
        self.samples = samples
    }
}

private struct EndpointTelemetryPreparedGPUSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let gpuUtilizationFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointGPUHistorySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp) else { return nil }
        let gpu = sample.gpuUtilizationFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard gpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        gpuUtilizationFraction = gpu
        memoryFraction = memory
    }
}

private struct EndpointTelemetryLineSeries: Identifiable, Equatable {
    let id: String
    let label: String
    let color: Color
    let points: [EndpointTelemetryChartPoint]

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.points == rhs.points
    }
}

private struct EndpointTelemetryChartPoint: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let value: Double
    let segmentID: String
}

private struct EndpointTelemetryHoverItem: Identifiable, Equatable {
    var id: Date { timestamp }
    let timestamp: Date
    let title: String
    let entries: [EndpointTelemetryHoverEntry]

    var summary: String {
        "\(title)：\(entries.map { "\($0.label) \($0.value)" }.joined(separator: " · "))"
    }
}

private struct EndpointTelemetryHoverEntry: Identifiable, Equatable {
    let id: String
    let label: String
    let value: String
    let color: Color

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.value == rhs.value
    }
}

private func endpointTelemetryHistoryDate(_ value: String) -> Date? {
    EndpointTelemetryHistoryDateParser.fractional.date(from: value)
        ?? EndpointTelemetryHistoryDateParser.standard.date(from: value)
}

private enum EndpointTelemetryHistoryDateParser {
    static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    static let standard = ISO8601DateFormatter()
}

private func endpointTelemetryHistoryFraction(_ value: Double) -> Double? {
    guard value.isFinite, (0...1).contains(value) else { return nil }
    return value
}

private func historyPercent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int((value * 100).rounded()))%"
}

private func historyDateTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm:ss"
    return formatter.string(from: value)
}

private func historyShortTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: value)
}

private func historyElapsedDescription(_ value: TimeInterval) -> String {
    let seconds = max(0, Int(value.rounded()))
    if seconds < 60 { return "\(seconds) 秒" }
    if seconds < 3_600 { return "\(seconds / 60) 分钟" }
    return "\(seconds / 3_600) 小时 \((seconds % 3_600) / 60) 分钟"
}

private func durationLabel(_ seconds: Int) -> String {
    if seconds <= 0 { return "\(seconds) 秒" }
    if seconds % 3_600 == 0 {
        let hours = seconds / 3_600
        return hours == 1 ? "1 小时" : "\(hours) 小时"
    }
    if seconds % 60 == 0 {
        let minutes = seconds / 60
        return minutes == 1 ? "1 分钟" : "\(minutes) 分钟"
    }
    if seconds > 3_600 {
        let hours = seconds / 3_600
        let minutes = (seconds % 3_600) / 60
        if minutes == 0 { return hours == 1 ? "1 小时" : "\(hours) 小时" }
        return "\(hours) 小时 \(minutes) 分钟"
    }
    return "\(seconds) 秒"
}

private func memoryMiBLabel(_ mib: Int) -> String {
    if mib > 0, mib % 1024 == 0 {
        return "\(mib / 1024) GB"
    }
    return "\(mib) MiB"
}

@MainActor
private func endpointGPUAvailabilitySummary(
    _ endpoints: [EndpointRecord],
    store: BrokerStore
) -> (available: Int, total: Int) {
    let gpus = endpoints.flatMap { store.snapshot.gpus(for: $0) }
    let available = store.freshness == .fresh
        ? endpoints
            .filter { $0.monitorStatus == "ONLINE" }
            .flatMap { store.snapshot.gpus(for: $0) }
            .filter(\.isPubliclyAvailable)
            .count
        : 0
    return (available, gpus.count)
}

/// Inventory counts stay on per-card rows.  Delegated capacity uses the
/// one-apply cap, never pool `free_gpu_count`.  `limits` and
/// `largest_allocatable_block` exist on every group; they are not a
/// delegated signal — only `allocation == delegated` or a member
/// `schedulerCapacity` is.
@MainActor
private func endpointGroupCapacitySummary(
    _ endpoints: [EndpointRecord],
    group: ServerGroupRecord?,
    store: BrokerStore
) -> String {
    let inventory = endpointGPUAvailabilitySummary(endpoints, store: store)
    let inventoryPart: String?
    if inventory.total > 0 {
        inventoryPart = store.freshness == .fresh
            ? "\(inventory.available)/\(inventory.total) 空闲"
            : "空闲未确认 · \(inventory.total) 张"
    } else {
        inventoryPart = nil
    }
    let hasOnDemand = group?.allocation == .delegated
        || endpoints.contains { $0.schedulerCapacity != nil }
    let onDemandPart: String?
    if hasOnDemand {
        if let limit = onDemandApplyLimit(group: group, endpoints: endpoints) {
            onDemandPart = "按需申请 · 一次最多 \(limit) 卡"
        } else {
            onDemandPart = "按需申请"
        }
    } else {
        onDemandPart = nil
    }
    switch (inventoryPart, onDemandPart) {
    case (let inventory?, let onDemand?):
        return "\(inventory) · \(onDemand)"
    case (let inventory?, nil):
        return inventory
    case (nil, let onDemand?):
        return onDemand
    case (nil, nil):
        return "无 GPU"
    }
}

private func onDemandApplyLimit(group: ServerGroupRecord?, endpoints: [EndpointRecord]) -> Int? {
    if let block = group?.largestAllocatableBlock { return block }
    if let leaseMax = group?.limits?.maxGPUsPerLease { return leaseMax }
    let endpointLimits = endpoints.compactMap { endpoint -> Int? in
        endpoint.schedulerCapacity?.maxGPUsPerLease ?? endpoint.schedulerCapacity?.largestFreeBlock
    }
    return endpointLimits.max()
}

/// "60 核（容器配额）".  Without the note a container's budget reads as the
/// size of the whole machine, which is the number a caller must not plan with.
private func scopedFact(_ text: String, note: String?) -> String {
    guard let note else { return text }
    return "\(text)（\(note)）"
}

private func endpointGPUModelSummary(_ gpus: [GPURecord]) -> String {
    guard !gpus.isEmpty else { return "无 GPU" }
    let names = Array(Set(gpus.map(\.name))).sorted()
    guard let first = names.first else { return "无 GPU" }
    return names.count == 1 ? first : "\(first) +\(names.count - 1) 类"
}

private func endpointOverviewGPUMemoryFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.memoryFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}

private func endpointOverviewGPUUtilizationFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    let values = gpus.compactMap { $0.recentTelemetryAverage?.utilizationFraction }
    guard !values.isEmpty else { return nil }
    return min(max(values.reduce(0, +) / Double(values.count), 0), 1)
}

private func endpointOverviewCPULoadFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.cpuLoadFraction
}

private func endpointOverviewMemoryFraction(endpoint: EndpointRecord) -> Double? {
    guard endpoint.monitorStatus == "ONLINE" else { return nil }
    return endpoint.recentTelemetryAverage?.memoryFraction
}

private func percentageLabel(_ value: Double?) -> String {
    value.map { "\(Int(($0 * 100).rounded()))%" } ?? "—"
}

private let endpointHighPressureThreshold = 0.85

private func endpointPressureFraction(endpoint: EndpointRecord, gpus: [GPURecord]) -> Double? {
    [
        endpointOverviewCPULoadFraction(endpoint: endpoint),
        endpointOverviewMemoryFraction(endpoint: endpoint),
        endpointOverviewGPUUtilizationFraction(endpoint: endpoint, gpus: gpus)
    ]
    .compactMap { $0 }
    .max()
}

private func endpointHighPressure(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    guard let pressure = endpointPressureFraction(endpoint: endpoint, gpus: gpus) else { return false }
    return pressure >= endpointHighPressureThreshold
}

private func endpointRequiresAttention(endpoint: EndpointRecord, gpus: [GPURecord]) -> Bool {
    endpointNeedsAttention(endpoint)
        || gpus.contains(where: gpuNeedsAttention)
        || endpointHighPressure(endpoint: endpoint, gpus: gpus)
}

private func pressureColor(_ fraction: Double?) -> Color {
    guard let fraction else { return DesignTokens.mutedInk }
    switch fraction {
    case ..<0.70: return DesignTokens.success
    case ..<0.90: return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

private func endpointNeedsAttention(_ endpoint: EndpointRecord) -> Bool {
    ["ERROR", "STALE", "DISABLED", "DRAINING"].contains(endpoint.monitorStatus)
        || !endpoint.enabled
}

private func gpuNeedsAttention(_ gpu: GPURecord) -> Bool {
    [
        "BUSY_UNMANAGED",
        "UNKNOWN_RECOVERING",
        "UNKNOWN_STALE",
        "UNHEALTHY",
        "CONFLICT",
        "ORPHANED_BUSY",
        "DISABLED",
        "DRAINING",
        "MAINTENANCE"
    ].contains(gpu.state)
}

/// Older brokers can report a workload's ordinary worker replacement as a
/// process-attribution conflict.  That is distinct from a keeper or foreign
/// process conflict, both of which remain error/attention states.
private func gpuHasLegacyWorkloadProcessReview(_ gpu: GPURecord) -> Bool {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    return gpu.state == "CONFLICT"
        && gpu.stateReason == "lease/process attribution conflict"
        && gpu.keepalive.leaseID == nil
        && task?.isEmpty == false
}

private func gpuTaskObservationLabel(_ gpu: GPURecord) -> String? {
    let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        if let task, !task.isEmpty {
            return "任务：\(task) · 观测到进程已更新"
        }
        return "任务已指派 · 观测到进程已更新"
    }
    if let task, !task.isEmpty, !gpu.isPubliclyAvailable {
        return "任务：\(task)"
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "观测到计算任务"
    }
    return nil
}

private func gpuStateColor(_ state: String) -> Color {
    switch state {
    case "AVAILABLE": return DesignTokens.success
    // Held is real state but not a warning, and a user-chosen accent could sit
    // anywhere on the luminance scale — a neutral keeps the graphical floor.
    case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.hold
    case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED", "DRAINING", "MAINTENANCE": return DesignTokens.warning
    default: return DesignTokens.danger
    }
}

private func gpuStateLabel(_ state: String) -> String {
    switch state {
    case "AVAILABLE": return "可用 · 未开启占卡"
    case "HELD", "LEASED_IDLE", "RUNNING_MANAGED": return "使用中"
    case "KEEPALIVE": return "可用 · 空闲占卡"
    case "BUSY_UNMANAGED", "ORPHANED_BUSY": return "任务占用"
    case "RESERVED": return "不可分配"
    case "UNKNOWN_RECOVERING": return "正在连接"
    case "UNKNOWN_STALE": return "采集延迟"
    case "UNHEALTHY": return "GPU 故障"
    case "CONFLICT": return "归属待确认"
    case "DISABLED": return "已停用"
    case "MAINTENANCE", "DRAINING": return "不可分配"
    default: return "不可分配"
    }
}

func gpuPresentationLabel(_ gpu: GPURecord) -> String {
    if let publicStatus = gpu.publicStatus {
        return publicStatus
    }
    if gpuHasLegacyWorkloadProcessReview(gpu) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        return task.map { "任务占用 · \($0)" } ?? "任务占用 · 进程已更新"
    }
    if gpu.keepalive.state == "ERROR" {
        let reason = gpu.keepalive.reason.map(localizedStateReason) ?? "未知原因"
        return gpu.isPubliclyAvailable
            ? "可用 · 占卡异常：\(reason)"
            : "占卡异常：\(reason)"
    }
    if gpu.state == "AVAILABLE" {
        return gpu.keepalive.desired == "ON"
            ? "可用 · 占卡未运行"
            : "可用 · 未开启占卡"
    }
    if gpu.state == "KEEPALIVE" { return "可用 · 空闲占卡" }
    if ["HELD", "LEASED_IDLE", "RUNNING_MANAGED"].contains(gpu.state) {
        let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let task, !task.isEmpty else { return "使用中" }
        return task
    }
    if ["BUSY_UNMANAGED", "ORPHANED_BUSY"].contains(gpu.state) {
        return "任务占用"
    }
    return gpuStateLabel(gpu.state)
}

private func gpuVisualStateColor(_ gpu: GPURecord) -> Color {
    gpuHasLegacyWorkloadProcessReview(gpu) ? DesignTokens.warning : gpuStateColor(gpu.state)
}

private func endpointStateIcon(_ state: String) -> String {
    switch state {
    case "ONLINE": return "server.rack"
    case "PENDING": return "hourglass"
    case "STALE": return "clock.badge.exclamationmark"
    case "ERROR": return "exclamationmark.triangle.fill"
    case "DISABLED": return "pause.circle.fill"
    case "DRAINING": return "arrow.down.forward.and.arrow.up.backward"
    default: return "questionmark.diamond.fill"
    }
}

/// DISABLED/DRAINING are a human's own setting, not a connection failure;
/// painting them danger-red would say the opposite of what happened. Shared
/// by every non-ONLINE status color so the mapping lives in one table.
private func endpointMonitorStatusColor(_ state: String) -> Color {
    switch state {
    case "ERROR": return DesignTokens.danger
    case "DISABLED", "DRAINING": return DesignTokens.mutedInk
    default: return DesignTokens.warning
    }
}

/// The footer says why the sheet is not offering a claim.  A server a person
/// stopped is not a server that went quiet, so the two get different sentences.
private func endpointFooterMessage(_ endpoint: EndpointRecord) -> String {
    switch endpoint.monitorStatus {
    case "ONLINE": return "状态按设定周期自动更新"
    case "DISABLED": return "这台服务器已停用，暂不可申请 GPU"
    case "DRAINING": return "这台服务器正在排空，暂不接收新申请"
    case "PENDING": return "正在进行首次连接，暂不可申请 GPU"
    default: return "当前数据已过期，暂不可申请 GPU"
    }
}

private func localizedStateReason(_ reason: String) -> String {
    if reason == "no fresh telemetry after service start" {
        return "正在进行首次连接"
    }
    if reason == "GPU absent from latest complete endpoint observation" {
        return "本次更新未检测到这块 GPU"
    }
    if reason == "endpoint or GPU is disabled" {
        return "服务器或 GPU 已停用"
    }
    if reason == "lease/process attribution conflict" {
        return "此前观测到的任务进程与租约绑定不匹配；请由所属任务确认当前观测，或在任务结束后释放租约"
    }
    if reason == "lease expired while a compute process remains" {
        return "资源使用记录已到期，但服务器上仍有任务运行"
    }
    if reason == "bound workload process observed" || reason == "compute process observed on assigned GPU" {
        return "已指派任务，当前检测到计算进程"
    }
    if reason == "compute process observed; admission blocked" {
        return "检测到未登记的计算进程，暂不能分配"
    }
    if reason == "exclusive lease active" {
        return "资源已分配给其他项目或任务"
    }
    if reason.hasPrefix("telemetry age "), reason.contains("exceeds stale threshold") {
        return "最近一次服务器数据已过期"
    }
    if reason.hasPrefix("reservation "), reason.contains(" is active") {
        return "预约正在生效"
    }
    return "状态需要人工确认"
}

private func formattedTimestamp(_ value: String?) -> String {
    guard let value else { return "未知" }
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return "时间格式异常" }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm"
    return formatter.string(from: date)
}

private func historyTimestamp(_ value: String) -> String {
    let parser = ISO8601DateFormatter()
    parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let date = parser.date(from: value) ?? ISO8601DateFormatter().date(from: value)
    guard let date else { return value }
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: date)
}

private struct ServerDetailSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let endpointID: String
    let claim: () -> Void
    let edit: () -> Void

    private var endpoint: EndpointRecord? {
        store.snapshot.endpoint(id: endpointID)
    }

    private var gpus: [GPURecord] {
        guard let endpoint else { return [] }
        return store.snapshot.gpus(for: endpoint)
    }

    private var availableGPUCount: Int {
        guard let endpoint else { return 0 }
        guard endpoint.monitorStatus == "ONLINE" else { return 0 }
        return gpus.filter(\.isPubliclyAvailable).count
    }

    private var conflictedLeases: [LeaseRecord] {
        reclaimableLeases.filter { lease in
            lease.gpuIDs.contains { gpuID in
                gpus.first(where: { $0.id == gpuID })?.state == "CONFLICT"
            }
        }
    }

    private var reclaimableLeases: [LeaseRecord] {
        store.snapshot.leases.filter { lease in
            guard lease.kind == "workload",
                  ["HELD", "ACTIVE", "CONFLICT"].contains(lease.state),
                  !lease.gpuIDs.isEmpty else { return false }
            return lease.gpuIDs.allSatisfy { gpuID in
                guard let gpu = gpus.first(where: { $0.id == gpuID }) else { return false }
                return ["HELD", "LEASED_IDLE", "CONFLICT"].contains(gpu.state)
            }
        }
    }

    private var reclaimableKeepaliveLeaseIDs: [String] {
        guard let endpoint else { return [] }
        let policyDisabled = endpoint.keepalive.policy == "disabled"
        let ids = gpus.compactMap { gpu -> String? in
            guard let leaseID = gpu.keepalive.leaseID else { return nil }
            // A normal start owns a held per-GPU lease while the helper starts.
            // It is not a stale record. Recovery appears only
            // after the user has asked to stop occupancy but that lease still
            // survives the fresh state projection.
            return policyDisabled ? leaseID : nil
        }
        return Array(Set(ids)).sorted()
    }

    private var isMutating: Bool {
        guard let endpoint else { return false }
        return store.mutatingEndpointIDs.contains(endpoint.id)
    }

    private var canApplyForGPU: Bool {
        (availableGPUCount > 0 || schedulerApplyAvailable) && store.allowsMutations && !isMutating
    }

    private var schedulerApplyAvailable: Bool {
        guard let endpoint, endpoint.monitorStatus == "ONLINE" else { return false }
        if let free = endpoint.schedulerCapacity?.freeGPUCount { return free > 0 }
        if let block = store.snapshot.serverGroup(for: endpoint)?.largestAllocatableBlock { return block > 0 }
        return false
    }

    private var showsApplyAction: Bool {
        if !gpus.isEmpty { return true }
        guard let endpoint else { return false }
        if endpoint.schedulerCapacity != nil { return true }
        return store.snapshot.serverGroup(for: endpoint)?.allocation == .delegated
    }

    private var occupancyActionStarts: Bool {
        guard let endpoint else { return true }
        // A disabled policy can still have leases left by a partial/uncertain
        // stop. Keep the action as “结束占卡” so a human can retry the
        // authoritative stop instead of being forced through a new start.
        return !endpoint.keepalive.isEnabled && !endpoint.keepalive.hasResidualLease
    }

    private var occupancyActionTitle: String {
        occupancyActionStarts ? "开始占卡" : "结束占卡"
    }

    private var occupancyActionIcon: String {
        occupancyActionStarts ? "shield.fill" : "stop.circle.fill"
    }

    var body: some View {
        Group {
            if let endpoint {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        header(endpoint)
                        serverActions(endpoint)

                        if endpoint.monitorStatus != "ONLINE" {
                            DetailCallout(
                                icon: endpointStateIcon(endpoint.monitorStatus),
                                color: endpointMonitorStatusColor(endpoint.monitorStatus),
                                message: endpoint.monitorDetail ?? endpoint.monitorLabel
                            )
                        }

                        // Only the missing case is a callout.  A configured
                        // path is a fact, and it now sits with the other host
                        // facts instead of taking a full-width banner.
                        if endpoint.workspacePath == nil {
                            DetailCallout(
                                icon: "folder",
                                color: DesignTokens.warning,
                                message: "远端工作区未设置；申请资源后仍需先补齐路径。"
                            )
                        }

                        if !reclaimableKeepaliveLeaseIDs.isEmpty {
                            ForEach(reclaimableKeepaliveLeaseIDs, id: \.self) { leaseID in
                                let gpuCount = gpus.filter { $0.keepalive.leaseID == leaseID }.count
                                DetailCallout(
                                    icon: "shield.lefthalf.filled.badge.checkmark",
                                    color: DesignTokens.warning,
                                    message: "占卡已停止，但 \(gpuCount) 张 GPU 仍有遗留占卡租约；确认没有进程后可释放。",
                                    actionTitle: isMutating ? "处理中" : "释放遗留占卡",
                                    action: {
                                        guard !isMutating, confirmEmptyKeepaliveCleanup(gpuCount: gpuCount) else { return }
                                        store.clearEmptyConflictedLease(
                                            endpointID: endpoint.id,
                                            leaseID: leaseID
                                        ) { _, _ in }
                                    }
                                )
                            }
                        }

                        let conflictedGPUCount = gpus.filter { $0.state == "CONFLICT" }.count
                        let legacyWorkloadReviewGPUCount = gpus.filter(gpuHasLegacyWorkloadProcessReview).count
                        if !reclaimableLeases.isEmpty {
                            ForEach(reclaimableLeases) { lease in
                                let conflict = conflictedLeases.contains(where: { $0.id == lease.id })
                                let legacyWorkloadReview = lease.gpuIDs.contains { gpuID in
                                    gpus.first(where: { $0.id == gpuID }).map(gpuHasLegacyWorkloadProcessReview) ?? false
                                }
                                let gpuCount = lease.gpuIDs.filter { gpuID in
                                    guard let state = gpus.first(where: { $0.id == gpuID })?.state else { return false }
                                    return conflict ? state == "CONFLICT" : ["HELD", "LEASED_IDLE"].contains(state)
                                }.count
                                let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
                                DetailCallout(
                                    icon: conflict
                                        ? (legacyWorkloadReview ? "exclamationmark.shield.fill" : "exclamationmark.triangle.fill")
                                        : "clock.badge.exclamationmark",
                                    color: conflict
                                        ? (legacyWorkloadReview ? DesignTokens.warning : DesignTokens.danger)
                                        : DesignTokens.interaction,
                                    message: conflict
                                        ? (legacyWorkloadReview
                                            ? "有 \(gpuCount) 张 GPU 仍指派给 \(lease.projectID) · \(task)。当前观测到计算进程变更；worker 重启或替换不会被当作硬件故障，也不会自动释放或停止任务。它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。如需更正实际任务-GPU 指派，请在“使用情况”中选择任务后调整 GPU 分配。"
                                            : "有 \(gpuCount) 张 GPU 的归属状态需要处理；它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。请在“使用情况”中核对任务-GPU 指派，并根据实际任务决定改派或在任务结束后释放。")
                                        : "有 \(gpuCount) 张 GPU 仍被租约占用，但当前采集没有观察到进程；可确认后释放。",
                                    actionTitle: isMutating
                                        ? "处理中"
                                        : (conflict ? "任务结束后清理记录" : "释放空闲占用"),
                                    action: {
                                        guard confirmEmptyLeaseCleanup(lease, conflict: conflict) else { return }
                                        store.clearEmptyConflictedLease(
                                            endpointID: endpoint.id,
                                            leaseID: lease.id
                                        ) { _, _ in }
                                    }
                                )
                            }
                        } else if conflictedGPUCount > 0 {
                            DetailCallout(
                                icon: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? "exclamationmark.shield.fill"
                                    : "exclamationmark.triangle.fill",
                                color: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? DesignTokens.warning
                                    : DesignTokens.danger,
                                message: legacyWorkloadReviewGPUCount == conflictedGPUCount
                                    ? "有 \(conflictedGPUCount) 张 GPU 仍有任务归属待核对。任务运行中的 worker 可正常更换；请根据当前任务与显存、利用率观测判断。它们暂不能申请，其余 \(availableGPUCount) 张仍可申请；如需更正任务-GPU 指派，请在“使用情况”中调整分配。"
                                    : "有 \(conflictedGPUCount) 张 GPU 的状态需要人工处理；它们暂不能申请，其余 \(availableGPUCount) 张仍可申请。请在“使用情况”中核对任务-GPU 指派。"
                            )
                        }

                        if let error = store.errorMessage {
                            InlineValidation(message: error)
                        }

                        if !hostFacts(endpoint).isEmpty {
                            hostFactsCard(endpoint)
                        }

                        if let group = store.snapshot.serverGroup(for: endpoint) {
                            groupMetadataCard(group, endpoint: endpoint)
                        } else if shouldShowUngroupedMetadata(endpoint) {
                            ungroupedMetadataCard(endpoint: endpoint)
                        }

                        if !gpus.isEmpty {
                            ServerGPUMemoryStatusGrid(gpus: gpus)
                        }

                        EndpointTelemetryHistoryPanel(store: store, endpoint: endpoint)

                        HStack {
                            Label(
                                endpointFooterMessage(endpoint),
                                systemImage: endpoint.monitorStatus == "ONLINE" ? "arrow.clockwise" : "hand.raised.fill"
                            )
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(DesignTokens.mutedInk)
                            Spacer()
                            Button("关闭") { dismiss() }
                                .keyboardShortcut(.cancelAction)
                        }
                    }
                    .padding(20)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            } else {
                ContentUnavailableView("服务器已不在当前快照中", systemImage: "server.rack")
                    .task { dismiss() }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(minWidth: 720, idealWidth: 1_040, maxWidth: 1_260, minHeight: 620, idealHeight: 800, maxHeight: 920)
        // The sheet was the one translucent surface in an app of opaque planes.
        // It now stands on the same page plane the grid does.
        .background(DesignTokens.ambientSmoke)
        // Labelling this container turned the whole sheet into one leaf, hiding
        // every button and value inside it.  The header states the same thing
        // in visible text.
        .accessibilityElement(children: .contain)
        .onChange(of: store.snapshot.endpoints.map(\.id)) { _, endpointIDs in
            if !endpointIDs.contains(endpointID) {
                dismiss()
            }
        }
    }

    private var accessibilityValue: String {
        guard let endpoint else { return "服务器已不在当前快照中" }
        let occupancy = endpoint.keepalive.configured && !gpus.isEmpty
            ? "，占卡\(endpoint.keepalive.label)"
            : ""
        let groupName = store.snapshot.serverGroup(for: endpoint)?.displayName ?? "未分组"
        return "\(endpoint.displayName)，服务器组 \(groupName)，\(endpoint.monitorLabel)，\(gpus.count) 块 GPU\(occupancy)"
    }

    /// Facts the table row deliberately leaves out: core count, total RAM, and
    /// peak temperature.  CPU load, memory pressure, and GPU model already
    /// live on the row.  Workspace lives on the group card, or on the
    /// ungrouped metadata card when there is no group.
    private func hostFactsCard(_ endpoint: EndpointRecord) -> some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                CardSectionLabel(text: "主机")
                metadataFactGrid(hostFacts(endpoint))
            }
        }
    }

    private func hostFacts(_ endpoint: EndpointRecord) -> [(String, String)] {
        var facts: [(String, String)] = []
        if let cores = endpoint.cpuCores {
            facts.append(("CPU 核数", scopedFact(ResourceText.cores(cores), note: endpoint.cpuScopeNote)))
        }
        if let total = endpoint.memoryTotalMiB, total > 0 {
            facts.append(("内存总量", scopedFact(ResourceText.memory(total), note: endpoint.memoryScopeNote)))
        }
        if let peak = gpus.compactMap(\.temperature).max() {
            facts.append(("最高温度", "\(peak) °C"))
        }
        return facts
    }

    private func groupMetadataCard(_ group: ServerGroupRecord, endpoint: EndpointRecord) -> some View {
        let shortFacts = groupShortFacts(group: group, endpoint: endpoint)
        let longFacts = groupLongFacts(group: group, endpoint: endpoint)
        return HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                CardSectionLabel(
                    text: "服务器组",
                    accessory: allocationBadge(group: group, endpoint: endpoint)
                )
                if !shortFacts.isEmpty {
                    metadataFactGrid(shortFacts)
                }
                ForEach(longFacts, id: \.0) { fact in
                    groupFact(label: fact.0, value: fact.1, allowWrap: true)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("server-group-metadata")
    }

    private func shouldShowUngroupedMetadata(_ endpoint: EndpointRecord) -> Bool {
        !applyConstraintFacts(group: nil, endpoint: endpoint).isEmpty
            || ungroupedWorkspacePath(endpoint) != nil
            || endpoint.schedulerCapacity?.note != nil
            || allocationBadge(group: nil, endpoint: endpoint) != nil
    }

    private func ungroupedWorkspacePath(_ endpoint: EndpointRecord) -> String? {
        guard let path = endpoint.workspacePath?.trimmingCharacters(in: .whitespacesAndNewlines),
              !path.isEmpty else { return nil }
        return path
    }

    private func ungroupedMetadataCard(endpoint: EndpointRecord) -> some View {
        let constraints = applyConstraintFacts(group: nil, endpoint: endpoint)
        let longFacts = ungroupedLongFacts(endpoint: endpoint)
        let badge = allocationBadge(group: nil, endpoint: endpoint)
        let title = constraints.isEmpty && badge == nil ? "工作区" : "申请约束"
        return HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 12) {
                CardSectionLabel(text: title, accessory: badge)
                if !constraints.isEmpty {
                    metadataFactGrid(constraints)
                }
                ForEach(longFacts, id: \.0) { fact in
                    groupFact(label: fact.0, value: fact.1, allowWrap: true)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("ungrouped-endpoint-metadata")
    }

    private func allocationBadge(group: ServerGroupRecord?, endpoint: EndpointRecord) -> String? {
        if let allocation = group?.allocation {
            return allocation == .direct ? "本机直接分配" : "调度器按需申请"
        }
        if endpoint.schedulerCapacity != nil {
            return "调度器按需申请"
        }
        return nil
    }

    private func groupShortFacts(group: ServerGroupRecord, endpoint: EndpointRecord) -> [(String, String)] {
        var facts: [(String, String)] = [("显示名称", group.displayName)]
        facts.append(contentsOf: applyConstraintFacts(group: group, endpoint: endpoint))
        return facts
    }

    private func groupLongFacts(group: ServerGroupRecord, endpoint: EndpointRecord) -> [(String, String)] {
        var facts: [(String, String)] = []
        facts.append(("组默认工作区", group.workspacePath))
        let override = endpoint.workspacePathOverride?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        facts.append(("本机路径覆盖", override.isEmpty ? "未设置，使用组默认路径" : override))
        if !group.environmentNotes.isEmpty {
            facts.append(("环境说明", group.environmentNotes))
        }
        if !group.description.isEmpty {
            facts.append(("说明", group.description))
        }
        if let note = endpoint.schedulerCapacity?.note {
            facts.append(("申请说明", note))
        }
        return facts
    }

    private func ungroupedLongFacts(endpoint: EndpointRecord) -> [(String, String)] {
        var facts: [(String, String)] = []
        if let workspace = ungroupedWorkspacePath(endpoint) {
            facts.append(("远端工作区", workspace))
        }
        if let note = endpoint.schedulerCapacity?.note {
            facts.append(("申请说明", note))
        }
        return facts
    }

    /// Short apply-time constraints only.  Allocation is a title chip;
    /// workspace, notes, and apply copy take a full row outside the grid.
    private func applyConstraintFacts(group: ServerGroupRecord?, endpoint: EndpointRecord) -> [(String, String)] {
        var facts: [(String, String)] = []
        let leaseEnds = group?.limits?.leaseEnds
        let maxLeaseSeconds = group?.limits?.maxLeaseSeconds
        if let leaseEnds {
            switch leaseEnds {
            case .onRelease:
                facts.append(("租约结束", "直到显式释放"))
                if let maxLeaseSeconds {
                    facts.append(("租约时限", durationLabel(maxLeaseSeconds)))
                }
            case .hardKillAtTimeLimit:
                if let maxLeaseSeconds {
                    facts.append(("租约时限", "\(durationLabel(maxLeaseSeconds))，到期硬杀"))
                } else {
                    facts.append(("租约时限", "到期硬杀"))
                }
            }
        } else if let maxLeaseSeconds {
            facts.append(("租约时限", durationLabel(maxLeaseSeconds)))
        }
        // `largest_allocatable_block` is live capacity on one machine right
        // now; `max_gpus_per_lease` is the structural cap.  Never invent the
        // former from the latter when the block is unknown.
        let currentBlock = group?.largestAllocatableBlock
        let leaseMax = group?.limits?.maxGPUsPerLease
            ?? endpoint.schedulerCapacity?.maxGPUsPerLease
        if let currentBlock {
            facts.append(("现在可申请", "\(currentBlock) 卡"))
        }
        if let leaseMax, leaseMax != currentBlock {
            facts.append(("单次上限", "\(leaseMax) 卡"))
        }
        let cpuPerGPU = group?.limits?.cpuCoresPerGPU ?? endpoint.schedulerCapacity?.cpuCoresPerGPU
        let memoryPerGPU = group?.limits?.memoryMiBPerGPU ?? endpoint.schedulerCapacity?.memoryMiBPerGPU
        if let cpuPerGPU {
            facts.append(("每卡 CPU", "\(cpuPerGPU) 核"))
        }
        if let memoryPerGPU {
            facts.append(("每卡内存", memoryMiBLabel(memoryPerGPU)))
        }
        if let applyMax = group?.limits?.applyMaxSeconds {
            facts.append(("申请等待上限", durationLabel(applyMax)))
        }
        // Direct groups never queue; the field is a constant, not a constraint.
        if group?.allocation == .delegated, let queues = group?.limits?.queues {
            facts.append(("排队", queues ? "会排队" : "不排队"))
        }
        return facts
    }

    private func metadataFactGrid(_ facts: [(String, String)]) -> some View {
        LazyVGrid(
            columns: [GridItem(.adaptive(minimum: 170, maximum: 280), spacing: 12)],
            alignment: .leading,
            spacing: 12
        ) {
            ForEach(facts, id: \.0) { fact in
                groupFact(label: fact.0, value: fact.1)
            }
        }
    }

    private func groupFact(label: String, value: String, allowWrap: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
            Text(value)
                .font(Typography.rowValue)
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: allowWrap)
                .lineLimit(allowWrap ? nil : 1)
                .truncationMode(.middle)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue(value)
    }

    private func header(_ endpoint: EndpointRecord) -> some View {
        SheetTitle(
            icon: endpoint.monitorStatus == "ONLINE" ? "server.rack" : endpointStateIcon(endpoint.monitorStatus),
            title: "服务器详情",
            subtitle: endpoint.sshCommand
        )
        // The overview moved here from the sheet's root, where it collapsed
        // everything below it into one element.
        .accessibilityElement(children: .combine)
        .accessibilityValue(accessibilityValue)
    }

    @ViewBuilder
    private func serverActions(_ endpoint: EndpointRecord) -> some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 9) {
                primaryServerActions(endpoint)
                Spacer(minLength: 0)
                serverOperations()
            }
            VStack(alignment: .leading, spacing: 10) {
                primaryServerActions(endpoint)
                serverOperations()
            }
        }
    }

    @ViewBuilder
    private func primaryServerActions(_ endpoint: EndpointRecord) -> some View {
        HStack(spacing: 9) {
            if showsApplyAction {
                Button {
                    dismiss()
                    DispatchQueue.main.async { claim() }
                } label: {
                    Label("申请 GPU", systemImage: "key.fill")
                }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.interaction, foreground: DesignTokens.onInteraction))
                .accessibilityIdentifier("server-detail-claim")
                .disabled(!canApplyForGPU)
                .help(claimActionHelp)
                .accessibilityHint(claimActionHelp)
            }

            if store.supportsEndpointKeepalive {
                Button {
                    if occupancyActionStarts || confirmKeepaliveEnd() {
                        store.setEndpointKeepalive(endpoint, enabled: occupancyActionStarts) { _, _ in }
                    }
                } label: {
                    Label(isMutating ? "处理中" : occupancyActionTitle, systemImage: occupancyActionIcon)
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .accessibilityIdentifier("endpoint-keepalive-action")
                .disabled(
                    gpus.isEmpty
                        || !store.allowsEndpointLifecycleMutations
                        || isMutating
                )
                .help(occupancyActionHelp)
            }
        }
    }

    @ViewBuilder
    private func serverOperations() -> some View {
        if store.supportsEndpointUpdate || store.supportsEndpointDelete {
            Menu {
                Button("编辑或移除服务器", systemImage: "slider.horizontal.3", action: edit)
                    .disabled(isMutating)
            } label: {
                Label("服务器操作", systemImage: "ellipsis.circle")
                    .font(.callout.weight(.semibold))
            }
            .menuStyle(.borderlessButton)
            .accessibilityLabel("服务器操作")
            .help("编辑或移除服务器")
        }
    }

    private var unavailableReason: String {
        if !store.allowsMutations { return store.mutationUnavailableReason }
        if isMutating { return "服务器操作正在处理中。" }
        if !showsApplyAction { return "这台服务器没有 GPU。" }
        if availableGPUCount == 0, !schedulerApplyAvailable { return "当前没有可申请的 GPU。" }
        return "当前不可申请。"
    }

    private var claimActionHelp: String {
        guard canApplyForGPU, let endpoint else { return unavailableReason }
        if store.snapshot.serverGroup(for: endpoint) != nil {
            return "在此服务器组内申请 GPU；由控制面选择服务器，不会启动任务"
        }
        return "只申请这台服务器上的 GPU；不会启动任务"
    }

    private var occupancyActionHelp: String {
        if gpus.isEmpty { return "这台服务器没有 GPU。" }
        guard store.allowsEndpointLifecycleMutations else { return store.endpointLifecycleMutationUnavailableReason }
        return occupancyActionStarts
            ? "开始这台服务器上空闲 GPU 的占卡"
            : "结束这台服务器上空闲 GPU 的占卡；不会停止正在运行的任务"
    }
}

private enum GPUStatusSection: String, CaseIterable, Identifiable {
    case available
    case keepalive
    case busy
    case error

    var id: String { rawValue }

    var label: String {
        switch self {
        case .available: return "空闲"
        case .keepalive: return "占卡"
        case .busy: return "繁忙"
        case .error: return "错误"
        }
    }

    var icon: String {
        switch self {
        case .available: return "checkmark.circle.fill"
        case .keepalive: return "shield.fill"
        case .busy: return "bolt.fill"
        case .error: return "exclamationmark.triangle.fill"
        }
    }

    var tint: Color {
        switch self {
        case .available: return DesignTokens.success
        case .keepalive: return DesignTokens.interaction
        case .busy: return DesignTokens.warning
        case .error: return DesignTokens.danger
        }
    }
}

private struct ServerGPUMemoryStatusGrid: View {
    let gpus: [GPURecord]

    private func section(for gpu: GPURecord) -> GPUStatusSection {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return .busy }
        if gpuNeedsAttention(gpu) { return .error }
        if gpu.keepalive.isActive { return .keepalive }
        if gpu.isPubliclyAvailable { return .available }
        return .busy
    }

    private var orderedGPUs: [GPURecord] {
        gpus.sorted { lhs, rhs in
            let lhsRank = GPUStatusSection.allCases.firstIndex(of: section(for: lhs)) ?? 0
            let rhsRank = GPUStatusSection.allCases.firstIndex(of: section(for: rhs)) ?? 0
            if lhsRank == rhsRank { return lhs.index < rhs.index }
            return lhsRank < rhsRank
        }
    }

    private func count(in section: GPUStatusSection) -> Int {
        gpus.filter { self.section(for: $0) == section }.count
    }

    private var accessibilityValue: String {
        let gpuStates = gpus
            .sorted { $0.index < $1.index }
            .map { gpu in
                let observation = gpuTaskObservationLabel(gpu).map { " · \($0)" } ?? ""
                return "GPU \(gpu.index) \(section(for: gpu).label)\(observation) 显存 \(gpuMemoryPercent(gpu))"
            }
            .joined(separator: "，")
        let counts = GPUStatusSection.allCases
            .map { "\($0.label) \(count(in: $0)) 张" }
            .joined(separator: "，")
        return "\(gpuStates)；\(counts)"
    }

    var body: some View {
        HomeCard(padding: 16) { gridContent }
    }

    private var gridContent: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Label("GPU 状态", systemImage: "memorychip")
                    .font(Typography.sectionTitle)
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 0)
                Text("当前显存 · \(gpus.count) 张 GPU")
                    .font(Typography.metricLabel)
                    .foregroundStyle(DesignTokens.mutedInk)
            }

            HStack(spacing: 7) {
                ForEach(GPUStatusSection.allCases) { section in
                    GPUStatusCountChip(section: section, count: count(in: section))
                }
                Spacer(minLength: 0)
            }

            // A GPU card carries a 40pt ring, an index, a non-idle status
            // pill, an absolute memory figure, a task line, and a percentage.
            // Idle is already counted on the chips above.  At the old 132pt
            // minimum every one of those was clipped mid-word.
            LazyVGrid(
                columns: [GridItem(.adaptive(minimum: 258, maximum: 340), spacing: 10)],
                alignment: .leading,
                spacing: 10
            ) {
                ForEach(orderedGPUs) { gpu in
                    GPUMemoryStatusRow(gpu: gpu, status: section(for: gpu))
                }
            }
        }
        // Each card already carries its own label and value; collapsing the
        // grid hid all of them behind one summary string.
        .accessibilityElement(children: .contain)
        .help("每张卡片代表一张 GPU；环图表示当前显存占用，状态标签表示可用状态。\n\(accessibilityValue)")
    }
}

private struct GPUStatusCountChip: View {
    let section: GPUStatusSection
    let count: Int

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(section.tint)
                .frame(width: 6, height: 6)
            Text(section.label)
            Text("\(count)")
                .fontWeight(.semibold)
                .foregroundStyle(count == 0 ? DesignTokens.mutedInk : DesignTokens.ink)
        }
        .font(Typography.annotation)
        .foregroundStyle(DesignTokens.mutedInk)
        .padding(.horizontal, 8)
        .frame(height: 24)
        .background(DesignTokens.ink.opacity(count == 0 ? 0.025 : 0.045), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(section.label)
        .accessibilityValue("\(count) 张 GPU")
    }
}

private struct GPUMemoryStatusRow: View {
    let gpu: GPURecord
    let status: GPUStatusSection

    var body: some View {
        HStack(spacing: 11) {
            GPUMemoryGlyph(gpu: gpu, diameter: 40)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 7) {
                    Text("GPU \(gpu.index)")
                        .font(Typography.rowValue.weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                    if status != .available {
                        Label(status.label, systemImage: status.icon)
                            .font(Typography.metricLabel.weight(.semibold))
                            .foregroundStyle(status.tint)
                            .lineLimit(1)
                            .fixedSize()
                            .padding(.horizontal, 7)
                            .frame(height: 20)
                            .background(status.tint.opacity(DesignTokens.Alpha.fill), in: Capsule())
                    }
                }
                Text(gpu.memoryLabel)
                    .font(Typography.command)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                    .truncationMode(.tail)
                if let observation = gpuTaskObservationLabel(gpu) {
                    Text(observation)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(status == .error ? DesignTokens.danger : DesignTokens.mutedInk)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }

            Spacer(minLength: 8)

            VStack(alignment: .trailing, spacing: 2) {
                Text(gpuMemoryPercent(gpu))
                    .font(Typography.cardValue)
                    .foregroundStyle(memoryPressureColor(gpu))
                    .lineLimit(1)
                Text("显存")
                    .font(Typography.metricLabel)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
            .fixedSize()
        }
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, minHeight: 74, alignment: .leading)
        .background(
            DesignTokens.ambientSmoke,
            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("GPU \(gpu.index)")
        .accessibilityValue("\(status.label)，\(gpuTaskObservationLabel(gpu) ?? "无任务指派")，当前显存 \(gpu.memoryLabel)，占用 \(gpuMemoryPercent(gpu))")
        .help("GPU \(gpu.index) · \(status.label) · \(gpuTaskObservationLabel(gpu) ?? "无任务指派") · 当前显存 \(gpu.memoryLabel)")
    }

    private func memoryPressureColor(_ gpu: GPURecord) -> Color {
        guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return DesignTokens.mutedInk }
        return pressureColor(gpu.memoryFraction)
    }
}

private struct GPUMemoryGlyph: View {
    let gpu: GPURecord
    let diameter: CGFloat

    private var memoryFraction: Double? {
        guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return nil }
        return min(max(gpu.memoryFraction, 0), 1)
    }

    private var memoryColor: Color {
        pressureColor(memoryFraction)
    }

    var body: some View {
        ZStack {
            Circle()
                .stroke(DesignTokens.ink.opacity(DesignTokens.Alpha.edge), lineWidth: 4)

            if let memoryFraction {
                Circle()
                    .trim(from: 0, to: memoryFraction)
                    .stroke(memoryColor, style: StrokeStyle(lineWidth: 4, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                Circle()
                    .fill(memoryColor.opacity(DesignTokens.Alpha.fill))
                    .frame(width: diameter * 0.56, height: diameter * 0.56)
            } else {
                Circle()
                    .stroke(DesignTokens.mutedInk.opacity(DesignTokens.Alpha.muted), style: StrokeStyle(lineWidth: 2, dash: [3, 3]))
            }

            Text("\(gpu.index)")
                .font(Typography.identity.weight(.semibold))
                .foregroundStyle(DesignTokens.ink)
        }
        .frame(width: diameter, height: diameter)
        .accessibilityHidden(true)
    }
}

private func gpuMemoryPercent(_ gpu: GPURecord) -> String {
    guard gpu.memoryUsedMiB != nil, gpu.totalVRAMMiB > 0 else { return "—" }
    return "\(Int((min(max(gpu.memoryFraction, 0), 1) * 100).rounded()))%"
}

private struct GPUDetailSheet: View {
    @Environment(\.dismiss) private var dismiss
    let gpu: GPURecord

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            SheetTitle(
                icon: stateIcon,
                title: "GPU \(gpu.index) · \(stateLabel)",
                subtitle: gpu.name
            )
            HStack(spacing: 12) {
                GPUDetailMetric(label: "显存", value: gpu.vramLabel, accent: stateColor)
                GPUDetailMetric(label: "已用显存", value: gpu.memoryLabel, accent: DesignTokens.interaction)
                GPUDetailMetric(label: "计算利用率", value: utilizationLabel, accent: DesignTokens.warning)
                GPUDetailMetric(label: "温度", value: temperatureLabel, accent: DesignTokens.danger)
            }
            if gpuHasLegacyWorkloadProcessReview(gpu) {
                DetailCallout(
                    icon: "info.circle.fill",
                    color: DesignTokens.warning,
                    message: "任务仍保持 GPU 指派，采集到的计算进程与此前观测不同。worker 重启或替换会出现这一提示，并不表示 GPU 硬件故障；仅在实际任务-GPU 指派需要更正时，才到“使用情况”调整分配。"
                )
            } else if let reason = gpu.stateReason {
                DetailCallout(icon: "info.circle.fill", color: stateColor, message: localizedStateReason(reason))
            }
            if let task = gpu.taskReference?.trimmingCharacters(in: .whitespacesAndNewlines), !task.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("当前任务")
                        .fieldLabel()
                    Text(task)
                        .font(.system(.body, design: .monospaced).weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(2)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(DesignTokens.selection.opacity(0.64), in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
            }
            HStack {
                Spacer()
                Button("关闭") { dismiss() }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(28)
        .frame(width: 560)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private var utilizationLabel: String {
        guard let value = gpu.utilization else { return "—" }
        return "\(value)%"
    }

    private var temperatureLabel: String {
        guard let value = gpu.temperature else { return "—" }
        return "\(value)°C"
    }

    private var stateLabel: String { gpuPresentationLabel(gpu) }

    private var stateIcon: String {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return "bolt.fill" }
        switch gpu.state {
        case "AVAILABLE": return "checkmark.circle.fill"
        case "HELD", "LEASED_IDLE": return "key.fill"
        case "KEEPALIVE": return "shield.fill"
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return "bolt.fill"
        default: return "exclamationmark.triangle.fill"
        }
    }

    private var stateColor: Color {
        if gpuHasLegacyWorkloadProcessReview(gpu) { return DesignTokens.warning }
        switch gpu.state {
        case "AVAILABLE": return DesignTokens.success
        case "HELD", "LEASED_IDLE", "KEEPALIVE": return DesignTokens.interaction
        case "RUNNING_MANAGED", "BUSY_UNMANAGED", "ORPHANED_BUSY", "RESERVED": return DesignTokens.warning
        default: return DesignTokens.danger
        }
    }
}

private struct GPUDetailMetric: View {
    let label: String
    let value: String
    let accent: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Circle()
                .fill(accent)
                .frame(width: 7, height: 7)
            Text(value)
                .font(Typography.label)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
            Text(label)
                .font(.caption2.weight(.medium))
                .foregroundStyle(DesignTokens.mutedInk)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(DesignTokens.surface.opacity(0.76), in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
    }
}

private struct DetailCallout: View {
    let icon: String
    let color: Color
    let message: String
    let actionTitle: String?
    let action: (() -> Void)?

    init(
        icon: String,
        color: Color,
        message: String,
        actionTitle: String? = nil,
        action: (() -> Void)? = nil
    ) {
        self.icon = icon
        self.color = color
        self.message = message
        self.actionTitle = actionTitle
        self.action = action
    }

    var body: some View {
        HStack(spacing: 10) {
            Label(message, systemImage: icon)
                .font(.callout.weight(.medium))
                .foregroundStyle(DesignTokens.ink)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .buttonStyle(.borderless)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .fixedSize()
            }
        }
        .padding(14)
        .background(
            color.opacity(DesignTokens.Alpha.fill),
            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
        )
    }
}

private struct AddServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var sshCommand = ""
    @State private var serverGroupID = ""
    @State private var inheritGroupPath = true
    @State private var workspacePath = ""
    @State private var observationProfile = "server-script-v1"
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                SheetTitle(icon: "server.rack", title: "添加服务器", subtitle: "")
                VStack(alignment: .leading, spacing: 8) {
                    Text("SSH 指令")
                        .fieldLabel()
                    TextField("ssh -p 22 gpu@node-a.example", text: $sshCommand)
                        .textFieldStyle(.roundedBorder)
                        .font(.system(.body, design: .monospaced).weight(.medium))
                        .accessibilityLabel("SSH 指令")
                }
                ServerGroupAssignmentFields(
                    serverGroupID: $serverGroupID,
                    inheritGroupPath: $inheritGroupPath,
                    workspacePath: $workspacePath,
                    groups: store.snapshot.serverGroups
                )
                EndpointObservationProfileField(selection: $observationProfile, profiles: store.observationProfiles)
                if let validationMessage {
                    InlineValidation(message: validationMessage)
                }
                HStack {
                    Spacer()
                    Button("取消") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button("添加服务器") { submit() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                        .disabled(!store.allowsMutations || isSubmitting)
                        .help(store.allowsMutations ? "添加服务器" : store.mutationUnavailableReason)
                }
            }
            .padding(28)
        }
        .frame(width: 520, height: 580)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private func submit() {
        do {
            let parsed = try parseSSHCommand(sshCommand)
            let assignment = try ServerGroupPathAssignment(
                groups: store.snapshot.serverGroups,
                serverGroupID: serverGroupID,
                inheritGroupPath: inheritGroupPath,
                workspacePath: workspacePath
            )
            let draft = try EndpointDraft(
                host: parsed.host,
                port: parsed.port,
                sshUser: parsed.user,
                workspacePath: assignment.effectiveWorkspacePath,
                observationProfile: observationProfile,
                suppliedID: "",
                serverGroupID: assignment.serverGroupID,
                workspacePathOverride: assignment.workspacePathOverride
            )
            validationMessage = nil
            isSubmitting = true
            store.addEndpoint(draft) { success, error in
                isSubmitting = false
                if success {
                    dismiss()
                } else {
                    validationMessage = error
                }
            }
        } catch {
            validationMessage = error.localizedDescription
        }
    }
}

private struct ParsedSSHCommand {
    let user: String
    let host: String
    let port: Int
}

private func parseSSHCommand(_ command: String) throws -> ParsedSSHCommand {
    let parts = command.split(whereSeparator: \Character.isWhitespace).map(String.init)
    guard parts.first == "ssh" else { throw EndpointDraftError.invalidEndpointFields }
    var port = 22
    var destination: String?
    var index = 1
    while index < parts.count {
        if parts[index] == "-p" {
            guard index + 1 < parts.count, let parsedPort = Int(parts[index + 1]), (1...65535).contains(parsedPort) else {
                throw EndpointDraftError.invalidEndpointFields
            }
            port = parsedPort
            index += 2
        } else if parts[index].hasPrefix("-") {
            throw EndpointDraftError.invalidEndpointFields
        } else if destination == nil {
            destination = parts[index]
            index += 1
        } else {
            throw EndpointDraftError.invalidEndpointFields
        }
    }
    guard let destination else { throw EndpointDraftError.invalidEndpointFields }
    let identity = destination.split(separator: "@", omittingEmptySubsequences: false)
    guard identity.count == 2, !identity[0].isEmpty, !identity[1].isEmpty else {
        throw EndpointDraftError.invalidEndpointFields
    }
    return ParsedSSHCommand(user: String(identity[0]), host: String(identity[1]), port: port)
}

private struct EditServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let endpoint: EndpointRecord
    let onRemoved: () -> Void
    @State private var sshUser: String
    @State private var serverGroupID: String
    @State private var inheritGroupPath: Bool
    @State private var workspacePath: String
    @State private var observationProfile: String
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    init(store: BrokerStore, endpoint: EndpointRecord, onRemoved: @escaping () -> Void = {}) {
        self.store = store
        self.endpoint = endpoint
        self.onRemoved = onRemoved
        _sshUser = State(initialValue: endpoint.sshUser)
        let groupID = endpoint.serverGroupID ?? ""
        let override = endpoint.workspacePathOverride?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        _serverGroupID = State(initialValue: groupID)
        _inheritGroupPath = State(initialValue: endpoint.inheritsGroupWorkspacePath)
        _workspacePath = State(initialValue: override.isEmpty ? (endpoint.workspacePath ?? "") : override)
        _observationProfile = State(initialValue: endpoint.observationProfile)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                SheetTitle(icon: "slider.horizontal.3", title: "编辑服务器", subtitle: "端点地址和端口是身份边界，不能在此修改。")
                VStack(alignment: .leading, spacing: 7) {
                    Text("端点")
                        .fieldLabel()
                    Text(endpoint.sshCommand)
                        .font(.system(.callout, design: .monospaced).weight(.semibold))
                        .textSelection(.enabled)
                }
                LabeledField(label: "SSH 用户", placeholder: "collector", text: $sshUser)
                ServerGroupAssignmentFields(
                    serverGroupID: $serverGroupID,
                    inheritGroupPath: $inheritGroupPath,
                    workspacePath: $workspacePath,
                    groups: store.snapshot.serverGroups
                )
                EndpointObservationProfileField(selection: $observationProfile, profiles: store.observationProfiles)
                if let validationMessage {
                    InlineValidation(message: validationMessage)
                }
                HStack {
                    Spacer()
                    Button("取消") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button("保存设置") { submit() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                        .disabled(!store.allowsEndpointLifecycleMutations || !store.supportsEndpointUpdate || isSubmitting)
                        .help(store.allowsEndpointLifecycleMutations ? "保存采集设置" : store.endpointLifecycleMutationUnavailableReason)
                }
                if store.supportsEndpointDelete {
                    VStack(alignment: .leading, spacing: 10) {
                        Divider()
                        Text("危险操作")
                            .font(.callout.weight(.semibold))
                            .foregroundStyle(DesignTokens.danger)
                        Text("从本机控制面移除这台服务器。会停止监控与协调并删除本机关联记录，不会停止远端进程。")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(DesignTokens.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("从 ServerPilot 移除…") { deleteServer() }
                            .buttonStyle(SoftButtonStyle(tint: DesignTokens.danger, foreground: DesignTokens.onInteraction))
                            .disabled(!store.allowsEndpointLifecycleMutations || isSubmitting)
                            .help(deleteHelp)
                            .accessibilityIdentifier("endpoint-delete-action")
                    }
                }
            }
            .padding(28)
        }
        .frame(width: 520, height: 640)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private var deleteHelp: String {
        guard store.allowsEndpointLifecycleMutations else {
            return store.endpointLifecycleMutationUnavailableReason
        }
        return "从本机控制面移除这台服务器；不会停止远端进程"
    }

    private func deleteServer() {
        guard confirmEndpointDelete(endpoint) else { return }
        validationMessage = nil
        isSubmitting = true
        store.deleteEndpoint(endpoint) { success, error in
            isSubmitting = false
            if success {
                onRemoved()
                dismiss()
            } else {
                validationMessage = error
            }
        }
    }

    private func submit() {
        do {
            let assignment = try ServerGroupPathAssignment(
                groups: store.snapshot.serverGroups,
                serverGroupID: serverGroupID,
                inheritGroupPath: inheritGroupPath,
                workspacePath: workspacePath
            )
            let draft = try EndpointUpdateDraft(
                sshUser: sshUser,
                workspacePath: assignment.effectiveWorkspacePath,
                observationProfile: observationProfile,
                serverGroupID: assignment.serverGroupID,
                workspacePathOverride: assignment.workspacePathOverride
            )
            validationMessage = nil
            isSubmitting = true
            store.updateEndpoint(endpoint, draft: draft) { success, error in
                isSubmitting = false
                if success {
                    dismiss()
                } else {
                    validationMessage = error
                }
            }
        } catch {
            validationMessage = error.localizedDescription
        }
    }
}

private struct EndpointObservationProfileField: View {
    @Binding var selection: String
    let profiles: [ObservationProfileRecord]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("采集方式")
                .fieldLabel()
            Picker("采集方式", selection: $selection) {
                ForEach(profiles) { profile in
                    Text(profile.displayName).tag(profile.id)
                }
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(profiles.first(where: { $0.id == selection })?.description ?? "")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("采集方式")
        .accessibilityValue(profiles.first(where: { $0.id == selection })?.displayName ?? selection)
    }
}

private let ungroupedClaimToken = "__ungrouped__"

private struct ClaimSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let initialEndpointID: String
    @State private var projectID = ""
    @State private var taskReference = ""
    @State private var gpuCountText = "1"
    @State private var serverGroupID: String
    @State private var endpointID: String
    @State private var validationMessage: String?
    @State private var submissionResult: ClaimSubmissionResult?
    @State private var isSubmitting = false

    init(store: BrokerStore, initialEndpointID: String) {
        self.store = store
        self.initialEndpointID = initialEndpointID
        let endpoint = store.snapshot.endpoint(id: initialEndpointID)
        let group = endpoint.flatMap { store.snapshot.serverGroup(for: $0) }
        if let group {
            _serverGroupID = State(initialValue: group.id)
            _endpointID = State(initialValue: "")
        } else if endpoint != nil {
            _serverGroupID = State(initialValue: store.snapshot.serverGroups.isEmpty ? "" : ungroupedClaimToken)
            _endpointID = State(initialValue: initialEndpointID)
        } else {
            _serverGroupID = State(initialValue: "")
            _endpointID = State(initialValue: "")
        }
    }

    private var usesGroupedClaim: Bool {
        !store.snapshot.serverGroups.isEmpty
    }

    private var ungroupedClaimEndpoints: [EndpointRecord] {
        store.snapshot.ungroupedEndpoints
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(
                icon: "checkmark.seal.fill",
                title: "申请 GPU",
                subtitle: usesGroupedClaim ? "先选择服务器组，由控制面在组内选择服务器。" : ""
            )
            HStack(spacing: 14) {
                LabeledField(label: "项目", placeholder: "project-a", text: $projectID)
                LabeledField(label: "任务", placeholder: "training-042", text: $taskReference)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("GPU 数量")
                    .fieldLabel()
                TextField("1", text: $gpuCountText)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 110)
            }
            if usesGroupedClaim {
                VStack(alignment: .leading, spacing: 8) {
                    Text("服务器组")
                        .fieldLabel()
                    ClaimGroupPicker(
                        groups: store.snapshot.serverGroups,
                        includeUngrouped: !ungroupedClaimEndpoints.isEmpty,
                        store: store,
                        selection: $serverGroupID
                    )
                    if serverGroupID != ungroupedClaimToken, !serverGroupID.isEmpty {
                        Text("由控制面在该组内选择一台能放下本次申请的服务器，不必指定具体机器。")
                            .font(Typography.annotation)
                            .foregroundStyle(DesignTokens.mutedInk)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                if serverGroupID == ungroupedClaimToken {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("未分组的服务器")
                            .fieldLabel()
                        ClaimEndpointPicker(
                            endpoints: ungroupedClaimEndpoints,
                            selection: $endpointID
                        )
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    Text("服务器")
                        .fieldLabel()
                    ClaimEndpointPicker(
                        endpoints: store.snapshot.operationalEndpoints,
                        selection: $endpointID
                    )
                }
            }
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
            if let submissionResult {
                InlineResult(message: submissionResult.message, allocated: submissionResult.allocated)
            }
            HStack {
                Spacer()
                if submissionResult == nil {
                    Button("取消") { dismiss() }
                        .keyboardShortcut(.cancelAction)
                    Button("申请") { submit() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                        .disabled(!store.allowsMutations || isSubmitting)
                    .help(store.allowsMutations ? "提交 GPU 申请" : store.mutationUnavailableReason)
                } else {
                    Button("完成") { dismiss() }
                        .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(28)
        .frame(width: 640)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
    }

    private func submit() {
        guard let gpuCount = Int(gpuCountText), gpuCount > 0 else {
            validationMessage = "GPU 数量必须是大于 0 的整数。"
            return
        }
        let project = projectID.trimmingCharacters(in: .whitespacesAndNewlines)
        let task = taskReference.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !project.isEmpty, !task.isEmpty else {
            validationMessage = "请填写项目和任务。"
            return
        }
        let selectedGroupID: String?
        let selectedEndpointID: String
        if usesGroupedClaim {
            if serverGroupID.isEmpty {
                validationMessage = "请选择服务器组。"
                return
            }
            if serverGroupID == ungroupedClaimToken {
                selectedGroupID = nil
                selectedEndpointID = endpointID
            } else {
                selectedGroupID = serverGroupID
                selectedEndpointID = ""
            }
        } else {
            selectedGroupID = nil
            selectedEndpointID = endpointID
        }
        validationMessage = nil
        submissionResult = nil
        isSubmitting = true
        store.submitClaim(
            ClaimDraft(
                projectID: project,
                taskReference: task,
                purpose: task,
                gpuCount: gpuCount,
                endpointID: selectedEndpointID,
                serverGroupID: selectedGroupID,
                minimumCPUCores: nil,
                minimumMemoryMiB: nil,
                minimumTotalVRAMMiB: nil,
                minimumFreeVRAMMiB: nil
            )
        ) { result, error in
            isSubmitting = false
            if let error {
                validationMessage = error
                return
            }
            submissionResult = result
        }
    }
}

private struct ClaimGroupPicker: View {
    let groups: [ServerGroupRecord]
    let includeUngrouped: Bool
    @ObservedObject var store: BrokerStore
    @Binding var selection: String

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 7) {
                ForEach(groups) { group in
                    let members = store.snapshot.endpoints(inGroup: group.id)
                    option(
                        id: group.id,
                        title: group.displayName,
                        detail: claimGroupDetail(group, members: members)
                    )
                }
                if includeUngrouped {
                    option(
                        id: ungroupedClaimToken,
                        title: "未分组的服务器",
                        detail: "沿用未加入服务器组的机器；可选自动选择或指定一台"
                    )
                }
            }
            .padding(1)
        }
        .frame(maxHeight: 224)
        .accessibilityLabel("服务器组")
        .accessibilityValue(selectedGroupDescription)
        .accessibilityIdentifier("claim-group-picker")
    }

    private var selectedGroupDescription: String {
        if selection == ungroupedClaimToken { return "未分组的服务器" }
        return groups.first(where: { $0.id == selection })?.displayName ?? "未选择"
    }

    private func claimGroupDetail(_ group: ServerGroupRecord, members: [EndpointRecord]) -> String {
        let notes = group.environmentNotes.trimmingCharacters(in: .whitespacesAndNewlines)
        var parts = [endpointGroupCapacitySummary(members, group: group, store: store)]
        if let ends = group.limits?.leaseEnds {
            switch ends {
            case .onRelease:
                parts.append("直到显式释放")
            case .hardKillAtTimeLimit:
                if let seconds = group.limits?.maxLeaseSeconds {
                    parts.append("\(durationLabel(seconds))，到期硬杀")
                } else {
                    parts.append("到期硬杀")
                }
            }
        }
        parts.append(group.workspacePath)
        if !notes.isEmpty { parts.append(notes) }
        return parts.joined(separator: " · ")
    }

    private func option(id: String, title: String, detail: String) -> some View {
        let selected = selection == id
        return Button {
            selection = id
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(detail)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.fill) : DesignTokens.ink.opacity(DesignTokens.Alpha.hairline),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                    .stroke(selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.muted) : DesignTokens.surfaceStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue("\(detail)，\(selected ? "已选择" : "未选择")")
    }
}

private struct ClaimEndpointPicker: View {
    let endpoints: [EndpointRecord]
    @Binding var selection: String

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 7) {
                option(
                    id: "",
                    title: "自动选择",
                    detail: "由 ServerPilot 选择可用服务器"
                )
                ForEach(endpoints) { endpoint in
                    option(
                        id: endpoint.id,
                        title: endpoint.sshCommand,
                        detail: endpoint.workspacePath ?? "工作区未设置"
                    )
                }
            }
            .padding(1)
        }
        .frame(maxHeight: 224)
        .accessibilityLabel("服务器")
        .accessibilityValue(selection.isEmpty ? "自动选择" : selectedEndpointDescription)
    }

    private var selectedEndpointDescription: String {
        guard let endpoint = endpoints.first(where: { $0.id == selection }) else { return "未选择" }
        return "\(endpoint.sshCommand)，工作区 \(endpoint.workspacePath ?? "未设置")"
    }

    private func option(id: String, title: String, detail: String) -> some View {
        let selected = selection == id
        return Button {
            selection = id
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.system(.callout, design: id.isEmpty ? .default : .monospaced).weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(detail)
                        .font(.system(.subheadline, design: id.isEmpty ? .default : .monospaced).weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.fill) : DesignTokens.ink.opacity(DesignTokens.Alpha.hairline),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                    .stroke(selected ? DesignTokens.interaction.opacity(DesignTokens.Alpha.muted) : DesignTokens.surfaceStroke, lineWidth: 1)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(title)
        .accessibilityValue("工作区 \(detail)，\(selected ? "已选择" : "未选择")")
    }
}

private struct ServerGroupPathAssignment {
    let serverGroupID: String?
    let effectiveWorkspacePath: String
    let workspacePathOverride: String?

    init(
        groups: [ServerGroupRecord],
        serverGroupID: String,
        inheritGroupPath: Bool,
        workspacePath: String
    ) throws {
        let cleanedGroupID = serverGroupID.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanedPath = workspacePath.trimmingCharacters(in: .whitespacesAndNewlines)
        if cleanedGroupID.isEmpty {
            self.serverGroupID = nil
            self.effectiveWorkspacePath = cleanedPath
            self.workspacePathOverride = nil
            return
        }
        guard groups.contains(where: { $0.id == cleanedGroupID }) else {
            throw EndpointDraftError.invalidEndpointFields
        }
        self.serverGroupID = cleanedGroupID
        if inheritGroupPath {
            self.effectiveWorkspacePath = ""
            self.workspacePathOverride = nil
        } else {
            self.effectiveWorkspacePath = cleanedPath
            self.workspacePathOverride = cleanedPath
        }
    }
}

private struct ServerGroupAssignmentFields: View {
    @Binding var serverGroupID: String
    @Binding var inheritGroupPath: Bool
    @Binding var workspacePath: String
    let groups: [ServerGroupRecord]

    private var selectedGroup: ServerGroupRecord? {
        groups.first { $0.id == serverGroupID }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            VStack(alignment: .leading, spacing: 8) {
                Text("服务器组")
                    .fieldLabel()
                Picker("服务器组", selection: $serverGroupID) {
                    Text("未分组").tag("")
                    ForEach(groups) { group in
                        Text(group.displayName).tag(group.id)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityLabel("服务器组")
                .accessibilityValue(selectedGroup?.displayName ?? "未分组")
                if let group = selectedGroup {
                    Text("组默认路径 \(group.workspacePath)")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .textSelection(.enabled)
                    Toggle("使用本组默认路径", isOn: $inheritGroupPath)
                        .toggleStyle(.checkbox)
                        .accessibilityLabel("使用本组默认路径")
                }
            }
            if selectedGroup == nil || !inheritGroupPath {
                LabeledField(
                    label: selectedGroup == nil ? "远端工作区路径" : "本机路径覆盖",
                    placeholder: "/srv/serverpilot-workspace",
                    text: $workspacePath
                )
            }
        }
        .onChange(of: serverGroupID) { _, newValue in
            if newValue.isEmpty {
                inheritGroupPath = true
            } else {
                inheritGroupPath = true
            }
        }
    }
}

private enum ServerGroupSheetMode: Equatable {
    case list
    case create
    case edit(String)
}

private struct ManageServerGroupsSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var mode: ServerGroupSheetMode = .list
    @State private var displayName = ""
    @State private var groupID = ""
    @State private var workspacePath = ""
    @State private var environmentNotes = ""
    @State private var descriptionText = ""
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    private var groups: [ServerGroupRecord] {
        store.snapshot.serverGroups.sorted {
            $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending
        }
    }

    private var canMutateGroups: Bool {
        store.allowsMutations && store.supportsServerGroupCRUD
    }

    private var groupMutationHelp: String {
        if !store.allowsMutations { return store.mutationUnavailableReason }
        if !store.supportsServerGroupCRUD { return "当前服务不支持服务器组变更。" }
        return "管理服务器组"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(
                icon: "rectangle.3.group",
                title: "服务器组",
                subtitle: "同一路径和环境的机器放在一组；申请 GPU 时先选组。"
            )
            switch mode {
            case .list:
                groupList
            case .create, .edit:
                groupEditor
            }
            if let validationMessage {
                InlineValidation(message: validationMessage)
            }
        }
        .padding(28)
        .frame(width: 640, height: 640)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder
    private var groupList: some View {
        if groups.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("还没有服务器组。")
                    .font(Typography.sectionTitle)
                Text("创建后即可把共享路径和环境的机器放在一起申请。")
                    .font(Typography.secondary)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(groups) { group in
                        groupRow(group)
                    }
                }
            }
            .frame(maxHeight: .infinity)
        }
        HStack {
            Spacer()
            Button("完成") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("添加服务器组") { beginCreate() }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                .disabled(!canMutateGroups || isSubmitting)
                .help(groupMutationHelp)
                .accessibilityLabel("添加服务器组")
        }
    }

    private func groupRow(_ group: ServerGroupRecord) -> some View {
        let members = store.snapshot.endpoints(inGroup: group.id)
        let summary = endpointGroupCapacitySummary(members, group: group, store: store)
        return VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text(group.displayName)
                    .font(Typography.label)
                    .foregroundStyle(DesignTokens.ink)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(summary)
                    .font(Typography.rowValue)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            Text(group.workspacePath)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.mutedInk)
                .lineLimit(1)
                .truncationMode(.middle)
                .textSelection(.enabled)
            if !group.environmentNotes.isEmpty {
                Text(group.environmentNotes)
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("编辑") { beginEdit(group) }
                    .buttonStyle(.borderless)
                    .disabled(isSubmitting)
                    .accessibilityLabel("编辑服务器组 \(group.displayName)")
                Button("删除…") { deleteGroup(group) }
                    .buttonStyle(.borderless)
                    .foregroundStyle(DesignTokens.danger)
                    .disabled(!canMutateGroups || isSubmitting)
                    .help(groupMutationHelp)
                    .accessibilityLabel("删除服务器组 \(group.displayName)")
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            DesignTokens.ink.opacity(DesignTokens.Alpha.hairline),
            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel("服务器组 \(group.displayName)")
        .accessibilityValue("\(summary)，路径 \(group.workspacePath)")
    }

    @ViewBuilder
    private var groupEditor: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                LabeledField(label: "显示名称", placeholder: "训练服务器组", text: $displayName)
                if mode == .create {
                    LabeledField(label: "分组标识", placeholder: "training-lab", text: $groupID)
                    Text("创建后不能修改。只能使用小写字母、数字和连字符。")
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.mutedInk)
                }
                LabeledField(label: "默认工作区路径", placeholder: "/srv/shared-workspace", text: $workspacePath)
                LabeledField(label: "环境说明", placeholder: "CUDA 版本、共享盘与权重状态", text: $environmentNotes)
                LabeledField(label: "说明", placeholder: "这组机器的数据与用途", text: $descriptionText)
                Text("环境说明只给操作者阅读，不会进入采集、插件或远端进程环境。")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxHeight: .infinity)
        HStack {
            Spacer()
            Button("取消") { cancelEditor() }
                .keyboardShortcut(.cancelAction)
            Button(mode == .create ? "创建服务器组" : "保存服务器组") { submitEditor() }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                .keyboardShortcut(.defaultAction)
                .disabled(!canMutateGroups || isSubmitting)
                .help(groupMutationHelp)
        }
    }

    private func beginCreate() {
        mode = .create
        displayName = ""
        groupID = ""
        workspacePath = ""
        environmentNotes = ""
        descriptionText = ""
        validationMessage = nil
    }

    private func beginEdit(_ group: ServerGroupRecord) {
        mode = .edit(group.id)
        displayName = group.displayName
        workspacePath = group.workspacePath
        environmentNotes = group.environmentNotes
        descriptionText = group.description
        validationMessage = nil
    }

    private func cancelEditor() {
        mode = .list
        validationMessage = nil
        isSubmitting = false
    }

    private func submitEditor() {
        do {
            switch mode {
            case .create:
                let draft = try ServerGroupDraft(
                    id: groupID,
                    displayName: displayName,
                    workspacePath: workspacePath,
                    environmentNotes: environmentNotes,
                    description: descriptionText
                )
                validationMessage = nil
                isSubmitting = true
                store.createServerGroup(draft) { success, error in
                    isSubmitting = false
                    if success {
                        mode = .list
                    } else {
                        validationMessage = error
                    }
                }
            case .edit(let groupID):
                guard let group = store.snapshot.serverGroups.first(where: { $0.id == groupID }) else {
                    validationMessage = "该服务器组已不在当前快照中。"
                    return
                }
                let draft = try ServerGroupUpdateDraft(
                    displayName: displayName,
                    workspacePath: workspacePath,
                    environmentNotes: environmentNotes,
                    description: descriptionText
                )
                validationMessage = nil
                isSubmitting = true
                store.updateServerGroup(group, draft: draft) { success, error in
                    isSubmitting = false
                    if success {
                        mode = .list
                    } else {
                        validationMessage = error
                    }
                }
            case .list:
                break
            }
        } catch {
            validationMessage = error.localizedDescription
        }
    }

    private func deleteGroup(_ group: ServerGroupRecord) {
        guard confirmServerGroupDelete(group) else { return }
        validationMessage = nil
        isSubmitting = true
        store.deleteServerGroup(group) { success, error in
            isSubmitting = false
            if !success {
                validationMessage = error
            }
        }
    }
}

private struct SheetTitle: View {
    let icon: String
    let title: String
    let subtitle: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.title2.weight(.semibold))
                .foregroundStyle(DesignTokens.ink)
                .frame(width: 42, height: 42)
                .background(DesignTokens.selection, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                Text(subtitle)
                    .font(.callout.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
    }
}

private struct LabeledField: View {
    let label: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label)
                .fieldLabel()
            TextField(placeholder, text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}

private struct InlineValidation: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(.callout.weight(.medium))
            .foregroundStyle(DesignTokens.danger)
    }
}

private struct InlineResult: View {
    let message: String
    let allocated: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: allocated ? "checkmark.circle.fill" : "hourglass")
                .foregroundStyle(allocated ? DesignTokens.success : DesignTokens.warning)
            Text(message)
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(.callout.weight(.medium))
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            (allocated ? DesignTokens.success : DesignTokens.warning).opacity(DesignTokens.Alpha.fill),
            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                .stroke((allocated ? DesignTokens.success : DesignTokens.warning).opacity(DesignTokens.Alpha.muted), lineWidth: 1)
        )
    }
}
