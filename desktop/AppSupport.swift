import AppKit
import SwiftUI

/// The app's complete type ramp.
///
/// 26 for the figures a page exists to answer, 17 for its title, 15 for a section
/// or a task's name, 13 for what a row says first, 11 for what explains it.  A
/// number outranks the title above it, because the number is what a person came
/// for.  12 is only for field text and command lines in forms and sheets.
///
/// Add a role rather than a size: a new `.system(size:)` in interface code is
/// a regression, not a tweak.  Numerals carry `.monospacedDigit()` because the
/// collector repaints every few seconds and proportional figures twitch.

/// The one card shape in the app.
///
/// The server grid established the Home tile; the detail sheet, usage page, and
/// settings each had their own radius, fill opacity, and stroke, so the app read
/// as three products.  Every page-level panel now renders through this.
struct HomeCard<Content: View>: View {
    var padding: CGFloat = 20
    var radius: CGFloat = DesignTokens.Radius.panel
    @ViewBuilder var content: () -> Content

    var body: some View {
        content()
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(DesignTokens.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .strokeBorder(DesignTokens.tileStroke, lineWidth: 1)
            )
    }
}

/// Group label above a run of rows inside a card.
///
/// `accessory` is a quiet chip for a fact that would otherwise occupy a
/// whole field — allocation mode next to「服务器组」, for example.
struct CardSectionLabel: View {
    let text: String
    var accessory: String? = nil

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Text(text)
                .font(Typography.annotation)
                .foregroundStyle(DesignTokens.mutedInk)
                .textCase(nil)
                .accessibilityAddTraits(.isHeader)
            if let accessory, !accessory.isEmpty {
                Text(accessory)
                    .font(Typography.annotation)
                    .foregroundStyle(DesignTokens.mutedInk)
                    .padding(.horizontal, 7)
                    .frame(height: 20)
                    .background(
                        DesignTokens.ink.opacity(0.045),
                        in: Capsule()
                    )
            }
        }
    }
}

enum Typography {
    /// 26 · The figures a page exists to answer: everywhere else a number sits in its row.
    static let display = Font.system(.largeTitle, design: .rounded).weight(.semibold).monospacedDigit()
    /// 17 · A page's title in its header, and a sheet's title.
    static let screenTitle = Font.title2.weight(.semibold)
    /// 17 · A glyph standing in for an empty page.
    static let icon = Font.title2
    /// 15 · A section on a page, a task's name, and the figures on a task card.
    static let sectionTitle = Font.title3.weight(.semibold)
    /// 15 · The primary value on a card or ring.
    static let cardValue = Font.system(.title3, design: .rounded).weight(.semibold).monospacedDigit()
    /// 13 · A card's heading, field labels and button text.  Table column headers
    /// use `annotation`: at 13 pt they outweighed the values underneath them.
    static let label = Font.body.weight(.semibold)
    /// 13 · Running text.
    static let body = Font.body
    /// 13 · Percentages, counts, ratios inside a row.
    static let rowValue = Font.body.weight(.medium).monospacedDigit()
    /// 13 · The first line of a two-line row: an address, a task.
    static let rowPrimary = Font.body.weight(.medium).monospacedDigit()
    /// 12 · Project, task, model.
    static let identity = Font.callout.weight(.medium)
    /// 12 · SSH commands and GPU identifiers stay monospaced.
    static let command = Font.system(.callout, design: .monospaced).weight(.medium)
    /// 11 · Units, timestamps, status words, null markers.
    static let annotation = Font.subheadline.weight(.medium)
    /// 11 · Chart scales and identifiers inside a row.
    static let commandSmall = Font.system(.subheadline, design: .monospaced).weight(.medium)
}

/// Republishes when the system accessibility display options change.
///
/// `DesignTokens` resolves the Increase Contrast flag inside `NSColor` dynamic
/// providers, but SwiftUI keeps a resolved colour for the lifetime of a view
/// body.  Observing this and keying the root on `generation` rebuilds the tree
/// so the new values are actually read.
final class ContrastState: ObservableObject {
    static let shared = ContrastState()

    @Published private(set) var generation = 0

    private init() {
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            self?.generation += 1
        }
    }
}

enum DesignTokens {
    static let ink = Color(nsColor: .labelColor)
    /// `secondaryLabelColor` measures 3.95:1 on white — under the 4.5 body
    /// floor, and this app renders units and timestamps in it at 10pt.
    static let mutedInk = Color(nsColor: NSColor(name: nil) { appearance in
        _ = appearance
        return wantsIncreasedContrast
            ? NSColor(srgbRed: 0.24, green: 0.25, blue: 0.27, alpha: 1)
            : NSColor(srgbRed: 107.0 / 255.0, green: 110.0 / 255.0, blue: 117.0 / 255.0, alpha: 1)
    })
    static let onInteraction = Color(nsColor: .selectedMenuItemTextColor)
    // Apple Home-style restraint: one system interaction/resource accent.
    // Green, orange, and red are reserved for semantic status only.
    static let interaction = Color(nsColor: .controlAccentColor)
    // Resource categories stay neutral; labels and SF Symbols carry meaning.
    static let cpu = mutedInk
    static let memory = mutedInk
    static let gpu = mutedInk
    static let network = mutedInk
    // Beszel-derived semantic palette: success, warning, danger.
    // Status colors are graphical objects (dots, pressure bars), so they must
    // clear the 3:1 WCAG 1.4.11 floor against both the content surface and the
    // page background.  Measured against #F6F7F9 / #E4E7EC the three land at
    // 3.48 / 3.50 / 3.55 and 3.01 / 3.03 / 3.07, which also keeps their visual
    // weight matched.  Keep the hues; only lightness carries the contrast.
    // Status colour is two tiers, because one tier cannot be both luminous and
    // legible.  Forcing a 3:1 mark onto a ~50pt dot pushed green and amber into
    // forest and mustard: WCAG weights green at 0.7152, so at that contrast on
    // a near-white plane there is no luminous green.  Apple's own systemGreen
    // measures 1.79:1 against the page background — Apple spends colour on
    // large fills with dark content over them, and lets small pips be
    // decoration beside a word that repeats the state.
    //
    // Deep tier: marks, glyphs, and status words.  Solved on the OKLCh gamut
    // hull at >= 4.5:1 against the content surface, so it is legal on text too
    // and is *more* chromatic than the values it replaces.
    // Under Increase Contrast each is re-solved on the same hue at >= 7:1
    // (AAA) instead of 4.9:1, so the mark and the status word both harden.
    static let success = statusColor(
        normal: (0.0, 131.0, 47.0),
        increased: (0.0, 99.0, 34.0)
    )
    static let warning = statusColor(
        normal: (176.0, 90.0, 0.0),
        increased: (140.0, 71.0, 0.0)
    )
    static let danger = statusColor(
        normal: (228.0, 0.0, 33.0),
        increased: (182.0, 0.0, 26.0)
    )

    private static func statusColor(
        normal: (Double, Double, Double),
        increased: (Double, Double, Double)
    ) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            _ = appearance
            let channels = wantsIncreasedContrast ? increased : normal
            return NSColor(
                srgbRed: channels.0 / 255.0,
                green: channels.1 / 255.0,
                blue: channels.2 / 255.0,
                alpha: 1
            )
        })
    }


    /// Held or keepalive-owned: real state, but not a warning.  Neutral so a
    /// user-chosen accent can never drop it below the graphical floor.
    static let hold = Color(red: 91.0 / 255.0, green: 97.0 / 255.0, blue: 107.0 / 255.0)
    /// A card with room to spare, and the lines that plot how busy cards are.  Fixed
    /// rather than the system accent: this colour means something, and a person
    /// changing their accent must not change what it says.
    static let loadBlue = statusColor(
        normal: (0.0, 112.0, 240.0),
        increased: (0.0, 88.0, 208.0)
    )
    /// A bar nothing is wrong with.
    static let meterFill = statusColor(
        normal: (180.0, 180.0, 186.0),
        increased: (99.0, 99.0, 102.0)
    )
    /// A bar that is not a load: a keepalive's own hold, or a reading gone stale.
    static let meterMuted = statusColor(
        normal: (217.0, 217.0, 222.0),
        increased: (174.0, 174.0, 178.0)
    )
    /// A card in use whose load nothing measured, as a filled square.
    static let unmeasuredCard = statusColor(
        normal: (142.0, 142.0, 147.0),
        increased: (99.0, 99.0, 102.0)
    )
    /// A card close to full.  A brighter yellow than the `warning` used on text,
    /// so the square reads as a load, not as something needing a person.
    static let nearFullCard = statusColor(
        normal: (255.0, 196.0, 0.0),
        increased: (214.0, 148.0, 0.0)
    )
    static let nearFullCardEdge = statusColor(
        normal: (214.0, 158.0, 0.0),
        increased: (150.0, 100.0, 0.0)
    )
    /// A free card is an outline: the fleet at rest stays quiet, and colour is
    /// left for what is using a card or needs a person.
    static let freeCardStroke = statusColor(
        normal: (168.0, 172.0, 180.0),
        increased: (110.0, 114.0, 122.0)
    )
    /// A card nobody can currently speak for.
    static let staleCard = statusColor(
        normal: (229.0, 229.0, 234.0),
        increased: (199.0, 199.0, 204.0)
    )
    /// A pressure bar below the attention threshold, and a resting trend line.
    static let quietBar = statusColor(
        normal: (142.0, 142.0, 147.0),
        increased: (99.0, 99.0, 102.0)
    )
    /// Unused portion of every pressure bar.  Under Increase Contrast the
    /// track darkens too, so the bar's full extent stays readable.
    static let track = statusColor(
        normal: (229.0, 232.0, 238.0),
        increased: (198.0, 203.0, 214.0)
    )
    /// Lines that plot over time.  A hue no state uses, so a chart beside the
    /// squares cannot read as "low utilization", which is what blue means there.
    static let chartLine = statusColor(
        normal: (8.0, 145.0, 178.0),
        increased: (14.0, 116.0, 144.0)
    )
    static let selection = Color(nsColor: .unemphasizedSelectedContentBackgroundColor)
    /// Content plane.  White against a single page plane gives one visible
    /// 1.18:1 step; the previous three planes stepped 1.06-1.09 each, which
    /// reads as a colour-management bug rather than as elevation.
    static let surface = Color.white
    /// True when the system asks for stronger contrast.
    ///
    /// Read from the workspace rather than from the appearance:
    /// `NSAppearance(named: .accessibilityHighContrastAqua)` resolves to plain
    /// aqua on this macOS, so an appearance-based branch never fires.
    static var wantsIncreasedContrast: Bool {
#if DEBUG || DESKTOP_FIXTURES
        // `-AppleIncreaseContrast YES` does not drive the accessibility flag,
        // so without this the fixture harness can only ever report the option
        // as "not measured".  This proves the token wiring, not the OS
        // integration: only the real System Settings toggle proves that.
        if ProcessInfo.processInfo.environment["SERVERPILOT_DESKTOP_FORCE_INCREASE_CONTRAST"] == "1" {
            return true
        }
#endif
        return NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast
    }

    static let surfaceStroke = Color(nsColor: NSColor(name: nil) { appearance in
        // A hairline at 8% disappears under Increase Contrast, which is exactly
        // when the reader needs the edge.
        _ = appearance
        return NSColor(white: 0, alpha: wantsIncreasedContrast ? 0.20 : 0.08)
    })
    /// Page plane.  Planes are told apart by an edge, not by a step in grey:
    /// shadow and tint are kept for what actually floats.
    static let page = surface
    /// A panel set into a card, such as one GPU in a server's detail.
    static let subtle = statusColor(
        normal: (245.0, 245.0, 247.0),
        increased: (229.0, 229.0, 234.0)
    )
    /// A card's edge on the page plane.
    static let tileStroke = Color(nsColor: NSColor(name: nil) { appearance in
        _ = appearance
        return NSColor(white: 0, alpha: wantsIncreasedContrast ? 0.34 : 0.10)
    })
    /// The same edge under the pointer, and around an outlined button.
    static let tileStrokeStrong = Color(nsColor: NSColor(name: nil) { appearance in
        _ = appearance
        return NSColor(white: 0, alpha: wantsIncreasedContrast ? 0.50 : 0.20)
    })

    /// Three radii, one per role.  Eleven distinct values read as drift rather
    /// than as a system; anything between two steps rounds to the nearer one.
    enum Radius {
        /// Pips, count chips, inline badges.
        static let pip: CGFloat = 4
        /// Buttons, fields, segmented controls, icon squares.
        static let control: CGFloat = 6
        /// Cards, and the panels set into them.
        static let panel: CGFloat = 10
    }

    /// Five alphas.  Same reasoning as `Radius`: twenty-two values cannot be
    /// held in mind, and neighbouring ones were not visually distinguishable.
    enum Alpha {
        /// Barely-there zebra or hover wash.
        static let hairline: Double = 0.04
        /// Hairlines and dividers.
        static let edge: Double = 0.08
        /// Tinted icon squares and chip fills.
        static let fill: Double = 0.12
        /// Selected or hovered outlines.
        static let muted: Double = 0.30
        /// Emphasis fills that still sit under text.
        static let strong: Double = 0.55
    }

    static let chartSeries: [Color] = [
        chartColor(light: (0.22, 0.43, 0.76), dark: (0.40, 0.61, 0.94)),
        chartColor(light: (0.82, 0.43, 0.16), dark: (0.95, 0.59, 0.29)),
        chartColor(light: (0.08, 0.55, 0.51), dark: (0.24, 0.75, 0.70)),
        chartColor(light: (0.64, 0.27, 0.67), dark: (0.78, 0.45, 0.82)),
        chartColor(light: (0.34, 0.57, 0.25), dark: (0.52, 0.74, 0.41)),
        chartColor(light: (0.76, 0.25, 0.27), dark: (0.92, 0.43, 0.43)),
        chartColor(light: (0.39, 0.33, 0.68), dark: (0.57, 0.51, 0.86)),
        chartColor(light: (0.68, 0.50, 0.10), dark: (0.86, 0.68, 0.27))
    ]

    private static func chartColor(
        light: (CGFloat, CGFloat, CGFloat),
        dark: (CGFloat, CGFloat, CGFloat)
    ) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let components = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
            return NSColor(srgbRed: components.0, green: components.1, blue: components.2, alpha: 1)
        })
    }
}

enum DashboardSection: Hashable {
    case overview
    case resources
    case leases
    case alerts
    case settings
}

struct AmbientBackground: View {
    var body: some View {
        DesignTokens.page
            .ignoresSafeArea()
    }
}

struct SoftButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    let tint: Color
    let foreground: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(foreground.opacity(isEnabled ? 1 : 0.52))
            .padding(.horizontal, 13)
            .frame(height: 31)
            .background(
                tint.opacity(isEnabled ? (configuration.isPressed ? 0.72 : 0.94) : 0.22),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct PrimaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Typography.label)
            .foregroundStyle(DesignTokens.onInteraction.opacity(isEnabled ? 1 : 0.58))
            .padding(.horizontal, 14)
            .frame(height: 30)
            .background(
                DesignTokens.ink.opacity(isEnabled ? (configuration.isPressed ? 0.78 : 1) : 0.26),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SecondaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Typography.label)
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .padding(.horizontal, 12)
            .frame(height: 30)
            .background(
                DesignTokens.ink.opacity(configuration.isPressed ? DesignTokens.Alpha.edge : 0),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
                    .strokeBorder(DesignTokens.tileStrokeStrong, lineWidth: 1)
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

/// A quiet button: nothing but its glyph until the pointer is on it.
struct IconActionButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        IconActionLabel(configuration: configuration)
    }
}

private struct IconActionLabel: View {
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false
    let configuration: ButtonStyleConfiguration

    private var wash: Double {
        guard isEnabled else { return 0 }
        if configuration.isPressed { return DesignTokens.Alpha.fill }
        return hovering ? DesignTokens.Alpha.edge : 0
    }

    var body: some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .frame(width: 30, height: 30)
            .background(
                DesignTokens.ink.opacity(wash),
                in: RoundedRectangle(cornerRadius: DesignTokens.Radius.control, style: .continuous)
            )
            .contentShape(Rectangle())
            .onHover { hovering = $0 }
            .scaleEffect(configuration.isPressed ? 0.95 : 1)
    }
}

struct VisualEffect: NSViewRepresentable {
    let material: NSVisualEffectView.Material
    let blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
    }
}

extension View {
    func fieldLabel() -> some View {
        font(Typography.identity.weight(.semibold))
            .foregroundStyle(DesignTokens.ink)
    }

}
