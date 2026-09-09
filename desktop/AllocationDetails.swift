import AppKit
import SwiftUI

struct AllocationFactsView: View {
    let allocation: ResourceAllocationRecord

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(allocation.locationSummary).font(Typography.identity)
                Spacer()
                Text(allocation.stateLabel).foregroundStyle(DesignTokens.mutedInk)
            }
            Text("分配 \(allocation.reference ?? "待确认") · 截止 \(formattedTimestamp(allocation.expiresAt))")
                .font(Typography.annotation).textSelection(.enabled)
            if let accounting = allocation.accounting { JobAccountingView(accounting: accounting) }
            ForEach(allocation.locations) { location in
                ForEach(location.devices) { device in
                    Text("\(location.node) · \(device.locationLabel) · \(device.id)")
                        .font(Typography.annotation).textSelection(.enabled)
                    HStack(spacing: 20) {
                        Text("GPU 利用率：\(metric(device.gpuUtilizationPercent.map { String(format: "%.0f%%", $0) }))")
                        Text("显存：\(metric(memoryLabel(device)))")
                        Text("计算进程：\(metric(device.runningProcessCount.map(String.init)))")
                    }
                    .font(Typography.annotation).monospacedDigit().textSelection(.enabled)
                }
                if location.devices.isEmpty {
                    Text("\(location.node)：已分配 \(location.gpuCount) 张 GPU，具体设备身份尚未确认。")
                        .font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
                }
            }
            Text(allocation.fresh && allocation.workloadFresh
                 ? "资源采样：\(formattedTimestamp(allocation.workloadObservedAt)) · 当前值"
                 : "资源使用情况尚未取得新鲜采样；位置已知不代表使用率为零。")
                .font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
            if let entry = allocation.entry {
                Text("登录入口 \(entry.loginAddress) · 目录 \(entry.workspacePath)")
                    .font(Typography.annotation).textSelection(.enabled)
                if !entry.argv.isEmpty {
                    Text(entry.verified ? "进入已有分配" : "进入方式尚未通过现场验证")
                        .font(Typography.annotation).foregroundStyle(entry.verified ? DesignTokens.mutedInk : DesignTokens.warning)
                    Text(entry.commandText).font(Typography.command).textSelection(.enabled)
                }
                Text(entry.instructions).font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
            }
            if !allocation.fresh {
                Text("上次确认：\(formattedTimestamp(allocation.confirmedAt))。当前位置与占有状态需要重新确认。")
                    .font(Typography.annotation).foregroundStyle(DesignTokens.warning)
            }
            if let error = allocation.lastError {
                Text(error).font(Typography.annotation).foregroundStyle(DesignTokens.warning)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
    private func metric(_ value: String?) -> String {
        allocation.fresh && allocation.workloadFresh ? (value ?? "—") : "—"
    }

    private func memoryLabel(_ device: AllocatedDeviceRecord) -> String? {
        guard let used = device.memoryUsedMiB, let total = device.totalVRAMMiB else { return nil }
        return String(format: "%.1f / %.1f GiB", Double(used) / 1024, Double(total) / 1024)
    }

}

struct NodeAllocationSheet: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    let node: SchedulerNodeRecord
    @Environment(\.dismiss) private var dismiss

    private var leases: [LeaseRecord] {
        store.snapshot.leases.filter { lease in
            lease.allocations.contains { $0.endpointID == endpoint.id && $0.locations.contains { $0.node == node.name } }
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            SheetTitle(icon: "server.rack", title: node.name, subtitle: endpoint.connectionAddress)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    if let sample = endpoint.resourceNodes.first(where: { $0.id == node.name }) {
                        Text("资源采样：\(formattedTimestamp(sample.observedAt)) · \(sample.fresh ? "当前值" : "未知 / 已过期")")
                            .font(Typography.annotation)
                        if let message = sample.message { Text(message).foregroundStyle(DesignTokens.warning) }
                        ForEach(store.snapshot.gpus(for: endpoint).filter { sample.gpuIDs.contains($0.id) }) { gpu in
                            VStack(alignment: .leading, spacing: 4) {
                                Text("GPU \(gpu.index) · \(gpu.name) · \(gpu.gpuUUID ?? gpu.id)")
                                Text(sample.fresh
                                     ? "利用率 \(gpu.utilization.map { "\($0)%" } ?? "未知") · 显存 \(gpu.memoryUsedMiB.map { "\($0) MiB" } ?? "未知") / \(gpu.totalVRAMMiB) MiB"
                                     : "当前用量未知")
                            }.font(Typography.annotation).textSelection(.enabled)
                            GPUProcessesView(gpu: gpu)
                        }
                    } else {
                        Text("尚未取得节点资源采样").foregroundStyle(DesignTokens.mutedInk)
                    }
                    Divider()
                    ForEach(leases) { lease in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack {
                                Text("\(lease.projectID) · \(lease.taskReference ?? "未标注任务")").font(Typography.label)
                                Spacer()
                                Button("归还分配") {
                                    guard confirmLeaseRelease(lease) else { return }
                                    store.releaseLease(lease) { _, _ in }
                                }
                                .disabled(!store.allowsMutations)
                            }
                            ForEach(lease.allocations.filter { $0.endpointID == endpoint.id && $0.locations.contains { $0.node == node.name } }) { allocation in
                                AllocationFactsView(allocation: allocation)
                            }
                        }
                        Divider()
                    }
                    ForEach(endpoint.externalAllocations.filter { $0.locations.contains { $0.node == node.name } }) { allocation in
                        VStack(alignment: .leading, spacing: 5) {
                            Text("\(allocation.name ?? "外部作业") · \(allocation.reference) · \(allocation.owner ?? "本人账号")").font(Typography.identity)
                            if let accounting = allocation.accounting { JobAccountingView(accounting: accounting) }
                            Text("最近观测：\(formattedTimestamp(allocation.observedAt))")
                                .font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
                            Text("资源使用情况见上方节点观测；作业分配与节点实际用量分别展示。")
                                .font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
                            Text("由外部工具管理；ServerPilot 不会自动接管或归还。")
                                .font(Typography.annotation)
                        }
                    }
                    if leases.isEmpty && endpoint.externalAllocations.allSatisfy({ !$0.locations.contains { $0.node == node.name } }) {
                        Text("此节点没有已关联的 ServerPilot 任务或已观测到的同账号外部作业。")
                            .foregroundStyle(DesignTokens.mutedInk)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            HStack { Spacer(); Button("完成") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(24).frame(width: 700, height: 480)
    }
}


struct JobAccountingView: View {
    let accounting: JobAccountingRecord
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("作业累计 CPU：\(accounting.cpuSeconds.map { String(format: "%.0f 核秒", $0) } ?? "未知") · RSS：\(accounting.rssMiB.map { String(format: "%.1f GiB", $0 / 1024) } ?? "未知")")
            Text("Slurm 统计 · \(formattedTimestamp(accounting.observedAt))；RSS 与节点物理内存用量口径不同。")
        }.font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk).textSelection(.enabled)
    }
}

struct GPUProcessesView: View {
    let gpu: GPURecord
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("计算进程（最近观测）").font(Typography.label)
            ForEach(gpu.processes) { process in
                Text("PID \(process.pid) · \(process.username ?? "用户未知") · \(process.executable) · \(process.memoryMiB.map { "\($0) MiB" } ?? "显存未知")")
                    .font(Typography.annotation).textSelection(.enabled)
            }
            if gpu.processes.isEmpty {
                Text("未记录计算进程；采集失败或过期时不能据此判断空闲。")
                    .font(Typography.annotation).foregroundStyle(DesignTokens.mutedInk)
            }
        }
    }
}
