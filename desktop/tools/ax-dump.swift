// Accessibility-tree dump through the same AXUIElement C API VoiceOver uses.
//
// The evidence harness previously walked the tree through AppleScript's System
// Events bridge, which does not descend into SwiftUI scroll content: it
// reported zero server rows for both the old table and the new tile grid, while
// this API shows them with their labels and identifiers intact.  Use this for
// any claim about what assistive technology can reach.

import ApplicationServices
import AppKit

// Walk the accessibility tree through the same C API VoiceOver uses, rather
// than through AppleScript's System Events bridge.
func attr(_ el: AXUIElement, _ name: String) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, name as CFString, &value) == .success else { return nil }
    if let s = value as? String, !s.isEmpty { return s }
    if let n = value as? NSNumber { return n.stringValue }
    return nil
}

func children(_ el: AXUIElement) -> [AXUIElement] {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &value) == .success,
          let kids = value as? [AXUIElement] else { return [] }
    return kids
}

var lines: [String] = []
func walk(_ el: AXUIElement, _ depth: Int) {
    if depth > 20 { return }
    let role = attr(el, kAXRoleAttribute as String) ?? "?"
    let parts = ["AXDescription", "AXTitle", "AXValue", "AXHelp", "AXIdentifier"]
        .compactMap { attr(el, $0) }
    if !parts.isEmpty {
        lines.append("\(depth)\t\(role)\t\(parts.joined(separator: " | "))")
    }
    for kid in children(el) { walk(kid, depth + 1) }
}

let pid = Int32(CommandLine.arguments[1])!
let app = AXUIElementCreateApplication(pid)
var winValue: CFTypeRef?
if AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &winValue) == .success,
   let windows = winValue as? [AXUIElement], let first = windows.first {
    walk(first, 0)
} else {
    lines.append("no window")
}
print("element_lines=\(lines.count)")
print(lines.joined(separator: "\n"))
