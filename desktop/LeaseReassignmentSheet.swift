import AppKit
import SwiftUI

struct LeaseReassignmentSheet: View {
    @ObservedObject var store: BrokerStore
    @Environment(\.dismiss) private var dismiss
    let lease: LeaseRecord
    @State private var selectedGPUIDs: Set<String>
    @State private var resultMessage: String?

    init(store: BrokerStore, lease: LeaseRecord) {
        self.store = store
        self.lease = lease
        _selectedGPUIDs = State(initialValue: Set(lease.gpuIDs))
    }

    private var candidates: [GPURecord] {
        store.snapshot.gpus
            .filter { lease.gpuIDs.contains($0.id) || $0.isPubliclyAvailable }
            .sorted { lhs, rhs in
                if lhs.endpointID == rhs.endpointID { return lhs.index < rhs.index }
                return lhs.endpointID < rhs.endpointID
            }
    }

    private var selectionIsComplete: Bool {
        selectedGPUIDs.count == lease.gpuCount
    }

    private var selectionChanged: Bool {
        selectedGPUIDs != Set(lease.gpuIDs)
    }

    private var succeeded: Bool {
        resultMessage?.hasPrefix("分配已更新") == true
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: "arrow.triangle.swap")
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(DesignTokens.interaction)
                    .frame(width: 42, height: 42)
                    .background(DesignTokens.interaction.opacity(DesignTokens.Alpha.fill), in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
                VStack(alignment: .leading, spacing: 3) {
                    Text("改派 GPU")
                        .font(.title2.weight(.semibold))
                        .foregroundStyle(DesignTokens.ink)
                    Text("\(lease.projectID) · \(lease.taskReference ?? lease.purpose ?? "未命名任务")")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(2)
                }
            }

            Text("选择 \(lease.gpuCount) 块目标 GPU。应用后，请让对应 Agent 按新分配的 CVD 重启任务。")
                .font(.callout.weight(.medium))
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: true)

            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(candidates) { gpu in
                        gpuOption(gpu)
                    }
                }
                .padding(1)
            }
            .frame(minHeight: 180, maxHeight: 360)

            if !selectionIsComplete {
                Text("还需选择 \(lease.gpuCount - selectedGPUIDs.count) 块 GPU。")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(DesignTokens.warning)
            }

            if let resultMessage {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: succeeded ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(succeeded ? DesignTokens.success : DesignTokens.danger)
                    Text(resultMessage)
                        .foregroundStyle(DesignTokens.ink)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .font(.subheadline.weight(.medium))
                .padding(11)
                .background(
                    (succeeded ? DesignTokens.success : DesignTokens.danger).opacity(DesignTokens.Alpha.fill),
                    in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                )
            }

            HStack {
                Spacer()
                Button(succeeded ? "完成" : "取消") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                if !succeeded {
                    Button {
                        applyReassignment()
                    } label: {
                        Label(
                            store.reassigningLeaseIDs.contains(lease.id) ? "应用中" : "应用分配",
                            systemImage: "arrow.triangle.swap"
                        )
                    }
                    .buttonStyle(SoftButtonStyle(tint: DesignTokens.ink, foreground: DesignTokens.onInteraction))
                    .keyboardShortcut(.defaultAction)
                    .disabled(
                        !store.allowsMutations
                            || store.reassigningLeaseIDs.contains(lease.id)
                            || !selectionIsComplete
                            || !selectionChanged
                    )
                    .help(store.allowsMutations ? "应用选定的 GPU 分配" : store.mutationUnavailableReason)
                }
            }
        }
        .padding(28)
        .frame(width: 680, height: 620)
        .background(VisualEffect(material: .hudWindow, blendingMode: .behindWindow))
        .accessibilityLabel("改派 GPU")
    }

    private func gpuOption(_ gpu: GPURecord) -> some View {
        let selected = selectedGPUIDs.contains(gpu.id)
        let current = lease.gpuIDs.contains(gpu.id)
        return Button {
            resultMessage = nil
            if selected {
                selectedGPUIDs.remove(gpu.id)
            } else if selectedGPUIDs.count < lease.gpuCount {
                selectedGPUIDs.insert(gpu.id)
            }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: selected ? "checkmark.circle.fill" : "circle")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(selected ? DesignTokens.interaction : DesignTokens.mutedInk)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 7) {
                        Text("\(endpointName(for: gpu)) · GPU \(gpu.index)")
                            .font(.system(.callout, design: .monospaced).weight(.semibold))
                            .foregroundStyle(DesignTokens.ink)
                        if current {
                            Text("当前")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(DesignTokens.interaction)
                        }
                    }
                    Text(gpuPresentationLabel(gpu))
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                    Text(workspacePath(for: gpu))
                        .font(.system(.caption2, design: .monospaced).weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                Spacer(minLength: 0)
            }
            .padding(11)
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
        .accessibilityLabel("\(endpointName(for: gpu)) GPU \(gpu.index)")
        .accessibilityValue("\(gpuPresentationLabel(gpu))，工作区 \(workspacePath(for: gpu))，\(selected ? "已选择" : "未选择")")
    }

    private func endpointName(for gpu: GPURecord) -> String {
        store.snapshot.endpoint(id: gpu.endpointID)?.displayName ?? gpu.endpointID
    }

    private func workspacePath(for gpu: GPURecord) -> String {
        store.snapshot.endpoint(id: gpu.endpointID)?.workspacePath ?? "工作区未设置"
    }

    private func applyReassignment() {
        resultMessage = nil
        store.reassignLease(lease, gpuIDs: selectedGPUIDs.sorted()) { success, error in
            resultMessage = success
                ? "分配已更新；请让对应 Agent 按新 CVD 重启任务。"
                : (error ?? "分配更新失败。")
        }
    }
}
