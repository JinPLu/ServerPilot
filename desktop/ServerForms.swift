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

let ungroupedClaimToken = "__ungrouped__"


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


struct ManageServerGroupsSheet: View {
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

