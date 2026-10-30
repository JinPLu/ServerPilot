import SwiftUI

enum GPUStatusSection: String, CaseIterable, Identifiable {
    case available
    case keepalive
    case busy
    case error

    var id: String { rawValue }

    var label: String {
        switch self {
        case .available: return "空闲"
        case .keepalive: return "占卡中"
        case .busy: return "使用中"
        case .error: return "需关注"
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


struct ServerGPUMemoryStatusGrid: View {
    let gpus: [GPURecord]
    @State private var detailGPU: StableRecordSelection?

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
                    Button {
                        detailGPU = StableRecordSelection(id: gpu.id)
                    } label: {
                        GPUMemoryStatusRow(gpu: gpu, status: section(for: gpu))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("查看进程和持有者")
                }
            }
        }
        .sheet(item: $detailGPU) { selection in
            if let gpu = gpus.first(where: { $0.id == selection.id }) {
                GPUDetailSheet(gpu: gpu)
            }
        }
        // Each card already carries its own label and value; collapsing the
        // grid hid all of them behind one summary string.
        .accessibilityElement(children: .contain)
        .help("每张卡片代表一张 GPU；环图表示当前显存占用，状态标签表示可用状态。\n\(accessibilityValue)")
    }
}


struct GPUStatusCountChip: View {
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


struct GPUMemoryStatusRow: View {
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
                        Label(CardVocabulary.label(forState: gpu.state), systemImage: status.icon)
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
            GPUProcessesView(gpu: gpu)
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
