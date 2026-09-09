import Foundation
import Testing
@testable import ServerPilotCore

struct ResourceTableOrderTests {
    @Test func unknownMetricsRemainLastInBothDirections() {
        let rows: [(String, ResourceTableSortValue?)] = [
            ("node10", .number(0.8)), ("login", nil),
            ("node2", .number(0.2)), ("drained", nil),
        ]
        func ordered(_ ascending: Bool) -> [String] {
            rows.sorted {
                ResourceTableOrder.precedes($0.1, $1.1, leftAddress: $0.0,
                                             rightAddress: $1.0, ascending: ascending)
            }.map(\.0)
        }
        #expect(ordered(true) == ["node2", "node10", "drained", "login"])
        #expect(ordered(false) == ["node10", "node2", "drained", "login"])
    }

    @Test func addressSortingUsesNaturalHostAndPortOrder() {
        let addresses = ["10.0.0.10:22", "10.0.0.2:100", "10.0.0.2:9"]
        let sorted = addresses.sorted {
            ResourceTableOrder.precedes(.text($0), .text($1), leftAddress: $0,
                                         rightAddress: $1, ascending: true)
        }
        #expect(sorted == ["10.0.0.2:9", "10.0.0.2:100", "10.0.0.10:22"])
    }

    @Test func equalValuesKeepNaturalNodeOrderWhenDescending() {
        #expect(ResourceTableOrder.precedes(.number(3), .number(3),
            leftAddress: "hanhai22:22 / gnode2", rightAddress: "hanhai22:22 / gnode10",
            ascending: false))
        #expect(!ResourceTableOrder.precedes(.number(3), .number(3),
            leftAddress: "hanhai22:22 / gnode10", rightAddress: "hanhai22:22 / gnode2",
            ascending: false))
    }

    @Test func connectionAddressUsesBracketsForIPv6AndPreservesSSHCommand() throws {
        let endpoint = try #require(EndpointRecord(raw: [
            "id": "node", "host": "2001:db8::1", "port": 2222, "ssh_user": "user",
        ]))
        #expect(endpoint.connectionAddress == "[2001:db8::1]:2222")
        #expect(endpoint.sshCommand.contains("2222"))
        #expect(endpoint.sshCommand.contains("user@"))
    }
}
