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


struct NoticeBanner: View {
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

