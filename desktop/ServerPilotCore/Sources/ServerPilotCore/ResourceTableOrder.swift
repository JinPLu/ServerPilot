import Foundation

public enum ResourceTableSortValue: Equatable, Sendable {
    case text(String)
    case number(Double)
}

public enum ResourceTableOrder {
    /// Unknown values stay last in either direction; equal values keep natural address order.
    public static func precedes(
        _ left: ResourceTableSortValue?, _ right: ResourceTableSortValue?,
        leftAddress: String, rightAddress: String, ascending: Bool
    ) -> Bool {
        let order: ComparisonResult
        switch (left, right) {
        case (nil, .some): return false
        case (.some, nil): return true
        case (nil, nil): order = .orderedSame
        case let (.text(a), .text(b)): order = a.localizedStandardCompare(b)
        case let (.number(a), .number(b)):
            order = a < b ? .orderedAscending : a > b ? .orderedDescending : .orderedSame
        case (.text, .number): order = .orderedAscending
        case (.number, .text): order = .orderedDescending
        }
        if order == .orderedSame {
            return leftAddress.localizedStandardCompare(rightAddress) == .orderedAscending
        }
        return ascending ? order == .orderedAscending : order == .orderedDescending
    }
}
