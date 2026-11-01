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
    /// nil is every server; "" is the servers outside any group.
    @State private var selectedGroupID: String?
    @State private var resourceFilter: EndpointFilter = .all

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
            let compactNavigation = proxy.size.width < 1100
            let sidebarWidth: CGFloat = compactNavigation ? 60 : 216

            // The system's own sidebar column: its material is what the OS draws
            // for every other app, and everything carrying data stays opaque.
            NavigationSplitView(columnVisibility: .constant(.all)) {
                AppSidebar(
                    store: store,
                    selectedSection: selectedDashboardSection,
                    selectedGroupID: selectedGroupID,
                    compact: compactNavigation,
                    navigate: { section in
                        selectedGroupID = nil
                        resourceFilter = .all
                        selectedDashboardSection = section
                    },
                    openGroup: { groupID in
                        selectedGroupID = groupID
                        resourceFilter = .all
                        selectedDashboardSection = .resources
                    }
                )
                .toolbar(removing: .sidebarToggle)
                .navigationSplitViewColumnWidth(min: sidebarWidth, ideal: sidebarWidth, max: sidebarWidth)
            } detail: {
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
                        selectedGroupID: selectedGroupID,
                        resourceFilter: $resourceFilter,
                        selectGPU: { gpu in
                            selectedGPUID = gpu.id
                        }
                    )
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .clipped()
                .background(AmbientBackground())
            }
            .navigationSplitViewStyle(.balanced)
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
        .onReceive(NotificationCenter.default.publisher(for: .serverPilotRequestClaim)) { _ in
            claimInitialEndpointID = ""
            showClaim = true
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
    let selectedGroupID: String?
    let compact: Bool
    let navigate: (DashboardSection) -> Void
    let openGroup: (String) -> Void

    private var fleets: [ServerFleet] { serverFleets(in: store.snapshot) }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Color.clear.frame(height: 8)

            SidebarSelection(title: "总览", systemImage: "square.grid.2x2", selected: selectedSection == .overview, compact: compact) {
                navigate(.overview)
            }
            SidebarSelection(title: "服务器", systemImage: "server.rack", selected: selectedSection == .resources && selectedGroupID == nil, compact: compact) {
                navigate(.resources)
            }
            SidebarSelection(title: "任务", systemImage: "list.bullet.rectangle", badge: store.snapshot.leases.isEmpty ? nil : "\(store.snapshot.leases.count)", selected: selectedSection == .leases, compact: compact) {
                navigate(.leases)
            }
            SidebarSelection(title: "提醒", systemImage: "bell", badge: store.openAlertCount > 0 ? "\(store.openAlertCount)" : nil, selected: selectedSection == .alerts, compact: compact) {
                navigate(.alerts)
            }

            // Server groups are the rooms of this house: go straight to one.
            if !compact, !fleets.isEmpty {
                Text("服务器组")
                    .font(Typography.annotation.weight(.semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .padding(.horizontal, 20)
                    .padding(.top, 18)
                    .padding(.bottom, 4)
                    .accessibilityAddTraits(.isHeader)
                ForEach(fleets) { fleet in
                    SidebarSelection(title: fleet.name, systemImage: nil, badge: "\(fleet.freeCardCount(in: store.snapshot))", selected: selectedSection == .resources && selectedGroupID == fleet.id, compact: false) {
                        openGroup(fleet.id)
                    }
                }
            }

            Spacer(minLength: 16)

            if !store.isConnected, let error = store.errorMessage, !compact {
                HStack(alignment: .firstTextBaseline, spacing: 7) {
                    Circle()
                        .fill(DesignTokens.danger)
                        .frame(width: 7, height: 7)
                    Text(error)
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(2)
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 8)
            }

            SidebarSelection(title: "设置", systemImage: "gearshape", selected: selectedSection == .settings, compact: compact) {
                navigate(.settings)
            }
        }
        .padding(.bottom, 10)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}


private struct SidebarSelection: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var hovering = false
    let title: String
    let systemImage: String?
    var badge: String? = nil
    let selected: Bool
    let compact: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 9) {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(.body)
                        .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                        .frame(width: 18)
                }
                if !compact {
                    Text(title)
                        .font(Font.body.weight(selected ? .semibold : .regular))
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    if let badge {
                        Text(badge)
                            .font(Typography.annotation)
                            .monospacedDigit()
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
            }
            .padding(.horizontal, compact ? 0 : 10)
            .frame(height: 32)
            .frame(maxWidth: .infinity)
            .background(
                DesignTokens.ink.opacity(selected ? DesignTokens.Alpha.edge : (hovering ? DesignTokens.Alpha.hairline : 0)),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .focusable()
        .padding(.horizontal, compact ? 8 : 10)
        .focusEffectDisabled()
        .help(title)
        .accessibilityLabel(title)
        .accessibilityValue(selected ? "当前页面" : (badge ?? "未选中"))
        .onHover { hovering = $0 }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.14), value: hovering)
    }
}


private struct AppToolbar: View {
    @ObservedObject var store: BrokerStore
    let selectedSection: DashboardSection
    let addServer: () -> Void
    let claimGPU: () -> Void
    let refresh: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(title)
                .font(Typography.screenTitle)
                .foregroundStyle(DesignTokens.ink)
                .lineLimit(1)
                .fixedSize()
            VStack(alignment: .leading, spacing: 1) {
                Text(statusText)
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
                if let error = store.errorMessage {
                    Text(error)
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.danger)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .accessibilityLabel("当前刷新错误")
                        .accessibilityValue(error)
                }
            }
            .padding(.leading, 6)
            Spacer(minLength: 14)
            Button(action: refresh) {
                // Pulse refreshes every few seconds, so this must not react to
                // a refresh a person did not ask for.
                Image(systemName: "arrow.clockwise")
                    .font(.callout.weight(.medium))
            }
            .buttonStyle(IconActionButtonStyle())
            .focusable()
            .disabled(!store.canRefresh)
            .keyboardShortcut("r", modifiers: [.command])
            .help(store.canRefresh ? "更新资源数据" : "测试数据不能刷新")
            .accessibilityLabel("更新资源数据")
            .accessibilityValue(store.isRefreshing ? "正在更新" : (store.canRefresh ? "可以更新" : "测试数据"))
            if selectedSection == .resources {
                Button(action: addServer) {
                    Text("添加服务器")
                }
                .buttonStyle(SecondaryActionButtonStyle())
                .focusable()
                .disabled(!store.allowsMutations)
                .help(store.allowsMutations ? "添加服务器到本机资源池" : store.mutationUnavailableReason)
                .accessibilityLabel("添加服务器")
            }
            if [.overview, .resources, .leases].contains(selectedSection) {
                Button(action: claimGPU) {
                    Text("申请 GPU")
                }
                .buttonStyle(PrimaryActionButtonStyle())
                .focusable()
                .disabled(!store.allowsMutations || store.snapshot.operationalEndpoints.isEmpty)
                .help(claimHelp)
                .accessibilityLabel("申请 GPU")
                .accessibilityHint(claimHint)
                .accessibilityValue(claimHint)
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 14)
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

    private var lastUpdatedText: String {
        guard let lastUpdated = store.lastUpdated else { return "—" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "HH:mm"
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

