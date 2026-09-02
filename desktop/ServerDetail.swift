import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct ServerDetailSheet: View {
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

    /// Leases holding at least one of this server's cards. Scope only: it says
    /// nothing about whether any of them may be cleared.
    private var endpointLeases: [LeaseRecord] {
        store.snapshot.leases.filter { lease in
            lease.gpuIDs.contains { gpuID in gpus.contains(where: { $0.id == gpuID }) }
        }
    }

    private var conflictedLeases: [LeaseRecord] {
        endpointLeases.filter { lease in
            lease.gpuIDs.contains { gpuID in
                gpus.first(where: { $0.id == gpuID })?.state == "CONFLICT"
            }
        }
    }

    /// Leases on this server a person has to look at: either the broker says
    /// the lease can be cleared here, or its cards are in an attribution
    /// conflict somebody has to settle.
    ///
    /// Releasability is never inferred here. Reading it off GPU states is what
    /// invited a person to clear a healthy eight-card claim that merely sat
    /// between two batches of shards -- "no process is running" is not the
    /// same fact as "the holder is finished", and only the broker holds the
    /// second one.
    private var reclaimableLeases: [LeaseRecord] {
        endpointLeases.filter { lease in
            lease.manualRelease.allowed || conflictedLeases.contains(where: { $0.id == lease.id })
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
                                // Count the lease's own cards. Filtering by GPU
                                // state made an unreachable server -- whose
                                // cards all read UNKNOWN_STALE -- announce
                                // "有 0 张 GPU", next to a live button.
                                let gpuCount = lease.gpuIDs.count
                                // The broker waives the emptiness proof only
                                // when the machine has stopped answering, so
                                // that case may not borrow wording that claims
                                // a collection which did not happen.
                                let unreachable = endpoint.monitorStatus != "ONLINE"
                                let task = lease.taskReference ?? lease.purpose ?? "未命名任务"
                                // The broker decides whether this may be
                                // cleared. While it says no there is nothing to
                                // press: the click would come back with the
                                // same refusal, worded the same way.
                                let clearAction: (() -> Void)? = lease.manualRelease.allowed
                                    ? {
                                        guard confirmEmptyLeaseCleanup(lease, conflict: conflict) else { return }
                                        store.clearEmptyConflictedLease(
                                            endpointID: endpoint.id,
                                            leaseID: lease.id
                                        ) { _, _ in }
                                    }
                                    : nil
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
                                        : (unreachable
                                            ? "这台服务器已停止应答，无法证明这 \(gpuCount) 张 GPU 上是否还有进程；确认任务确实结束后可结清这条记录。"
                                            : "有 \(gpuCount) 张 GPU 仍被租约占用，但当前采集没有观察到进程；可确认后释放。"),
                                    actionTitle: clearAction == nil
                                        ? nil
                                        : (isMutating
                                            ? "处理中"
                                            : (conflict
                                                ? "任务结束后清理记录"
                                                : (unreachable ? "结清失联服务器的记录" : "释放空闲占用"))),
                                    action: clearAction
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
                    if occupancyActionStarts || confirmKeepaliveEnd(activeGPUCount: endpoint.keepalive.activeGPUCount) {
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
            : "结束整台服务器的占卡：正在占卡的 GPU 会一起停止，不会停止正在运行的任务。腾卡不必关它——Agent 申请时会自动让出所需的卡"
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


struct GPUDetailSheet: View {
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


struct DetailCallout: View {
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

