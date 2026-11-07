import AppKit
import SwiftUI

/// Slurm node facts share the existing table's columns; no SSH endpoint is invented.
struct SchedulerNodeTableRow: View {
    let openEndpoint: (EndpointRecord) -> Void
    let endpoint: EndpointRecord
    let node: SchedulerNodeRecord
    let gpuName: String
    let tier: EndpointTableLayout.Tier
    let columns: Set<EndpointColumn>
    let isSnapshotFresh: Bool
    let leases: [LeaseRecord]
    @ObservedObject var store: BrokerStore
    @State private var showDetails = false
    @State private var hovering = false

    private var assignment: String? {
        let tasks = leases.filter { $0.allocations.contains { $0.locations.contains { $0.node == node.name } } }
            .map { "\($0.projectID) · \($0.taskReference ?? "未标注任务")" }
        if !tasks.isEmpty { return tasks.joined(separator: "；") }
        let external = endpoint.externalAllocations.filter { $0.locations.contains { $0.node == node.name } }
        return external.isEmpty ? nil : "外部作业 " + external.map(\.reference).joined(separator: "、")
    }

    private var usage: ResourceNodeRecord? {
        isSnapshotFresh ? endpoint.resourceNodes.first(where: { $0.id == node.name }) : nil
    }

    /// A node reports one reading for all its cards, and only once its compute
    /// node can be observed; until then an allocation is a scheduling fact.
    private func nodeCardLoad(allocated: Bool) -> CardLoad {
        guard isSnapshotFresh else { return .stale }
        return CardLoad(
            state: allocated ? "RUNNING_MANAGED" : "AVAILABLE",
            utilization: usage?.gpuUtilizationFraction,
            memory: usage?.gpuMemoryFraction
        )
    }

    /// What the row can still show once the node stops answering, greyed.
    private var lastUsage: ResourceNodeUsage? {
        guard usage?.fresh != true else { return nil }
        return endpoint.resourceNodes.first(where: { $0.id == node.name })?.lastUsage
    }

    private var peakTemperature: Int? {
        guard let usage, usage.fresh else { return nil }
        return usage.gpuIDs.compactMap { store.snapshot.gpu(id: $0)?.temperature }.max()
    }

    private var statusColor: Color {
        if !isSnapshotFresh || !node.schedulable { return DesignTokens.warning }
        return node.freeGPUs > 0 ? DesignTokens.success : DesignTokens.mutedInk
    }

    private var address: String { "\(endpoint.connectionAddress) / \(node.name)" }
    private var modelLabel: String { "\(gpuName) × \(node.totalGPUs)" }

    func sortValue(for field: EndpointSort) -> ResourceTableSortValue? {
        switch field {
        case .id: return .text(address)
        case .gpuModel: return .text(modelLabel)
        case .availableGPU:
            return isSnapshotFresh && node.schedulable ? .number(Double(node.freeGPUs)) : nil
        case .attention: return .number(!isSnapshotFresh ? 3 : node.schedulable ? 0 : 1)
        case .assignment: return assignment.map(ResourceTableSortValue.text)
        case .gpuUtilization: return usage?.gpuUtilizationFraction.map(ResourceTableSortValue.number)
        case .gpuMemory: return usage?.gpuMemoryFraction.map(ResourceTableSortValue.number)
        case .cpuLoad: return usage?.cpuUtilizationFraction.map(ResourceTableSortValue.number)
        case .memory: return usage?.memoryFraction.map(ResourceTableSortValue.number)
        case .temperature: return peakTemperature.map { .number(Double($0)) }
        }
    }

    private var stateWord: String {
        isSnapshotFresh ? schedulerNodeStateLabel(node.state) : "观测已过期"
    }

    var body: some View {
        Button { showDetails = true } label: {
        HStack(spacing: tier.columnSpacing) {
            VStack(alignment: .leading, spacing: 2) {
                Text(address)
                    .font(Typography.rowPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                HStack(spacing: 5) {
                    Circle()
                        .fill(statusColor)
                        .frame(width: 7, height: 7)
                        .accessibilityHidden(true)
                    Text(stateWord)
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
            }
            .frame(minWidth: EndpointTableLayout.addressLane, maxWidth: .infinity, alignment: .leading)
            if tier.showsAssignment, columns.contains(.assignment) {
                Text(assignment ?? "—")
                    .font(Typography.rowPrimary)
                    .lineLimit(1)
                    .foregroundStyle(assignment == nil ? DesignTokens.mutedInk : DesignTokens.ink)
                    .frame(width: tier.assignmentWidth, alignment: .leading)
            }
            if tier.showsGPUModel, columns.contains(.cards) {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 3) {
                        ForEach(0..<node.totalGPUs, id: \.self) { index in
                            CardPip(load: nodeCardLoad(allocated: index < node.allocatedGPUs), size: 11)
                        }
                    }
                    .fixedSize()
                    .accessibilityHidden(true)
                    Text(modelLabel)
                        .font(Typography.annotation)
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                .frame(width: tier.gpuModelWidth, alignment: .leading)
            }
            HStack(alignment: .firstTextBaseline, spacing: 1) {
                Spacer(minLength: 0)
                Text(isSnapshotFresh && node.schedulable ? "\(node.freeGPUs)" : "—")
                    .font(Typography.rowValue.weight(.semibold))
                    .foregroundStyle(node.freeGPUs > 0 ? DesignTokens.ink : DesignTokens.mutedInk)
                Text("/\(node.totalGPUs)")
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
            .frame(width: tier.availabilityWidth, alignment: .trailing)
            if columns.contains(.gpuUtilization) {
                TablePressureCell(label: "GPU 利用率", metric: .gpuUtilization, fraction: usage?.gpuUtilizationFraction ?? lastUsage?.gpuUtilization, muted: usage?.gpuUtilizationFraction == nil, width: tier.pressureWidth)
            }
            if columns.contains(.gpuMemory) {
                TablePressureCell(label: "显存占用率", metric: .gpuMemory, fraction: usage?.gpuMemoryFraction ?? lastUsage?.gpuMemory, muted: usage?.gpuMemoryFraction == nil, width: tier.pressureWidth)
            }
            if columns.contains(.cpu) {
                TablePressureCell(label: "CPU 负载", metric: .cpu, fraction: usage?.cpuUtilizationFraction ?? lastUsage?.cpu, muted: usage?.cpuUtilizationFraction == nil, width: tier.pressureWidth)
            }
            if columns.contains(.memory) {
                TablePressureCell(label: "内存占用率", metric: .memory, fraction: usage?.memoryFraction ?? lastUsage?.memory, muted: usage?.memoryFraction == nil, width: tier.pressureWidth)
            }
            if columns.contains(.temperature) {
                TableTemperatureCell(celsius: peakTemperature, width: tier.temperatureWidth)
            }
        }
        .foregroundStyle(DesignTokens.ink)
        .padding(.horizontal, EndpointTableLayout.cardPadding)
        .frame(height: EndpointTableLayout.rowHeight)
        .background(DesignTokens.ink.opacity(hovering ? DesignTokens.Alpha.hairline : 0))
        .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help("\(node.name) · \(stateWord) · 已分配 \(node.allocatedGPUs)/\(node.totalGPUs) GPU。指标来自与普通服务器相同的节点探针。未分配量不保证立即可申请，仍由 Slurm 校验 CPU、内存与调度条件。")
        .sheet(isPresented: $showDetails) { NodeAllocationSheet(store: store, endpoint: endpoint, node: node) { openEndpoint(endpoint) } }
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("scheduler-node-\(node.name)")
    }
}


/// Keep group scrollbars visible; the table header reserves the same two native gutters.
struct TableScrollStyle: NSViewRepresentable {
    var hideWhenUnused = false

    func makeNSView(context: Context) -> TableScrollAnchor { TableScrollAnchor() }
    func updateNSView(_ view: TableScrollAnchor, context: Context) {
        view.hideWhenUnused = hideWhenUnused
        view.applyStyle()
    }
}

final class TableScrollAnchor: NSView {
    var hideWhenUnused = false

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyStyle()
    }

    func applyStyle() {
        DispatchQueue.main.async { [weak self] in
            guard let scroll = self?.enclosingScrollView else { return }
            scroll.scrollerStyle = .legacy
            scroll.autohidesScrollers = self?.hideWhenUnused ?? false
        }
    }
}
