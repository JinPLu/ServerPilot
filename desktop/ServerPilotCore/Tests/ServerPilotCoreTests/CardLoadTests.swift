import Testing
@testable import ServerPilotCore

struct CardLoadTests {
    @Test func aKeepaliveCardIsFreeWhateverMemoryItReserves() {
        #expect(CardLoad(state: "KEEPALIVE", utilization: 0.65, memory: 0.80) == .free)
        #expect(CardLoad(state: "AVAILABLE", utilization: nil, memory: nil) == .free)
    }

    @Test func eitherComputeOrMemoryCanMakeACardNearlyFull() {
        #expect(CardLoad(state: "RUNNING_MANAGED", utilization: 0.30, memory: 0.40) == .low)
        #expect(CardLoad(state: "RUNNING_MANAGED", utilization: 0.80, memory: 0.10) == .nearFull)
        #expect(CardLoad(state: "BUSY_UNMANAGED", utilization: 0.05, memory: 0.92) == .nearFull)
        #expect(CardLoad(state: "HELD", utilization: 0, memory: 0) == .low)
    }

    @Test func anAllocationNobodyMeasuredDoesNotReadAsLow() {
        #expect(CardLoad(state: "RUNNING_MANAGED", utilization: nil, memory: nil) == .unmeasured)
    }

    @Test func aFaultOutranksAnyReading() {
        #expect(CardLoad(state: "CONFLICT", utilization: 0.10, memory: 0.10) == .fault)
        #expect(CardLoad(state: "UNKNOWN_STALE", utilization: 0.90, memory: 0.90) == .stale)
    }
}
