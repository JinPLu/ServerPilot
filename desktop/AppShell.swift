import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct StableRecordSelection: Identifiable, Equatable {
    let id: String
}


struct NativeBrokerRoot: View {
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
        case "alerts": .alerts
        case "overview": .overview
        case "settings": .settings
        default: .overview
        }
        _selectedDashboardSection = State(initialValue: initialSection)
#else
        _selectedDashboardSection = State(initialValue: .overview)
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

            SidebarSelection(title: "总览", systemImage: "square.grid.2x2.fill", color: DesignTokens.interaction, selected: selectedSection == .overview, compact: compact) {
                navigate(.overview)
            }
            SidebarSelection(title: "服务器", systemImage: "server.rack", color: DesignTokens.interaction, selected: selectedSection == .resources, compact: compact) {
                navigate(.resources)
            }
            SidebarSelection(title: "任务", systemImage: "chart.bar.xaxis", color: DesignTokens.interaction, selected: selectedSection == .leases, compact: compact) {
                navigate(.leases)
            }
            SidebarSelection(title: store.openAlertCount > 0 ? "提醒 \(store.openAlertCount)" : "提醒", systemImage: "bell.fill", color: DesignTokens.interaction, selected: selectedSection == .alerts, compact: compact) {
                navigate(.alerts)
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
        .focusEffectDisabled()
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
                    if selectedSection == .leases {
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
                    }
                    Button(action: refresh) {
                        // Pulse refreshes every few seconds, so this must not
                        // react to a refresh a person did not ask for.
                        Label("刷新", systemImage: "arrow.clockwise")
                            .font(.callout.weight(.semibold))
                    }
                    .buttonStyle(SecondaryActionButtonStyle())
                    .focusable()
                    .disabled(!store.canRefresh)
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
        .padding(.top, 22)
        .padding(.bottom, 10)
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
        case .overview: return "总览"
        case .resources: return "服务器"
        case .leases: return "任务"
        case .alerts: return "提醒"
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

