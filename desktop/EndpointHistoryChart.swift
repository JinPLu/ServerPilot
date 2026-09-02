import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct EndpointTelemetryHistoryPanel: View {
    @ObservedObject var store: BrokerStore
    let endpoint: EndpointRecord
    @State private var range: EndpointTelemetryRange = .oneHour

    private var history: EndpointTelemetryHistory? {
        store.endpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .firstTextBaseline) {
                Text("历史")
                    .font(.body.weight(.semibold))
                Spacer()
                Picker("时间范围", selection: $range) {
                    Text("1h").tag(EndpointTelemetryRange.oneHour)
                    Text("6h").tag(EndpointTelemetryRange.sixHours)
                    Text("24h").tag(EndpointTelemetryRange.twentyFourHours)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 126)
                .accessibilityLabel("资源历史时间范围")
            }

            if !store.supportsEndpointTelemetryHistory {
                DetailCallout(
                    icon: "chart.line.uptrend.xyaxis",
                    color: DesignTokens.warning,
                    message: "当前服务不支持历史数据。"
                )
            } else if store.endpointTelemetryHistoryLoading.contains(endpoint.id) {
                Label("正在载入 \(range.rawValue)", systemImage: "arrow.clockwise")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 76, alignment: .leading)
                    .padding(.horizontal, 12)
                    .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            } else if let error = store.endpointTelemetryHistoryErrors[endpoint.id] {
                DetailCallout(icon: "exclamationmark.triangle.fill", color: DesignTokens.danger, message: error)
            } else if let history, !history.samples.isEmpty {
                if history.endpointID == endpoint.id {
                    EndpointTelemetryHistoryChart(history: history).equatable()
                } else {
                    DetailCallout(
                        icon: "exclamationmark.shield.fill",
                        color: DesignTokens.danger,
                        message: "历史数据校验失败。"
                    )
                }
            } else {
                DetailCallout(icon: "clock", color: DesignTokens.mutedInk, message: "所选时段暂无数据。")
            }
        }
        // History persists at a 60-second cadence. Refresh only while this
        // selected detail is visible, so the overview never fans out into one
        // request per endpoint and hidden charts cannot become a render driver.
        .task(id: "\(endpoint.id):\(range.rawValue)") {
            requestIfSupported()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(60))
                guard !Task.isCancelled else { return }
                requestIfSupported()
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("资源历史")
    }

    private func requestIfSupported() {
        guard store.supportsEndpointTelemetryHistory else { return }
        store.requestEndpointTelemetryHistory(endpointID: endpoint.id, range: range)
    }
}


private struct EndpointTelemetryHistoryChart: View, Equatable {
    let history: EndpointTelemetryHistory

    static func == (lhs: Self, rhs: Self) -> Bool { lhs.history == rhs.history }

    var body: some View {
        let prepared = EndpointTelemetryPreparedHistory(history: history)
        VStack(alignment: .leading, spacing: 8) {
            EndpointTelemetryHistoryContext(prepared: prepared)
            if prepared.hostSamples.isEmpty {
                Label(
                    "所选时段暂无数据。",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption2.weight(.semibold))
                .foregroundStyle(DesignTokens.warning)
                .frame(maxWidth: .infinity, minHeight: 112, alignment: .leading)
            } else {
                LazyVGrid(
                    columns: [
                        GridItem(.flexible(minimum: 300), spacing: 12),
                        GridItem(.flexible(minimum: 300), spacing: 12),
                    ],
                    alignment: .leading,
                    spacing: 12
                ) {
                    EndpointTelemetryMetricChart(
                        title: "CPU 使用率",
                        subtitle: "",
                        series: prepared.cpuSeries,
                        hoverItems: prepared.cpuHoverItems,
                        emptyMessage: "无 CPU 历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "内存占用率",
                        subtitle: "",
                        series: prepared.memorySeries,
                        hoverItems: prepared.memoryHoverItems,
                        emptyMessage: "无内存历史数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "GPU 利用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuUtilizationSeries,
                        hoverItems: prepared.gpuUtilizationHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无 GPU 利用率数据"
                    )
                    EndpointTelemetryMetricChart(
                        title: "显存占用率",
                        subtitle: prepared.gpuSeries.isEmpty ? "无 GPU" : "",
                        series: prepared.gpuMemorySeries,
                        hoverItems: prepared.gpuMemoryHoverItems,
                        emptyMessage: prepared.gpuSeries.isEmpty ? "无 GPU" : "无显存历史数据"
                    )
                }
                if prepared.gpuUtilizationSeries.count > 1 {
                    EndpointTelemetryGPULegend(series: prepared.gpuUtilizationSeries)
                }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("端点资源历史")
        .accessibilityValue(prepared.accessibilityValue)
    }
}


private struct EndpointTelemetryHistoryContext: View {
    let prepared: EndpointTelemetryPreparedHistory

    var body: some View {
        HStack(spacing: 8) {
            Label(prepared.lastObservationLabel, systemImage: "clock")
            if let warning = prepared.visualWarningLabel {
                Label(warning, systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(DesignTokens.warning)
            }
            Spacer(minLength: 0)
        }
        .font(Typography.annotation.weight(.semibold))
        .foregroundStyle(DesignTokens.mutedInk)
        .lineLimit(1)
    }
}


private struct EndpointTelemetryMetricChart: View, Equatable {
    let title: String
    let subtitle: String
    let series: [EndpointTelemetryLineSeries]
    let hoverItems: [EndpointTelemetryHoverItem]
    let emptyMessage: String

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.title == rhs.title
            && lhs.subtitle == rhs.subtitle
            && lhs.series == rhs.series
            && lhs.hoverItems == rhs.hoverItems
            && lhs.emptyMessage == rhs.emptyMessage
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Text(title).font(.callout.weight(.semibold))
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption2.weight(.medium))
                        .foregroundStyle(DesignTokens.mutedInk)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }

            if series.allSatisfy({ $0.points.isEmpty }) {
                Label(emptyMessage, systemImage: "chart.line.flattrend.xyaxis")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
            } else {
#if canImport(Charts)
                chart
#else
                Text("当前系统没有 Swift Charts，使用文本降级。")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
                    .frame(maxWidth: .infinity, minHeight: 178, alignment: .leading)
#endif
            }
        }
        .padding(7)
        .background(DesignTokens.glassSmoke, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 0.8))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
    }

    private var accessibilitySummary: String {
        let summaries = series.compactMap { line -> String? in
            guard
                let latest = line.points.max(by: { $0.timestamp < $1.timestamp }),
                let minimum = line.points.map(\.value).min(),
                let maximum = line.points.map(\.value).max()
            else { return nil }
            return "\(line.label)：最新 \(historyPercent(latest.value))，最低 \(historyPercent(minimum))，最高 \(historyPercent(maximum))"
        }
        return summaries.isEmpty ? emptyMessage : summaries.joined(separator: "；")
    }

#if canImport(Charts)
    private var chart: some View {
        Chart {
            ForEach(series) { line in
                ForEach(line.points) { point in
                    LineMark(
                        x: .value("观测时间", point.timestamp),
                        y: .value("使用率", point.value),
                        series: .value("连续片段", point.segmentID)
                    )
                    .foregroundStyle(line.color)
                    .interpolationMethod(.linear)
                    .lineStyle(StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round))
                }
            }
        }
        .chartYScale(domain: 0...1)
        .chartXAxis {
            AxisMarks(values: .automatic(desiredCount: 3)) { _ in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel(format: .dateTime.hour().minute())
            }
        }
        .chartYAxis {
            AxisMarks(position: .leading, values: [0, 0.5, 1]) { value in
                AxisGridLine(stroke: StrokeStyle(lineWidth: 0.5)).foregroundStyle(DesignTokens.surfaceStroke)
                AxisTick()
                AxisValueLabel {
                    if let fraction = value.as(Double.self) { Text(historyPercent(fraction)) }
                }
            }
        }
        .chartLegend(.hidden)
        .chartOverlay { proxy in
            EndpointTelemetryChartHoverOverlay(proxy: proxy, items: hoverItems)
        }
        .frame(height: 160)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(title)
        .accessibilityValue(accessibilitySummary)
        .accessibilityHint("将指针悬停在图表上可检查最近的观测样本。")
    }
#endif
}


private struct EndpointTelemetryGPULegend: View {
    let series: [EndpointTelemetryLineSeries]

    private static let visibleLimit = 12

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 86), spacing: 8)], alignment: .leading, spacing: 6) {
                ForEach(Array(series.prefix(Self.visibleLimit))) { line in
                    HStack(spacing: 4) {
                        Capsule().fill(line.color).frame(width: 12, height: 3)
                        Text(line.label).lineLimit(1)
                    }
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(DesignTokens.mutedInk)
                }
            }
            if series.count > Self.visibleLimit {
                Text("另 \(series.count - Self.visibleLimit) 张未列出")
                    .font(.caption2.weight(.medium))
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
        .accessibilityHidden(true)
    }
}

#if canImport(Charts)

private struct EndpointTelemetryChartHoverOverlay: View {
    let proxy: ChartProxy
    let items: [EndpointTelemetryHoverItem]
    @State private var selectedIndex: Int?

    var body: some View {
        GeometryReader { geometry in
            if let anchor = proxy.plotFrame {
                let frame = geometry[anchor]
                Rectangle()
                    .fill(.clear)
                    .contentShape(Rectangle())
                    .onContinuousHover { phase in
                        switch phase {
                        case .active(let location):
                            let x = location.x - frame.origin.x
                            guard x >= 0, x <= frame.width, let date = proxy.value(atX: x, as: Date.self) else { return }
                            updateSelection(for: date)
                        case .ended:
                            updateSelection(to: nil)
                        }
                    }
                    .overlay {
                        if let selected = selectedItem, let position = proxy.position(forX: selected.timestamp) {
                            let x = frame.minX + position
                            let cardWidth = min(
                                EndpointTelemetryHoverCard.preferredWidth(for: selected.entries.count),
                                max(184, geometry.size.width - 8)
                            )
                            let halfCardWidth = cardWidth / 2
                            Path { path in
                                path.move(to: CGPoint(x: x, y: frame.minY))
                                path.addLine(to: CGPoint(x: x, y: frame.maxY))
                            }
                            .stroke(DesignTokens.ink.opacity(DesignTokens.Alpha.strong), style: StrokeStyle(lineWidth: 1, dash: [2, 2]))
                            .allowsHitTesting(false)
                            EndpointTelemetryHoverCard(item: selected, width: cardWidth)
                                .position(
                                    x: min(max(4 + halfCardWidth, x), geometry.size.width - 4 - halfCardWidth),
                                    y: frame.midY
                                )
                                .allowsHitTesting(false)
                        }
                    }
            } else {
                Color.clear
            }
        }
    }

    private var selectedItem: EndpointTelemetryHoverItem? {
        guard let selectedIndex, items.indices.contains(selectedIndex) else { return nil }
        return items[selectedIndex]
    }

    private func updateSelection(for date: Date) {
        guard !items.isEmpty else { return }
        var lowerBound = 0
        var upperBound = items.count
        while lowerBound < upperBound {
            let middle = (lowerBound + upperBound) / 2
            if items[middle].timestamp < date { lowerBound = middle + 1 } else { upperBound = middle }
        }
        let candidate: Int
        if lowerBound == 0 {
            candidate = 0
        } else if lowerBound == items.count {
            candidate = items.count - 1
        } else {
            let earlier = items[lowerBound - 1]
            let later = items[lowerBound]
            candidate = abs(earlier.timestamp.timeIntervalSince(date)) <= abs(later.timestamp.timeIntervalSince(date))
                ? lowerBound - 1 : lowerBound
        }
        updateSelection(to: candidate)
    }

    private func updateSelection(to index: Int?) {
        guard selectedIndex != index else { return }
        var transaction = Transaction()
        transaction.animation = nil
        withTransaction(transaction) { selectedIndex = index }
    }
}


private struct EndpointTelemetryHoverCard: View {
    let item: EndpointTelemetryHoverItem
    let width: CGFloat

    static func preferredWidth(for entryCount: Int) -> CGFloat {
        entryCount > 4 ? 316 : 184
    }

    private var columns: [GridItem] {
        Array(
            repeating: GridItem(.flexible(minimum: 116), spacing: 8, alignment: .leading),
            count: item.entries.count > 4 ? 2 : 1
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(historyDateTime(item.timestamp))
                .font(.system(.caption2, design: .monospaced).weight(.bold))
                .foregroundStyle(DesignTokens.ink)
            ScrollView(.vertical) {
                LazyVGrid(columns: columns, alignment: .leading, spacing: 4) {
                    ForEach(item.entries) { entry in
                        HStack(spacing: 5) {
                            Capsule()
                                .fill(entry.color)
                                .frame(width: 12, height: 3)
                            Text(entry.label)
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            Text(entry.value)
                                .fontWeight(.bold)
                        }
                        .font(.system(.caption2, design: .monospaced).weight(.medium))
                        .foregroundStyle(DesignTokens.ink)
                    }
                }
            }
            .scrollIndicators(.hidden)
            .frame(maxHeight: 146)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .frame(width: width, alignment: .leading)
        .frame(maxHeight: 176)
        .background(DesignTokens.surface, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous).stroke(DesignTokens.surfaceStroke, lineWidth: 1))
        .shadow(color: .black.opacity(DesignTokens.Alpha.fill), radius: 5, y: 2)
    }
}
#endif


private struct EndpointTelemetryPreparedHistory: Equatable {
    let range: EndpointTelemetryRange
    let hostSamples: [EndpointTelemetryPreparedHostSample]
    let gpuSeries: [EndpointTelemetryPreparedGPUSeries]
    let cpuSeries: [EndpointTelemetryLineSeries]
    let memorySeries: [EndpointTelemetryLineSeries]
    let gpuUtilizationSeries: [EndpointTelemetryLineSeries]
    let gpuMemorySeries: [EndpointTelemetryLineSeries]
    let cpuHoverItems: [EndpointTelemetryHoverItem]
    let memoryHoverItems: [EndpointTelemetryHoverItem]
    let gpuUtilizationHoverItems: [EndpointTelemetryHoverItem]
    let gpuMemoryHoverItems: [EndpointTelemetryHoverItem]
    let rejectedHostSampleCount: Int
    let generatedAt: Date?
    let hostSamplingGapCount: Int

    init(history: EndpointTelemetryHistory) {
        range = history.range
        generatedAt = history.generatedAt.flatMap(endpointTelemetryHistoryDate)
        var seenHostIDs = Set<String>()
        let decodedHost = history.samples.compactMap(EndpointTelemetryPreparedHostSample.init).sorted { $0.timestamp < $1.timestamp }
        hostSamples = decodedHost.filter { seenHostIDs.insert($0.id).inserted }
        rejectedHostSampleCount = history.samples.count - hostSamples.count
        hostSamplingGapCount = EndpointTelemetryPreparedHistory.gapCount(in: hostSamples.map(\.timestamp))

        cpuSeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "cpu", label: "CPU", color: DesignTokens.chartSeries[0],
            samples: hostSamples.map { ($0.timestamp, $0.cpuFraction) }
        )]
        memorySeries = [EndpointTelemetryPreparedHistory.lineSeries(
            id: "memory", label: "内存", color: DesignTokens.chartSeries[2],
            samples: hostSamples.map { ($0.timestamp, $0.memoryFraction) }
        )]
        cpuHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "CPU 占用",
                entries: [EndpointTelemetryHoverEntry(id: "cpu", label: "CPU", value: historyPercent($0.cpuFraction), color: DesignTokens.chartSeries[0])]
            )
        }
        memoryHoverItems = hostSamples.map {
            EndpointTelemetryHoverItem(
                timestamp: $0.timestamp,
                title: "内存占用",
                entries: [EndpointTelemetryHoverEntry(id: "memory", label: "内存", value: historyPercent($0.memoryFraction), color: DesignTokens.chartSeries[2])]
            )
        }

        let preparedGPUs = history.gpuSeries
            .map(EndpointTelemetryPreparedGPUSeries.init)
            .sorted { $0.index < $1.index }
        gpuSeries = preparedGPUs
        // Stale device rows linger after a container rebuild: same gpu_index,
        // empty sample arrays in this window.  They must not occupy a chart
        // series or a legend slot.  Color stays aligned by visible GPU.
        let visibleGPUs = preparedGPUs.filter { !$0.samples.isEmpty }
        gpuUtilizationSeries = visibleGPUs.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: gpu.id, label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.gpuUtilizationFraction) }
            )
        }
        gpuMemorySeries = visibleGPUs.enumerated().map { offset, gpu in
            EndpointTelemetryPreparedHistory.lineSeries(
                id: "\(gpu.id)-memory", label: gpu.label, color: EndpointTelemetryPreparedHistory.gpuColor(offset),
                samples: gpu.samples.map { ($0.timestamp, $0.memoryFraction) }
            )
        }
        gpuUtilizationHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuUtilizationSeries,
            prefix: "GPU 利用率"
        )
        gpuMemoryHoverItems = EndpointTelemetryPreparedHistory.hoverItems(
            series: gpuMemorySeries,
            prefix: "GPU 显存"
        )
    }

    var lastObservationLabel: String {
        guard let latest = hostSamples.last else { return "时间未知" }
        return "更新于 \(historyShortTime(latest.timestamp))"
    }

    var visualWarningLabel: String? {
        if rejectedHostSampleCount > 0, hostSamplingGapCount > 0 {
            return "\(rejectedHostSampleCount) 异常 · \(hostSamplingGapCount) 断点"
        }
        if rejectedHostSampleCount > 0 { return "\(rejectedHostSampleCount) 异常" }
        if hostSamplingGapCount > 0 { return "\(hostSamplingGapCount) 断点" }
        if hostSamples.count > 1, hostSamples.count < 3 { return "样本不足" }
        return nil
    }

    var freshnessAndTrustLabel: String {
        var contexts = [freshnessLabel]
        if rejectedHostSampleCount > 0 { contexts.append("已省略 \(rejectedHostSampleCount) 个无法验证的主机样本。") }
        if hostSamples.count < 3, hostSamples.count > 1 {
            contexts.append("样本不足 3 个，未假设连续采样。")
        } else if hostSamplingGapCount > 0 {
            contexts.append("发现 \(hostSamplingGapCount) 段采样间隔，趋势已在间隔处断开。")
        }
        contexts.append("历史趋势只供检查；资源申请仍以当前快照和端点状态为准。")
        return contexts.joined(separator: " ")
    }

    var accessibilityValue: String {
        "范围 \(range.rawValue)，已验证主机样本 \(hostSamples.count) 个，GPU 序列 \(gpuUtilizationSeries.count) 条。\(freshnessAndTrustLabel)"
    }

    private var freshnessLabel: String {
        guard let generatedAt else { return "服务未提供历史响应生成时间，无法判定历史数据新鲜度。" }
        guard let latest = hostSamples.last else { return "响应生成于 \(historyDateTime(generatedAt))，但没有可验证的观测样本。" }
        let lag = generatedAt.timeIntervalSince(latest.timestamp)
        guard lag >= 0 else { return "响应生成时间早于最后观测时间，无法判定历史数据新鲜度。" }
        return "响应生成于 \(historyDateTime(generatedAt))；最后观测落后 \(historyElapsedDescription(lag))。"
    }

    private static func lineSeries(
        id: String, label: String, color: Color, samples: [(Date, Double?)]
    ) -> EndpointTelemetryLineSeries {
        let threshold = gapThreshold(samples.map(\.0))
        var points: [EndpointTelemetryChartPoint] = []
        var segment = 0
        var previousTimestamp: Date?
        for (timestamp, value) in samples {
            if let previousTimestamp, let threshold, timestamp.timeIntervalSince(previousTimestamp) > threshold { segment += 1 }
            guard let value else {
                segment += 1
                previousTimestamp = timestamp
                continue
            }
            points.append(EndpointTelemetryChartPoint(
                id: "\(id)-\(timestamp.timeIntervalSince1970)-\(segment)", timestamp: timestamp, value: value, segmentID: "\(id)-\(segment)"
            ))
            previousTimestamp = timestamp
        }
        return EndpointTelemetryLineSeries(id: id, label: label, color: color, points: points)
    }

    private static func hoverItems(series: [EndpointTelemetryLineSeries], prefix: String) -> [EndpointTelemetryHoverItem] {
        var values = [Date: [EndpointTelemetryHoverEntry]]()
        for line in series {
            for point in line.points {
                values[point.timestamp, default: []].append(
                    EndpointTelemetryHoverEntry(
                        id: line.id,
                        label: line.label,
                        value: historyPercent(point.value),
                        color: line.color
                    )
                )
            }
        }
        return values.map { timestamp, entries in
            EndpointTelemetryHoverItem(timestamp: timestamp, title: prefix, entries: entries)
        }.sorted { $0.timestamp < $1.timestamp }
    }

    private static func gapCount(in timestamps: [Date]) -> Int {
        guard let threshold = gapThreshold(timestamps) else { return 0 }
        return zip(timestamps, timestamps.dropFirst()).filter { $1.timeIntervalSince($0) > threshold }.count
    }

    private static func gapThreshold(_ timestamps: [Date]) -> TimeInterval? {
        let intervals = zip(timestamps, timestamps.dropFirst()).map { $1.timeIntervalSince($0) }.filter { $0 > 0 }
        guard intervals.count >= 2 else { return nil }
        let sorted = intervals.sorted()
        let middle = sorted.count / 2
        let median = sorted.count.isMultiple(of: 2) ? (sorted[middle - 1] + sorted[middle]) / 2 : sorted[middle]
        return median * 2.5
    }

    private static func gpuColor(_ index: Int) -> Color {
        DesignTokens.chartSeries[index % DesignTokens.chartSeries.count]
    }
}


private struct EndpointTelemetryPreparedHostSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let cpuFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointTelemetrySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp), sample.status.map({ ["ONLINE", "OK"].contains($0) }) ?? true else { return nil }
        let cpu = sample.cpuLoadFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard cpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        cpuFraction = cpu
        memoryFraction = memory
    }
}


private struct EndpointTelemetryPreparedGPUSeries: Identifiable, Equatable {
    let id: String
    let index: Int
    let label: String
    let samples: [EndpointTelemetryPreparedGPUSample]

    init(_ series: EndpointGPUHistorySeries) {
        let samples = series.samples.compactMap(EndpointTelemetryPreparedGPUSample.init).sorted { $0.timestamp < $1.timestamp }
        id = series.id
        index = series.index
        label = series.label
        self.samples = samples
    }
}


private struct EndpointTelemetryPreparedGPUSample: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let gpuUtilizationFraction: Double?
    let memoryFraction: Double?

    init?(_ sample: EndpointGPUHistorySample) {
        guard let timestamp = endpointTelemetryHistoryDate(sample.timestamp) else { return nil }
        let gpu = sample.gpuUtilizationFraction.flatMap(endpointTelemetryHistoryFraction)
        let memory = sample.memoryFraction.flatMap(endpointTelemetryHistoryFraction)
        guard gpu != nil || memory != nil else { return nil }
        id = sample.timestamp
        self.timestamp = timestamp
        gpuUtilizationFraction = gpu
        memoryFraction = memory
    }
}


private struct EndpointTelemetryLineSeries: Identifiable, Equatable {
    let id: String
    let label: String
    let color: Color
    let points: [EndpointTelemetryChartPoint]

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.points == rhs.points
    }
}


private struct EndpointTelemetryChartPoint: Identifiable, Equatable {
    let id: String
    let timestamp: Date
    let value: Double
    let segmentID: String
}


private struct EndpointTelemetryHoverItem: Identifiable, Equatable {
    var id: Date { timestamp }
    let timestamp: Date
    let title: String
    let entries: [EndpointTelemetryHoverEntry]

    var summary: String {
        "\(title)：\(entries.map { "\($0.label) \($0.value)" }.joined(separator: " · "))"
    }
}


private struct EndpointTelemetryHoverEntry: Identifiable, Equatable {
    let id: String
    let label: String
    let value: String
    let color: Color

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.label == rhs.label && lhs.value == rhs.value
    }
}


func endpointTelemetryHistoryDate(_ value: String) -> Date? {
    EndpointTelemetryHistoryDateParser.fractional.date(from: value)
        ?? EndpointTelemetryHistoryDateParser.standard.date(from: value)
}


private enum EndpointTelemetryHistoryDateParser {
    static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    static let standard = ISO8601DateFormatter()
}


private func endpointTelemetryHistoryFraction(_ value: Double) -> Double? {
    guard value.isFinite, (0...1).contains(value) else { return nil }
    return value
}


private func historyPercent(_ value: Double?) -> String {
    guard let value else { return "—" }
    return "\(Int((value * 100).rounded()))%"
}


func historyDateTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "M 月 d 日 HH:mm:ss"
    return formatter.string(from: value)
}


private func historyShortTime(_ value: Date) -> String {
    let formatter = DateFormatter()
    formatter.locale = Locale(identifier: "zh_CN")
    formatter.dateFormat = "HH:mm"
    return formatter.string(from: value)
}


func historyElapsedDescription(_ value: TimeInterval) -> String {
    let seconds = max(0, Int(value.rounded()))
    if seconds < 60 { return "\(seconds) 秒" }
    if seconds < 3_600 { return "\(seconds / 60) 分钟" }
    return "\(seconds / 3_600) 小时 \((seconds % 3_600) / 60) 分钟"
}

