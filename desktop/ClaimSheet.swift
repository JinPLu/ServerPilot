import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct ClaimSheet: View {
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

