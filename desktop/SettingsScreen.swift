import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct DashboardView: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ObservedObject var store: BrokerStore
    let addServer: () -> Void
    let claimGPU: () -> Void
    let claimEndpoint: (String) -> Void
    let manageGroups: () -> Void
    let openEndpoint: (EndpointRecord) -> Void
    @Binding var selectedSection: DashboardSection
    let selectGPU: (GPURecord) -> Void

    var body: some View {
        VStack(spacing: 0) {
            if let error = store.errorMessage {
                NoticeBanner(message: error, color: DesignTokens.danger, icon: "exclamationmark.triangle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if store.freshness == .stale {
                NoticeBanner(message: "连接已中断，显示上次数据。", color: DesignTokens.danger, icon: "wifi.exclamationmark")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            } else if let notice = displayedNotice {
                NoticeBanner(message: notice, color: DesignTokens.success, icon: "checkmark.circle.fill")
                    .padding(.horizontal, 24)
                    .padding(.top, 12)
            }

            Group {
                switch selectedSection {
                case .resources:
                    ResourcesDashboard(
                        store: store,
                        claimEndpoint: claimEndpoint,
                        manageGroups: manageGroups,
                        openEndpoint: openEndpoint,
                        selectGPU: selectGPU
                    )
                case .leases:
                    ResourceUsageDashboard(store: store, claimGPU: claimGPU)
                case .settings:
                    SettingsDashboard(store: store)
                }
            }
            .id(selectedSection)
            .transition(.opacity.combined(with: .offset(y: reduceMotion ? 0 : 6)))
            .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: selectedSection)
        }
        .background(Color.clear)
    }

    private var displayedNotice: String? {
        guard let notice = store.notice,
              notice.hasPrefix("已申领，待使用：")
        else { return store.notice }

        guard let runningLease = store.snapshot.leases.first(where: { lease in
            notice.contains(lease.id) && lease.runtimeState == "RUNNING"
        }) else { return notice }

        let task = runningLease.taskReference ?? runningLease.purpose ?? "未命名任务"
        return "任务占用：\(runningLease.projectID) · \(task) · \(runningLease.gpuIDs.count) GPU。"
    }
}


private struct SettingsDashboard: View {
    @ObservedObject var store: BrokerStore

    var body: some View {
        ScrollView {
            // The sidebar item and the page title already say "设置"; a third
            // section header repeats one piece of information three times,
            // which DESIGN_SYSTEM 4 forbids.
            VStack(alignment: .leading, spacing: 16) {
                HomeCard {
                    VStack(alignment: .leading, spacing: 14) {
                        CardSectionLabel(text: "本机服务")
                        SettingsFact(label: "服务地址", value: store.serviceAddress, icon: "network")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "版本", value: store.serviceInfo?.version ?? "未知", icon: "number")
                    }
                }

                HomeCard {
                    VStack(alignment: .leading, spacing: 14) {
                        CardSectionLabel(text: "数据状态")
                        SettingsFact(label: "连接", value: connectionValue, icon: "bolt.horizontal.circle")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "快照", value: snapshotValue, icon: "clock")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "清单", value: inventoryValue, icon: "server.rack")
                        Divider().opacity(DesignTokens.Alpha.strong)
                        SettingsFact(label: "资源变更", value: store.allowsMutations ? "可执行" : store.mutationUnavailableReason, icon: "hand.raised")
                    }
                }

                if store.supportsCollectorSettings {
                    HomeCard {
                        VStack(alignment: .leading, spacing: 14) {
                            CardSectionLabel(text: "数据采集")
                            HStack(spacing: 12) {
                                SettingsIcon(icon: "clock.arrow.circlepath")
                                VStack(alignment: .leading, spacing: 2) {
                                    Text("采集间隔")
                                        .font(Typography.identity)
                                        .foregroundStyle(DesignTokens.ink)
                                    Text("每台服务器的只读探针执行频率。")
                                        .font(Typography.metricLabel)
                                        .foregroundStyle(DesignTokens.mutedInk)
                                }
                                Spacer(minLength: 16)
                                Picker(
                                    "数据采集间隔",
                                    selection: Binding(
                                        get: { store.collectorSettings?.intervalSeconds ?? 10 },
                                        set: { store.updateCollectorInterval($0) { _, _ in } }
                                    )
                                ) {
                                    ForEach(store.collectorSettings?.allowedIntervals ?? [5, 10, 30], id: \.self) { seconds in
                                        Text("\(seconds) 秒").tag(seconds)
                                    }
                                }
                                .pickerStyle(.segmented)
                                .labelsHidden()
                                .frame(width: 210)
                                .accessibilityLabel("数据采集间隔")
                                .accessibilityValue("\(store.collectorSettings?.intervalSeconds ?? 10) 秒")
                                .disabled(
                                    store.collectorSettingsLoading
                                        || store.collectorSettings == nil
                                        || !store.canUpdateCollectorSettings
                                )
                            }
                        }
                    }
                }

                if store.supportsMcpEntry {
                    MCPEntryPanel(entry: store.mcpEntry, loading: store.mcpEntryLoading)
                }
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 24)
            .frame(maxWidth: 680, alignment: .leading)
            // Without this the ScrollView centres the capped column, which put
            // the settings cards in the middle of a wide window while every
            // other page starts at the left margin.
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(DesignTokens.ambientSmoke)
    }

    private var connectionValue: String {
        // The fixture provider reports connected, so the read-only case has to
        // be tested first or it claims a live local service that is not there.
        guard store.canRefresh else { return "只读测试夹具" }
        guard store.isConnected else { return "未连接" }
        let address = store.serviceAddress
        return address.isEmpty || address == "—" ? "已连接" : "已连接 \(address)"
    }

    private var snapshotValue: String {
        let state = switch store.freshness {
        case .fresh: "最新"
        case .stale: "已过期"
        case .failed: "获取失败"
        case .waiting: "等待中"
        }
        guard let revision = store.snapshot.snapshotRevision else { return state }
        return "\(state) · 修订 \(revision)"
    }

    private var inventoryValue: String {
        let endpoints = store.snapshot.endpoints.count
        let gpus = store.snapshot.gpus.count
        let leases = store.snapshot.leases.count
        return "\(endpoints) 台服务器 · \(gpus) 张 GPU · \(leases) 个租约"
    }
}

/// Tinted square that carries a settings row's symbol.

private struct SettingsIcon: View {
    let icon: String

    var body: some View {
        Image(systemName: icon)
            .font(.callout.weight(.semibold))
            .foregroundStyle(DesignTokens.interaction)
            .frame(width: 30, height: 30)
            .background(
                DesignTokens.interaction.opacity(DesignTokens.Alpha.fill),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .accessibilityHidden(true)
    }
}


private struct SettingsFact: View {
    let label: String
    let value: String
    let icon: String

    var body: some View {
        HStack(spacing: 12) {
            SettingsIcon(icon: icon)
            Text(label)
                .font(Typography.identity)
                .foregroundStyle(DesignTokens.ink)
            Spacer(minLength: 16)
            Text(value)
                .font(Typography.identity.weight(.semibold))
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .accessibilityElement(children: .combine)
    }
}


private struct MCPEntryPanel: View {
    let entry: MCPEntryRecord?
    let loading: Bool
    @State private var copiedToken: String?

    var body: some View {
        HomeCard {
            VStack(alignment: .leading, spacing: 14) {
                CardSectionLabel(text: "Agent MCP")
                if let entry, entry.available, let command = entry.command, let configJSON = entry.configJSON {
                    copyRow(label: "入口路径", value: command, token: "path")
                    Divider().opacity(DesignTokens.Alpha.strong)
                    copyBlock(label: "mcpServers 配置", value: configJSON, token: "config")
                } else if loading && entry == nil {
                    Text("正在读取 MCP 入口。")
                        .font(Typography.identity)
                        .foregroundStyle(DesignTokens.mutedInk)
                } else {
                    Text("未找到 MCP 入口。")
                        .font(Typography.identity)
                        .foregroundStyle(DesignTokens.ink)
                    if let hint = entry?.hint, !hint.isEmpty {
                        Text(hint)
                            .font(Typography.command)
                            .foregroundStyle(DesignTokens.mutedInk)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func copyRow(label: String, value: String, token: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                SettingsIcon(icon: "terminal")
                Text(label)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 16)
                copyButton(value: value, token: token, accessibilityLabel: "复制\(label)")
            }
            Text(value)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .lineLimit(2)
                .truncationMode(.middle)
        }
    }

    private func copyBlock(label: String, value: String, token: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                SettingsIcon(icon: "doc.on.clipboard")
                Text(label)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.ink)
                Spacer(minLength: 16)
                copyButton(value: value, token: token, accessibilityLabel: "复制\(label)")
            }
            Text(value)
                .font(Typography.command)
                .foregroundStyle(DesignTokens.ink)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func copyButton(value: String, token: String, accessibilityLabel: String) -> some View {
        Button(copiedToken == token ? "已复制" : "复制") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(value, forType: .string)
            copiedToken = token
        }
        .font(Typography.identity)
        .foregroundStyle(DesignTokens.interaction)
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
    }
}

