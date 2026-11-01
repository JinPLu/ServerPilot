import SwiftUI

/// Who is using what, and whether anything is at risk.  One card per task: a
/// project of one Agent holding everything is the usual case, so grouping by
/// project hid exactly the thing a person came to read.
struct ResourceUsageDashboard: View {
    @ObservedObject var store: BrokerStore
    let claimGPU: () -> Void

    private var ordered: [LeaseRecord] {
        store.snapshot.leases.sorted { ($0.issuedAt ?? "") > ($1.issuedAt ?? "") }
    }

    /// A task holding cards with nothing running is what a person can act on.
    private var quiet: [LeaseRecord] { ordered.filter { $0.runtimeState != "RUNNING" } }
    private var running: [LeaseRecord] { ordered.filter { $0.runtimeState == "RUNNING" } }

    var body: some View {
        if ordered.isEmpty {
            VStack(spacing: 12) {
                ContentUnavailableView("当前没有资源分配。", systemImage: "square.stack.3d.up.slash")
                    .frame(maxHeight: 180)
                Button("申请 GPU", action: claimGPU)
                    .buttonStyle(PrimaryActionButtonStyle())
                    .disabled(!store.allowsMutations)
                    .help(store.allowsMutations ? "申请空闲 GPU" : store.mutationUnavailableReason)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("\(ordered.count) 个任务 · 占用 \(ordered.reduce(0) { $0 + $1.gpuCount }) 张卡")
                        .font(Typography.identity)
                        .monospacedDigit()
                        .foregroundStyle(DesignTokens.mutedInk)
                    section("需要留意", leases: quiet)
                    section("运行中", leases: running)
                }
                .padding(.horizontal, 24)
                .padding(.top, 2)
                .padding(.bottom, 20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            // The sidebar item and the page title already name this page; a
            // label here would collapse it into one leaf.
            .accessibilityElement(children: .contain)
        }
    }

    @ViewBuilder
    private func section(_ title: String, leases: [LeaseRecord]) -> some View {
        if !leases.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text(title)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(DesignTokens.ink)
                    .accessibilityAddTraits(.isHeader)
                ForEach(leases) { lease in
                    TaskCard(store: store, lease: lease)
                }
            }
        }
    }
}

private struct TaskCard: View {
    @ObservedObject var store: BrokerStore
    @State private var showsReassignment = false
    let lease: LeaseRecord

    private var isRunning: Bool { lease.runtimeState == "RUNNING" }
    private var title: String { lease.taskReference ?? lease.purpose ?? "未命名任务" }

    private var cards: [GPURecord] { lease.gpuIDs.compactMap { store.snapshot.gpu(id: $0) } }

    /// One line per server: a lease of eight cards is one fact, not eight.
    private var servers: [(id: String, address: String, gpus: [GPURecord])] {
        Dictionary(grouping: cards, by: \.endpointID).keys.sorted().map { endpointID in
            let gpus = cards.filter { $0.endpointID == endpointID }.sorted { $0.index < $1.index }
            let address = store.snapshot.endpoint(id: endpointID)?.connectionAddress ?? endpointID
            return (endpointID, address, gpus)
        }
    }

    /// The server's line, not the task's: nothing measures a task's own share.
    private var serverTrend: UtilizationSeries? {
        servers.lazy.compactMap { store.utilizationTrends.servers[$0.id] }.first { $0.hasData }
    }

    private var stateWord: String {
        if isRunning { return CardVocabulary.label(forState: "RUNNING_MANAGED") }
        return lease.stateLabel == "使用中" ? CardVocabulary.label(forState: "HELD") : lease.stateLabel
    }

    private var stats: [(value: String, label: String)] {
        var answer = [(value: "\(lease.gpuCount)", label: "张卡")]
        if let held = secondsSince(lease.issuedAt) {
            answer.append((elapsedLabel(held), isRunning ? "已运行" : "已持有"))
        }
        if !isRunning {
            if let quiet = secondsSince(lease.lastProcessObservedAt) {
                answer.append((elapsedLabel(quiet), "没有进程"))
            } else {
                answer.append(("尚未", "看到进程"))
            }
        } else if let declared = lease.declaredDurationSeconds {
            answer.append((durationLabel(declared), "声明时长"))
        } else {
            answer.append(("未声明", "时长"))
        }
        return answer
    }

    var body: some View {
        HomeCard(padding: 16) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .top, spacing: 22) {
                    VStack(alignment: .leading, spacing: 10) {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(alignment: .firstTextBaseline, spacing: 10) {
                                Text(title)
                                    .font(.title3.weight(.semibold))
                                    .foregroundStyle(DesignTokens.ink)
                                    .lineLimit(2)
                                    .help(title)
                                Text(stateWord)
                                    .font(Typography.annotation)
                                    .foregroundStyle(isRunning ? DesignTokens.interaction : DesignTokens.warning)
                                    .fixedSize()
                            }
                            Text("\(lease.holderLabel) · \(lease.projectID)")
                                .font(Typography.annotation)
                                .foregroundStyle(DesignTokens.mutedInk)
                                .lineLimit(1)
                        }
                        HStack(spacing: 22) {
                            ForEach(stats, id: \.label) { stat in
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(stat.value)
                                        .font(Typography.pageTitle)
                                        .monospacedDigit()
                                        .foregroundStyle(DesignTokens.ink)
                                    Text(stat.label)
                                        .font(Typography.annotation)
                                        .foregroundStyle(DesignTokens.mutedInk)
                                }
                                .accessibilityElement(children: .combine)
                            }
                        }
                        ForEach(servers, id: \.id) { server in
                            HStack(spacing: 10) {
                                Text(server.address)
                                    .font(Typography.annotation)
                                    .monospacedDigit()
                                    .foregroundStyle(DesignTokens.ink)
                                    .lineLimit(1)
                                CardSquares(gpus: server.gpus, size: 11)
                                    .fixedSize()
                            }
                        }
                    }
                    Spacer(minLength: 0)
                    if let serverTrend {
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(alignment: .firstTextBaseline) {
                                Text("所在服务器 · 近 1 小时")
                                    .font(Typography.annotation)
                                    .foregroundStyle(DesignTokens.mutedInk)
                                Spacer(minLength: 6)
                                Text(percentageLabel(serverTrend.latest))
                                    .font(Typography.identity)
                                    .monospacedDigit()
                                    .foregroundStyle(DesignTokens.ink)
                            }
                            Sparkline(
                                points: serverTrend.points,
                                color: isRunning ? DesignTokens.interaction : DesignTokens.quietBar,
                                lineWidth: 2
                            )
                            .frame(height: 52)
                        }
                        .frame(width: 220)
                        .accessibilityElement(children: .ignore)
                        .accessibilityLabel("所在服务器近 1 小时的 GPU 利用率")
                        .accessibilityValue(percentageLabel(serverTrend.latest))
                    }
                    VStack(spacing: 6) {
                        Button(store.releasingLeaseIDs.contains(lease.id) ? "归还中" : "归还", action: release)
                            .buttonStyle(SecondaryActionButtonStyle())
                            .disabled(!store.allowsMutations || store.releasingLeaseIDs.contains(lease.id))
                            .help(store.allowsMutations ? (lease.allocations.contains { $0.releaseGranularity == "allocation" } ? "归还整个分配，提供方可能终止其中的程序" : "归还 GPU；不会停止任务") : store.mutationUnavailableReason)
                        Button("改派") { showsReassignment = true }
                            .buttonStyle(SecondaryActionButtonStyle())
                            .disabled(
                                !store.allowsMutations
                                    || store.reassigningLeaseIDs.contains(lease.id)
                                    || lease.gpuIDs.isEmpty
                            )
                            .help(store.allowsMutations ? "选择同等数量的目标 GPU" : store.mutationUnavailableReason)
                    }
                    .fixedSize()
                }
                ForEach(lease.allocations.filter { $0.releaseGranularity == "allocation" }) { allocation in
                    AllocationFactsView(allocation: allocation)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .sheet(isPresented: $showsReassignment) {
            LeaseReassignmentSheet(store: store, lease: lease)
        }
    }

    private func release() {
        guard confirmLeaseRelease(lease) else { return }
        store.releaseLease(lease) { _, _ in }
    }

    private func secondsSince(_ timestamp: String?) -> Int? {
        guard let timestamp else { return nil }
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard let date = parser.date(from: timestamp) ?? ISO8601DateFormatter().date(from: timestamp) else { return nil }
        return max(0, Int(Date().timeIntervalSince(date)))
    }
}
