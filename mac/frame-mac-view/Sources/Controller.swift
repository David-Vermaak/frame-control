// Thin, serialized binding to the same controller used by the PC host.
// Algorithm and bounded state live in desktop/controller.c.
import Foundation

final class RateController {
    static let enabled = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_ADAPT"] != "0"
    let maxFps: Int
    private let lock = NSLock()
    private let core: OpaquePointer
    private var events: [[String: Any]] = []
    init(maxFps: Int) {
        self.maxFps = maxFps
        core = fc_new(Int32(maxFps), Self.enabled ? 1 : 0)!
    }
    deinit { fc_free(core) }
    private func locked<T>(_ f: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return f() }
    private func value(_ field: Int32) -> Int { Int(fc_value(core, field)) }
    var target: Int { locked { value(0) } }
    var tier: Int { locked { value(2) } }
    var fps: Int { locked { value(3) } }
    var scale: Double { locked { Double(value(4)) / 100 } }
    var baseRtt: Int64 { locked { fc_value(core, 5) } }
    func setCeiling(_ bps: Int) { locked { fc_ceiling(core, Int32(bps)) } }
    func maySend(now: Int64, counts: Bool = true) -> Bool { locked { fc_gate(core, now, counts ? 1 : 0) != 0 } }
    func captured(at t: Int64) { locked { fc_capture(core, t) } }
    func sent(seq: UInt32, bytes: Int, at t: Int64) { locked { fc_sent(core, seq, Int32(bytes), t) } }
    func acked(seq: UInt32, at t: Int64) -> Bool { locked { fc_ack(core, seq, t) != 0 } }
    func update(now: Int64) -> Int? {
        locked {
            let old = value(0), tier = value(2)
            let result = Int(fc_update(core, now))
            if value(0) < old || value(2) != tier {
                events.append(["t": now, "e": "target \(value(0)) bit/s; tier \(value(2))"])
                if events.count > 200 { events.removeFirst() }
            }
            return result == 0 ? nil : result
        }
    }
    func state() -> [String: Any] {
        locked { ["target": value(0), "ceiling": value(1), "tier": value(2), "fps": value(3),
                  "scale": Double(value(4)) / 100, "baseRtt": Double(value(5)) / 1000,
                  "inFlight": value(6), "slack": Double(value(7)) / 1000, "adapt": Self.enabled] }
    }
    func eventList() -> [[String: Any]] { locked { events } }
}
