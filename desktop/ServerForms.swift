import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct AddServerSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    @State private var sshCommand = ""
    @State private var serverGroupID = ""
    @State private var inheritGroupPath = true
    @State private var workspacePath = ""
    @State private var observationProfile = "linux"
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
                        .font(Typography.command)
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
                knownGroupIDs: Set(store.snapshot.serverGroups.map(\.id)),
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


struct EditServerSheet: View {
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
                        .font(Typography.command.weight(.semibold))
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
                            .font(Typography.identity.weight(.semibold))
                            .foregroundStyle(DesignTokens.danger)
                        Text("从本机控制面移除这台服务器。会停止监控与协调并删除本机关联记录，不会停止远端进程。")
                            .font(Typography.annotation)
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
        guard confirmEndpointsDelete([endpoint]) else { return }
        validationMessage = nil
        isSubmitting = true
        store.deleteEndpoints([endpoint]) { result in
            isSubmitting = false
            if let failure = result.failureSummary {
                validationMessage = failure
            } else {
                onRemoved()
                dismiss()
            }
        }
    }

    private func submit() {
        do {
            let assignment = try ServerGroupPathAssignment(
                knownGroupIDs: Set(store.snapshot.serverGroups.map(\.id)),
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
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("采集方式")
        .accessibilityValue(profiles.first(where: { $0.id == selection })?.displayName ?? selection)
    }
}

let ungroupedClaimToken = "__ungrouped__"


private struct ServerGroupPathAssignment {
    let serverGroupID: String?
    let effectiveWorkspacePath: String
    let workspacePathOverride: String?

    init(
        knownGroupIDs: Set<String>,
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
        guard knownGroupIDs.contains(cleanedGroupID) else {
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
                        .font(Typography.annotation)
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


/// Where the server group sheet opens: its list, or straight into one form, as
/// the sidebar's add button and a group's context menu do.
enum ServerGroupSheetMode: Hashable, Identifiable {
    case list
    case create
    case edit(String)

    var id: String {
        switch self {
        case .list: "list"
        case .create: "create"
        case .edit(let groupID): "edit:\(groupID)"
        }
    }
}


struct ManageServerGroupsSheet: View {
    @ObservedObject var store: BrokerStore
    let opening: ServerGroupSheetMode
    @Environment(\.dismiss) private var dismiss
    @State private var mode: ServerGroupSheetMode = .list
    @State private var displayName = ""
    @State private var workspacePath = ""
    @State private var environmentNotes = ""
    @State private var descriptionText = ""
    /// The servers ticked to be in the group once the form is saved.
    @State private var memberIDs: Set<String> = []
    @State private var joinersUseGroupPath = true
    @State private var validationMessage: String?
    @State private var isSubmitting = false

    private var groups: [ServerGroupRecord] {
        store.snapshot.serverGroups.sorted {
            $0.displayName.localizedStandardCompare($1.displayName) == .orderedAscending
        }
    }

    private var servers: [EndpointRecord] {
        store.snapshot.endpoints.sorted {
            $0.connectionAddress.localizedStandardCompare($1.connectionAddress) == .orderedAscending
        }
    }

    private var editingGroupID: String? {
        if case .edit(let groupID) = mode { return groupID }
        return nil
    }

    /// The id a new group is saved under, made from its name.
    private var newGroupID: String {
        ServerGroupIdentifier.make(from: displayName, taken: Set(store.snapshot.serverGroups.map(\.id)))
    }

    private var canMutateGroups: Bool {
        store.allowsMutations && store.supportsServerGroupCRUD
    }

    private var groupMutationHelp: String {
        if !store.allowsMutations { return store.mutationUnavailableReason }
        if !store.supportsServerGroupCRUD { return "当前服务不支持服务器组变更。" }
        return "管理服务器组"
    }

    private var title: String {
        switch mode {
        case .list: "服务器组"
        case .create: "新建服务器组"
        case .edit: "编辑服务器组"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SheetTitle(
                icon: "rectangle.3.group",
                title: title,
                subtitle: "同一路径和环境的机器放在一组；申请 GPU 时先选组。"
            )
            switch mode {
            case .list:
                groupList
            case .create, .edit:
                groupEditor
            }
        }
        .padding(28)
        .frame(width: 640, height: 720)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityElement(children: .contain)
        .onAppear { open(opening) }
    }

    @ViewBuilder
    private var groupList: some View {
        if groups.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("还没有服务器组。")
                    .font(Typography.label)
                Text("创建后即可把共享路径和环境的机器放在一起申请。")
                    .font(Typography.annotation)
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
        if let validationMessage {
            InlineValidation(message: validationMessage)
        }
        HStack {
            Spacer()
            Button("完成") { dismiss() }
                .keyboardShortcut(.cancelAction)
            Button("新建服务器组") { beginCreate() }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                .disabled(!canMutateGroups || isSubmitting)
                .help(groupMutationHelp)
                .accessibilityLabel("新建服务器组")
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
                Text(members.isEmpty ? "还没有服务器" : "\(members.count) 台 · \(summary)")
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
                // The control plane refuses a group that still has servers. A
                // fixed red kept a disabled button looking pressable.
                let canDelete = canMutateGroups && !isSubmitting && members.isEmpty
                Button("删除…") { deleteGroup(group) }
                    .buttonStyle(.borderless)
                    .foregroundStyle(canDelete ? DesignTokens.danger : DesignTokens.mutedInk)
                    .disabled(!canDelete)
                    .help(members.isEmpty ? groupMutationHelp : "组内还有 \(members.count) 台服务器；先在“编辑”里把它们移出，才能删除这个组")
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
            VStack(alignment: .leading, spacing: 18) {
                VStack(alignment: .leading, spacing: 6) {
                    LabeledField(label: "名称", placeholder: "训练服务器组", text: $displayName)
                    Text(identifierLine)
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.mutedInk)
                        .textSelection(.enabled)
                }
                memberPicker
                VStack(alignment: .leading, spacing: 8) {
                    LabeledField(label: "默认工作路径", placeholder: "/srv/shared-workspace", text: $workspacePath)
                    Toggle("新加入的服务器使用这个路径", isOn: $joinersUseGroupPath)
                        .toggleStyle(.checkbox)
                        .accessibilityLabel("新加入的服务器使用这个路径")
                    Text(joinersUseGroupPath
                        ? "Agent 进入这些服务器时在这个目录下工作。勾选第一台服务器时，会先填上它现在的路径。"
                        : "新加入的服务器保留各自原来的路径。")
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                }
                LabeledField(label: "环境说明（可选）", placeholder: "CUDA 版本、共享盘与权重状态", text: $environmentNotes)
                LabeledField(label: "说明（可选）", placeholder: "这组机器的数据与用途", text: $descriptionText)
                Text("环境说明只给操作者阅读，不会进入采集、插件或远端进程环境。")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.trailing, 6)
        }
        .frame(maxHeight: .infinity)
        if let validationMessage {
            InlineValidation(message: validationMessage)
                .fixedSize(horizontal: false, vertical: true)
        }
        HStack {
            Spacer()
            Button("取消") { leaveEditor() }
                .keyboardShortcut(.cancelAction)
            Button(mode == .create ? "创建服务器组" : "保存服务器组") { submitEditor() }
                .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                .keyboardShortcut(.defaultAction)
                .disabled(!canMutateGroups || isSubmitting)
                .help(groupMutationHelp)
        }
    }

    private var identifierLine: String {
        if let editingGroupID { return "标识 \(editingGroupID)。Agent 申请 GPU 时用它选组，不能修改。" }
        if displayName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "标识按名称自动生成，Agent 申请 GPU 时用它选组。"
        }
        return "标识 \(newGroupID)，按名称自动生成，Agent 申请 GPU 时用它选组。"
    }

    private var memberPicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text("服务器")
                    .fieldLabel()
                Spacer()
                Text("已选 \(memberIDs.count) 台")
                    .font(Typography.annotation)
                    .monospacedDigit()
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            if servers.isEmpty {
                Text("还没有服务器。添加服务器后，可以在这里把它放进组。")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
            } else {
                VStack(spacing: 0) {
                    ForEach(servers) { endpoint in
                        memberRow(endpoint)
                        if endpoint.id != servers.last?.id {
                            Divider()
                        }
                    }
                }
                .padding(.horizontal, 12)
                .background(
                    DesignTokens.surface,
                    in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                        .strokeBorder(DesignTokens.tileStroke, lineWidth: 1)
                )
                Text("已在其他组的服务器勾选后会移到这个组；取消勾选的成员回到未分组。")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func memberRow(_ endpoint: EndpointRecord) -> some View {
        Toggle(isOn: memberBinding(endpoint)) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(endpoint.connectionAddress)
                        .font(Typography.body)
                        .foregroundStyle(DesignTokens.ink)
                        .lineLimit(1)
                    if let path = endpoint.workspacePath, !path.isEmpty {
                        Text(path)
                            .font(Typography.commandSmall)
                            .foregroundStyle(DesignTokens.mutedInk)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                Spacer(minLength: 8)
                Text(placeLabel(endpoint))
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .lineLimit(1)
            }
        }
        .toggleStyle(.checkbox)
        .padding(.vertical, 8)
        .disabled(isSubmitting)
        .accessibilityLabel(endpoint.connectionAddress)
        .accessibilityValue(placeLabel(endpoint))
    }

    private func memberBinding(_ endpoint: EndpointRecord) -> Binding<Bool> {
        Binding(
            get: { memberIDs.contains(endpoint.id) },
            set: { ticked in
                if ticked {
                    memberIDs.insert(endpoint.id)
                    let path = endpoint.workspacePath?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                    if workspacePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !path.isEmpty {
                        workspacePath = path
                    }
                } else {
                    memberIDs.remove(endpoint.id)
                }
            }
        )
    }

    /// Where a server is now, and where saving takes it.
    private func placeLabel(_ endpoint: EndpointRecord) -> String {
        let ticked = memberIDs.contains(endpoint.id)
        guard let groupID = endpoint.serverGroupID else {
            return ticked ? "未分组，保存后加入" : "未分组"
        }
        if groupID == editingGroupID {
            return ticked ? "本组" : "保存后移出本组"
        }
        let name = store.snapshot.serverGroups.first { $0.id == groupID }?.displayName ?? groupID
        return ticked ? "从「\(name)」移过来" : "在「\(name)」"
    }

    private func open(_ requested: ServerGroupSheetMode) {
        switch requested {
        case .list:
            mode = .list
        case .create:
            beginCreate()
        case .edit(let groupID):
            guard let group = store.snapshot.serverGroups.first(where: { $0.id == groupID }) else { return }
            beginEdit(group)
        }
    }

    private func beginCreate() {
        mode = .create
        displayName = ""
        workspacePath = ""
        environmentNotes = ""
        descriptionText = ""
        memberIDs = []
        joinersUseGroupPath = true
        validationMessage = nil
    }

    private func beginEdit(_ group: ServerGroupRecord) {
        mode = .edit(group.id)
        displayName = group.displayName
        workspacePath = group.workspacePath
        environmentNotes = group.environmentNotes
        descriptionText = group.description
        memberIDs = Set(store.snapshot.endpoints(inGroup: group.id).map(\.id))
        joinersUseGroupPath = true
        validationMessage = nil
    }

    /// A form reached from the list goes back to it; one the sheet opened on
    /// closes the sheet, so nobody lands in a list they never asked for.
    private func leaveEditor() {
        validationMessage = nil
        isSubmitting = false
        if opening == .list {
            mode = .list
        } else {
            dismiss()
        }
    }

    private func submitEditor() {
        do {
            switch mode {
            case .create:
                let draft = try ServerGroupDraft(
                    id: newGroupID,
                    displayName: displayName,
                    workspacePath: workspacePath,
                    environmentNotes: environmentNotes,
                    description: descriptionText
                )
                validationMessage = nil
                isSubmitting = true
                store.createServerGroup(draft) { success, error in
                    guard success else {
                        isSubmitting = false
                        validationMessage = error
                        return
                    }
                    // From here the form edits the group it made, so saving
                    // again only retries the servers that did not move.
                    mode = .edit(draft.id)
                    moveMembers(groupID: draft.id, groupPath: draft.workspacePath)
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
                    guard success else {
                        isSubmitting = false
                        validationMessage = error
                        return
                    }
                    moveMembers(groupID: group.id, groupPath: draft.workspacePath)
                }
            case .list:
                break
            }
        } catch {
            validationMessage = error.localizedDescription
        }
    }

    /// Moves the ticked servers in and the unticked members out.
    private func moveMembers(groupID: String, groupPath: String) {
        let knownGroupIDs = Set(store.snapshot.serverGroups.map(\.id)).union([groupID])
        var moves: [ServerMove] = []
        var refused: [ServerMoveFailure] = []
        for endpoint in servers {
            let isMember = endpoint.serverGroupID == groupID
            guard isMember != memberIDs.contains(endpoint.id) else { continue }
            do {
                moves.append(try serverMove(
                    endpoint,
                    toGroup: isMember ? "" : groupID,
                    groupPath: groupPath,
                    useGroupPath: joinersUseGroupPath,
                    knownGroupIDs: knownGroupIDs
                ))
            } catch {
                refused.append(ServerMoveFailure(
                    endpointName: endpoint.connectionAddress,
                    message: "没有可用的工作路径，请先在这台服务器的「编辑」里设置。"
                ))
            }
        }
        guard !moves.isEmpty else {
            finishMoving(refused)
            return
        }
        store.moveServers(moves) { failures in
            finishMoving(refused + failures)
        }
    }

    private func finishMoving(_ failures: [ServerMoveFailure]) {
        isSubmitting = false
        guard !failures.isEmpty else {
            leaveEditor()
            return
        }
        var lines = ["已保存服务器组，这些服务器没有移动："]
        lines += failures.map { "\($0.endpointName)：\($0.message)" }
        if joinersUseGroupPath {
            lines.append("正在占卡的服务器可以先停掉占卡，或取消勾选「新加入的服务器使用这个路径」后再保存。")
        }
        validationMessage = lines.joined(separator: "\n")
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


/// The update that moves one server into a group, or out of every group when
/// `groupID` is empty, keeping how it connects and how it is observed.  A
/// server joining keeps its own path only when asked to, and only when that
/// path differs from the group's.
private func serverMove(
    _ endpoint: EndpointRecord,
    toGroup groupID: String,
    groupPath: String,
    useGroupPath: Bool,
    knownGroupIDs: Set<String>
) throws -> ServerMove {
    let ownPath = endpoint.workspacePath?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    let assignment = try ServerGroupPathAssignment(
        knownGroupIDs: knownGroupIDs,
        serverGroupID: groupID,
        inheritGroupPath: useGroupPath || ownPath.isEmpty || ownPath == groupPath,
        workspacePath: ownPath
    )
    return ServerMove(
        endpoint: endpoint,
        draft: try EndpointUpdateDraft(
            sshUser: endpoint.sshUser,
            workspacePath: assignment.effectiveWorkspacePath,
            observationProfile: endpoint.observationProfile,
            serverGroupID: assignment.serverGroupID,
            workspacePathOverride: assignment.workspacePathOverride
        )
    )
}
