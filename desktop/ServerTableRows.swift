import AppKit
import SwiftUI

/// Slurm node facts share the existing table's columns; no SSH endpoint is invented.
struct SchedulerNodeTableRow: View {
    let openEndpoint: (EndpointRecord) -> Void
    let endpoint: EndpointRecord
    let node: SchedulerNodeRecord
    let gpuName: String
    let tier: EndpointTableLayout.Tier
    let isSnapshotFresh: Bool
    let leases: [LeaseRecord]
    @ObservedObject var store: BrokerStore
    @State private var showDetails = false

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
        }
    }

    var body: some View {
        HStack(spacing: tier.columnSpacing) {
            HStack(spacing: 9) {
                Circle().fill(statusColor).frame(width: 8, height: 8)
                Text(address).font(Typography.command).lineLimit(1).textSelection(.enabled)
                Text(isSnapshotFresh ? node.state : "观测已过期")
                    .font(Typography.annotation).foregroundStyle(statusColor)
                    .lineLimit(1)
                Spacer(minLength: 0)
            }
            .frame(minWidth: EndpointTableLayout.sshLane, maxWidth: .infinity, alignment: .leading)
            if tier.showsAssignment {
                Text(assignment ?? "—").lineLimit(1).foregroundStyle(DesignTokens.mutedInk)
                    .frame(width: tier.assignmentWidth, alignment: .leading)
            }
            if tier.showsGPUModel {
                Text(modelLabel)
                    .font(Typography.identity).lineLimit(1)
                    .frame(width: tier.gpuModelWidth, alignment: .leading)
            }
            Text(isSnapshotFresh && node.schedulable ? "\(node.freeGPUs)/\(node.totalGPUs)" : "—/\(node.totalGPUs)")
                .font(Typography.rowValue).monospacedDigit()
                .frame(width: tier.availabilityWidth, alignment: .trailing)
            TablePressureCell(label: "GPU 利用率", fraction: usage?.gpuUtilizationFraction, width: tier.pressureWidth)
            TablePressureCell(label: "显存占用率", fraction: usage?.gpuMemoryFraction, width: tier.pressureWidth)
            TablePressureCell(label: "CPU 负载", fraction: usage?.cpuUtilizationFraction, width: tier.pressureWidth)
            TablePressureCell(label: "内存占用率", fraction: usage?.memoryFraction, width: tier.pressureWidth)
        }
        .foregroundStyle(DesignTokens.ink)
        .padding(.horizontal, EndpointTableLayout.cardPadding)
        .frame(height: EndpointTableLayout.rowHeight)
        .help("\(node.name) · \(node.state) · 已分配 \(node.allocatedGPUs)/\(node.totalGPUs) GPU。指标来自与普通服务器相同的节点探针。未分配量不保证立即可申请，仍由 Slurm 校验 CPU、内存与调度条件。")
        .contentShape(Rectangle())
        .onTapGesture { showDetails = true }
        .accessibilityAction { showDetails = true }
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
