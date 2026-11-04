import AppKit
import Foundation
import SwiftUI
#if canImport(Charts)
import Charts
#endif

struct SheetTitle: View {
    let icon: String
    let title: String
    let subtitle: String

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(Typography.screenTitle)
                .foregroundStyle(DesignTokens.ink)
                .frame(width: 42, height: 42)
                .background(DesignTokens.selection, in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous))
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(Typography.screenTitle)
                    .foregroundStyle(DesignTokens.ink)
                Text(subtitle)
                    .font(Typography.identity)
                    .foregroundStyle(DesignTokens.mutedInk)
            }
        }
    }
}


struct LabeledField: View {
    let label: String
    let placeholder: String
    @Binding var text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label)
                .fieldLabel()
            TextField(placeholder, text: $text)
                .textFieldStyle(.roundedBorder)
        }
    }
}


struct InlineValidation: View {
    let message: String

    var body: some View {
        Label(message, systemImage: "exclamationmark.triangle.fill")
            .font(Typography.identity)
            .foregroundStyle(DesignTokens.danger)
    }
}


struct InlineResult: View {
    let message: String
    let allocated: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: allocated ? "checkmark.circle.fill" : "hourglass")
                .foregroundStyle(allocated ? DesignTokens.success : DesignTokens.warning)
            Text(message)
                .foregroundStyle(DesignTokens.ink)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(Typography.identity)
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            (allocated ? DesignTokens.success : DesignTokens.warning).opacity(DesignTokens.Alpha.fill),
            in: RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.panel, style: .continuous)
                .stroke((allocated ? DesignTokens.success : DesignTokens.warning).opacity(DesignTokens.Alpha.muted), lineWidth: 1)
        )
    }
}

