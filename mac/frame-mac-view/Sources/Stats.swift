// Per-frame timing, so every change to the stream can be judged by numbers.
// All times are the agent's host clock in microseconds (the clock
// ScreenCaptureKit stamps frames with). The viewer syncs to it over the
// WebSocket and reports when each frame arrived, decoded and was drawn.
import CoreMedia
import Foundation

private let timebase: mach_timebase_info_data_t = {
    var t = mach_timebase_info_data_t()
    mach_timebase_info(&t)
    return t
}()

/// Now on the host clock, in microseconds.
func nowUs() -> Int64 { hostUs(mach_absolute_time()) }

/// A mach_absolute_time value (as in SCStreamFrameInfo.displayTime) in microseconds.
func hostUs(_ ticks: UInt64) -> Int64 { Int64(ticks / 1000 * UInt64(timebase.numer) / UInt64(timebase.denom)) }

/// A host-clock CMTime (ScreenCaptureKit's presentation times) in microseconds.
func hostUs(_ t: CMTime) -> Int64 { t.isValid ? Int64(CMTimeGetSeconds(t) * 1_000_000) : nowUs() }

/// One frame's journey. Zero means "didn't happen" or "not reported yet".
struct FrameRecord {
    var seq: UInt32 = 0
    var key = false
    var bytes = 0
    var width = 0, height = 0
    var capture: Int64 = 0  // the Mac composited it (display time)
    var arrived: Int64 = 0  // ScreenCaptureKit handed it to us
    var encodeStart: Int64 = 0
    var encodeEnd: Int64 = 0
    var sent: Int64 = 0  // handed to the WebSocket
    var wire: Int64 = 0  // the socket took it
    var received: Int64 = 0  // viewer, agent clock
    var decoded: Int64 = 0
    var drawn: Int64 = 0
    var vsync: Int64 = 0  // the viewer's next animation frame after drawing it
    var echo: UInt32 = 0  // the first frame after input `echo`
    var tier = 0
    var bitrate = 0

    var json: [String: Any] {
        ["s": seq, "k": key ? 1 : 0, "b": bytes, "w": width, "h": height, "cap": capture, "arr": arrived,
         "e0": encodeStart, "e1": encodeEnd, "snd": sent, "wire": wire, "rx": received, "dec": decoded,
         "drw": drawn, "vs": vsync, "echo": echo, "tier": tier, "br": bitrate]
    }
}

/// One input event from the viewer and what came of it.
struct InputRecord {
    var id: UInt32
    var kind: String
    var viewer: Int64  // the viewer's event time, agent clock
    var injected: Int64 = 0  // posted as a CGEvent
    var frame: UInt32 = 0  // first frame captured after it (0: none yet)
    var capture: Int64 = 0

    var json: [String: Any] {
        ["id": id, "kind": kind, "tv": viewer, "inj": injected, "frame": frame, "cap": capture]
    }
}

func percentile(_ sorted: [Double], _ p: Double) -> Double? {
    guard !sorted.isEmpty else { return nil }
    let i = min(sorted.count - 1, max(0, Int((p * Double(sorted.count - 1)).rounded())))
    return sorted[i]
}

/// Frames and inputs of one stream, kept for the last few thousand frames.
/// Any thread.
final class StreamStats {
    static let capacity = 4096
    private let lock = NSLock()
    private var frames = [FrameRecord?](repeating: nil, count: StreamStats.capacity)
    private var inputs: [UInt32: InputRecord] = [:]
    private var inputOrder: [UInt32] = []
    private(set) var nextSeq: UInt32 = 1
    var captured = 0  // frames ScreenCaptureKit delivered
    var skipped = 0  // held back from encoding (link, encoder, pacing or gate busy); only the newest may go later
    var viewerDropped = 0  // not shown by the viewer (behind, or waiting for a keyframe)
    var rtt: Double = 0  // ms, the viewer's best recent ping
    var clockSynced = false
    var decoder = ""  // what the viewer reports about its decoder

    func withLock<T>(_ f: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return f() }

    func newFrame(_ r: FrameRecord) -> UInt32 {
        withLock {
            var r = r
            r.seq = nextSeq
            nextSeq &+= 1
            frames[Int(r.seq) % StreamStats.capacity] = r
            return r.seq
        }
    }

    func update(_ seq: UInt32, _ f: (inout FrameRecord) -> Void) {
        withLock {
            let i = Int(seq) % StreamStats.capacity
            guard var r = frames[i], r.seq == seq else { return }
            f(&r)
            frames[i] = r
        }
    }

    func frame(_ seq: UInt32) -> FrameRecord? {
        withLock {
            let r = frames[Int(seq) % StreamStats.capacity]
            return r?.seq == seq ? r : nil
        }
    }

    func addInput(_ r: InputRecord) {
        withLock {
            inputs[r.id] = r
            inputOrder.append(r.id)
            if inputOrder.count > 512 { inputs[inputOrder.removeFirst()] = nil }
        }
    }

    func updateInput(_ id: UInt32, _ f: (inout InputRecord) -> Void) {
        withLock { if var r = inputs[id] { f(&r); inputs[id] = r } }
    }

    /// Frames with seq > `since` that were sent at least `settle` µs ago, so
    /// the viewer has had time to report on them.
    func settled(since: UInt32, settle: Int64 = 1_500_000) -> [FrameRecord] {
        let cutoff = nowUs() - settle
        return withLock {
            frames.compactMap { $0 }.filter { $0.seq > since && $0.sent > 0 && $0.sent < cutoff }.sorted { $0.seq < $1.seq }
        }
    }

    func inputList() -> [InputRecord] { withLock { inputOrder.compactMap { inputs[$0] } } }

    /// Percentiles over the last `window` µs, for /status and the overlay.
    func summary(window: Int64 = 2_000_000) -> [String: Any] {
        let now = nowUs(), from = now - window
        let recent = withLock { frames.compactMap { $0 }.filter { $0.sent > from } }
        func ms(_ pick: (FrameRecord) -> (Int64, Int64)) -> [String: Double] {
            let v = recent.compactMap { r -> Double? in
                let (a, b) = pick(r)
                return a > 0 && b > 0 ? Double(b - a) / 1000 : nil
            }.sorted()
            guard !v.isEmpty else { return [:] }
            return ["p50": (percentile(v, 0.5)! * 10).rounded() / 10, "p95": (percentile(v, 0.95)! * 10).rounded() / 10]
        }
        let secs = Double(window) / 1_000_000
        let shown = recent.filter { $0.vsync > 0 }.count
        let bytes = recent.reduce(0) { $0 + $1.bytes }
        var s: [String: Any] = [
            "capture": ms { ($0.capture, $0.arrived) },
            "queue": ms { ($0.arrived, $0.encodeStart) },
            "encode": ms { ($0.encodeStart, $0.encodeEnd) },
            "network": ms { ($0.encodeEnd, $0.received) },
            "decode": ms { ($0.received, $0.decoded) },
            "draw": ms { ($0.decoded, $0.vsync) },
            "total": ms { ($0.capture, $0.vsync) },
            "fps": (Double(shown) / secs * 10).rounded() / 10,
            "sentFps": (Double(recent.count) / secs * 10).rounded() / 10,
            "mbps": (Double(bytes * 8) / secs / 1e5).rounded() / 10,
            "rtt": (rtt * 10).rounded() / 10,
            "synced": clockSynced,
        ]
        withLock {
            s["captured"] = captured
            s["skipped"] = skipped
            s["dropped"] = viewerDropped
            s["decoder"] = decoder
            let done = inputOrder.suffix(20).compactMap { inputs[$0] }.compactMap { i -> Double? in
                guard i.frame != 0, let f = frames[Int(i.frame) % StreamStats.capacity], f.seq == i.frame, f.vsync > 0
                else { return nil }
                return Double(f.vsync - i.viewer) / 1000
            }.sorted()
            if !done.isEmpty { s["input"] = ["p50": percentile(done, 0.5)!, "n": done.count] }
        }
        return s
    }
}
