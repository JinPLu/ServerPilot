import AppKit
import SwiftUI

/// The app's complete type ramp.
///
/// Eight roles over seven Apple semantic steps (10 / 11 / 12 / 13 / 15 / 17 /
/// 26).  The previous ramp spanned only 17/10 = 1.70x and, because macOS
/// resolves `footnote` and `caption2` to the same 10pt, two of its steps were
/// a duplicate pair — so nothing on a screen could read as primary.  This one
/// spans 2.60x, which is the band Apple Home works in.
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
    var radius: CGFloat = DesignTokens.Radius.tile
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
                .font(Typography.metricLabel)
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
    /// 26 · The one dominant number on a screen.  Never more than one.
    static let hero = Font.system(.largeTitle, design: .rounded).weight(.semibold).monospacedDigit()
    /// 17 · Page and sheet titles.
    static let pageTitle = Font.title2.weight(.semibold)
    /// 15 · The primary value on a card or ring.
    static let cardValue = Font.system(.title3, design: .rounded).weight(.semibold).monospacedDigit()
    /// 13 · Section titles and field labels.  Table column headers use
    /// `annotation`: they sit above 11 pt rows, and at 13 pt they outweighed
    /// the values underneath them.
    static let label = Font.headline
    /// 13 · The one explanatory sentence in an error or empty state.
    static let prose = Font.body
    /// 12 · Percentages, counts, ratios inside a row.
    static let rowValue = Font.callout.weight(.medium).monospacedDigit()
    /// 11 · Project, task, model.
    static let identity = Font.subheadline.weight(.medium)
    /// 11 · SSH commands and GPU identifiers stay monospaced.
    static let command = Font.system(.subheadline, design: .monospaced).weight(.medium)
    /// 10 · Units, timestamps, status words, null markers.
    static let annotation = Font.caption.weight(.medium)

    // Retired names kept only so no call site silently changes meaning during
    // the migration; both resolved to 10pt, which was the duplicate pair.
    static let metricLabel = annotation
    static let secondary = annotation
    static let metricValue = rowValue
    static let sectionTitle = label
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

    // Luminous tier: area only.  Apple's system colours composited at 12% over
    // white, shipped opaque so they do not drift with the plane beneath.  The
    // screen reads luminous because these cover roughly twenty times the area
    // of the marks above.
    static let successWash = Color(red: 231.0 / 255.0, green: 248.0 / 255.0, blue: 235.0 / 255.0)
    static let warningWash = Color(red: 1.0, green: 241.0 / 255.0, blue: 229.0 / 255.0)
    static let dangerWash = Color(red: 1.0, green: 231.0 / 255.0, blue: 232.0 / 255.0)

    /// Held or keepalive-owned: real state, but not a warning.  Neutral so a
    /// user-chosen accent can never drop it below the graphical floor.
    static let hold = Color(red: 91.0 / 255.0, green: 97.0 / 255.0, blue: 107.0 / 255.0)
    /// Unused portion of every pressure bar.  Under Increase Contrast the
    /// track darkens too, so the bar's full extent stays readable.
    static let track = statusColor(
        normal: (229.0, 232.0, 238.0),
        increased: (198.0, 203.0, 214.0)
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
    /// Page plane.  1.184:1 below `surface` — one step, deliberately visible.
    static let ambientSmoke = statusColor(
        normal: (233.0, 236.0, 241.0),
        increased: (221.0, 225.0, 233.0)
    )
    /// Tile edge.  Home tiles carry no outline normally — the elevation step
    /// does that work — but Increase Contrast asks for a drawn boundary.
    static let tileStroke = Color(nsColor: NSColor(name: nil) { appearance in
        _ = appearance
        return NSColor(white: 0, alpha: wantsIncreasedContrast ? 0.34 : 0)
    })
    /// The retired middle plane.  Kept as an alias so existing call sites do
    /// not gain a third elevation back; it now resolves to the content plane.
    static let glassSmoke = surface

    /// Four radii, one per role.  Eleven distinct values read as drift rather
    /// than as a system; anything between two steps rounds to the nearer one.
    enum Radius {
        /// Pips, count chips, inline badges.
        static let pip: CGFloat = 4
        /// Buttons, fields, segmented controls, icon squares.
        static let control: CGFloat = 7
        /// Inner panels nested inside a tile, and sheet sections.
        static let panel: CGFloat = 10
        /// The tile itself, and any page-level card that is one.
        static let tile: CGFloat = 20
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
    case resources
    case leases
    case settings
}

struct AmbientBackground: View {
    var body: some View {
        DesignTokens.ambientSmoke
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
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SoftIconButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Circle()
            )
            .overlay(Circle().stroke(DesignTokens.surfaceStroke, lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.94 : 1)
    }
}

struct PrimaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.callout.weight(.semibold))
            .foregroundStyle(DesignTokens.onInteraction.opacity(isEnabled ? 1 : 0.58))
            .padding(.horizontal, 15)
            .frame(height: 34)
            .background(
                DesignTokens.interaction.opacity(isEnabled ? (configuration.isPressed ? 0.78 : 1) : 0.26),
                in: Capsule()
            )
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct SecondaryActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.callout.weight(.semibold))
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .padding(.horizontal, 14)
            .frame(height: 34)
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Capsule()
            )
            .overlay(Capsule().stroke(DesignTokens.surfaceStroke.opacity(isEnabled ? 1 : 0.50), lineWidth: 1))
            .scaleEffect(configuration.isPressed ? 0.98 : 1)
    }
}

struct IconActionButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(DesignTokens.ink.opacity(isEnabled ? 1 : 0.42))
            .frame(width: 34, height: 34)
            .background(
                DesignTokens.surface.opacity(isEnabled ? (configuration.isPressed ? 0.74 : 1) : 0.46),
                in: Circle()
            )
            .overlay(Circle().stroke(DesignTokens.surfaceStroke.opacity(isEnabled ? 1 : 0.50), lineWidth: 1))
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
        font(.callout.weight(.semibold))
            .foregroundStyle(DesignTokens.ink)
    }

    @ViewBuilder
    func spatialGlass<S: Shape>(in shape: S) -> some View {
        background(DesignTokens.surface, in: shape)
            .overlay(shape.stroke(DesignTokens.surfaceStroke, lineWidth: 1))
    }

    func spatialContentSurface() -> some View {
        background(DesignTokens.glassSmoke)
    }
}
