// frame-mac-view: streams Mac windows or displays to viewers on the Steam
// Frame and plays their pointer and key input back on the Mac.
//
//   frame-mac-view serve --port 47811 --page ui/mac-view.html   (token in FRAME_MAC_VIEW_TOKEN)
//   frame-mac-view windows | displays | permissions              (JSON on stdout)
//   frame-mac-view request-permissions                           (shows macOS's prompts)
//
// It listens on 127.0.0.1 only. Frame Control reaches it from the Frame
// through an SSH reverse tunnel, and every request must carry the token.
//
// HTTP. Frame Control's key (?k=, from FRAME_MAC_VIEW_TOKEN) never leaves the
// Mac; the Frame gets a single-use ticket per viewer instead.
//   GET  /ping, /view                     open: tunnel check, viewer page
//   GET  /stream?src=...&t=TICKET|r=KEY   WebSocket (&codec=h264|jpeg&max=&fps=&bpp=)
//   GET  /status, /windows, /displays     ?k=: permissions, streams, sources
//   POST /ticket?src=...                  ?k=: a ticket for one viewer of src
//   POST /close[?src=...]                 ?k=: end those streams, close their windows
//   POST /permissions                     ?k=: show macOS's permission prompts
// src is window:<CGWindowID>, display:<CGDirectDisplayID> or test.
import AppKit
import ApplicationServices
import Foundation
import IOKit.pwr_mgt
import ScreenCaptureKit
import Security

let version = "1"

func windowsJSON() -> [[String: Any]] {
    let me = ProcessInfo.processInfo.processIdentifier
    let skip: Set<String> = ["Window Server", "Dock", "Control Centre", "Control Center", "Notification Centre",
                             "Notification Center", "Spotlight", "SystemUIServer", "Wallpaper", "WindowManager"]
    return WindowInfo.all().filter {
        $0.layer == 0 && $0.pid != me && !skip.contains($0.app) && $0.bounds.width >= 120 && $0.bounds.height >= 80
    }.map {
        ["id": $0.id, "src": "window:\($0.id)", "pid": $0.pid, "app": $0.app, "title": $0.title,
         "w": Int($0.bounds.width), "h": Int($0.bounds.height)]
    }
}

func displaysJSON() -> [[String: Any]] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 16)
    var n: UInt32 = 0
    // Online, not active: a display that's asleep is still one you can stream.
    CGGetOnlineDisplayList(16, &ids, &n)
    return ids.prefix(Int(n)).filter { CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay }.map { id in
        let b = CGDisplayBounds(id)
        return ["id": id, "src": "display:\(id)", "name": displayName(id), "w": Int(b.width), "h": Int(b.height),
                "main": CGDisplayIsMain(id) != 0]
    }
}

func permissionsJSON() -> [String: Any] {
    ["screen": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted()]
}

func printJSON(_ obj: Any) {
    let d = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted])) ?? Data()
    FileHandle.standardOutput.write(d + Data("\n".utf8))
}

/// One viewer watching one source.
final class Session {
    let id: Int
    let source: Source
    let capture: CaptureSource
    let encoder: Encoder
    let ws: WebSocket
    let codec: Codec
    let reconnectKey: String
    private let lock = NSLock()
    private var last: (CVPixelBuffer, CMTime)?
    private var skipped = false
    private var stopped = false
    private var inFlight = 0
    /// Frames already queued for the network; beyond this, or with two frames
    /// already in the encoder, new frames are skipped (before encoding, so no
    /// reference frame goes missing) and the newest picture is sent once
    /// things catch up. Only that newest picture is kept meanwhile.
    let maxPending: Int
    static let maxInFlight = 2
    var onEnd: ((Session) -> Void)?
    var onAck: (() -> Void)?
    var onFinished: ((String) -> Void)?
    private let encodeQueue = DispatchQueue(label: "frame-mac-view.encode", qos: .userInteractive)

    init(id: Int, source: Source, capture: CaptureSource, ws: WebSocket, codec: Codec, fps: Int, bitsPerPixel: Double,
         reconnectKey: String) {
        self.id = id
        self.source = source
        self.capture = capture
        self.ws = ws
        self.codec = codec
        self.reconnectKey = reconnectKey
        encoder = Encoder(codec: codec, fps: fps, bitsPerPixel: bitsPerPixel)
        maxPending = codec == .jpeg ? 3 << 20 : 1 << 20
    }

    /// Encodes `pb` unless the link or the encoder is busy, in which case it
    /// becomes the picture to send next.
    private func offer(_ pb: CVPixelBuffer, pts: CMTime) {
        lock.lock()
        last = (pb, pts)
        let busy = stopped || ws.pendingBytes > maxPending || inFlight >= Session.maxInFlight
        if busy { skipped = true } else { inFlight += 1 }
        lock.unlock()
        if !busy { submit(pb, pts: pts) }
    }

    private func submit(_ pb: CVPixelBuffer, pts: CMTime) {
        encodeQueue.async {
            if !self.encoder.encode(pb, pts: pts) { self.finished() }
        }
    }

    /// An encode finished (or failed): send the newest skipped picture if
    /// there's room now.
    private func finished() {
        lock.lock()
        inFlight = max(0, inFlight - 1)
        lock.unlock()
        resendIfRoom()
    }

    private func resendIfRoom() {
        lock.lock()
        var next: (CVPixelBuffer, CMTime)?
        if !stopped, skipped, ws.pendingBytes <= maxPending / 2, inFlight < Session.maxInFlight, let l = last {
            next = l
            skipped = false
            inFlight += 1
        }
        lock.unlock()
        if let (pb, _) = next { submit(pb, pts: CMClockGetTime(CMClockGetHostTimeClock())) }
    }

    func start(maxLong: Int, fps: Int) {
        encoder.onFrame = { [weak self] data, key, pts in
            guard let self else { return }
            var msg = Data([key ? 1 : 0])
            var us = UInt64(max(0, CMTimeGetSeconds(pts)) * 1_000_000).bigEndian
            msg.append(Data(bytes: &us, count: 8))
            msg.append(data)
            self.ws.sendBinary(msg)
        }
        encoder.onDone = { [weak self] in self?.finished() }
        encoder.onError = { [weak self] message in self?.ws.sendJSON(["t": "error", "message": message]) }
        capture.onFrame = { [weak self] pb, pts in self?.offer(pb, pts: pts) }
        capture.onEnded = { [weak self] reason in
            guard let self else { return }
            self.ws.sendJSON(["t": "closed", "reason": reason])
            self.onFinished?(self.source.key)
            self.end()
        }
        ws.onDrain = { [weak self] in self?.resendIfRoom() }
        ws.onText = { [weak self] text in self?.handle(text) }
        ws.onClose = { [weak self] in self?.end() }
        ws.start()
        // Reconnect with this (kept in the page's memory, never on a command line).
        ws.sendJSON(["t": "hello", "r": reconnectKey])
        if let sck = capture as? SCKSource {
            sck.onChange = { [weak self] in self?.sendInfo() }
        }
        capture.start(maxLong: maxLong, fps: fps) { [weak self] error in
            guard let self else { return }
            if let error {
                if (self.capture as? SCKSource)?.gone == true {
                    // Final, like a window closing mid-stream: the viewer closes
                    // and its key is revoked, rather than retrying for ever.
                    self.ws.sendJSON(["t": "closed", "reason": error])
                    self.onFinished?(self.source.key)
                } else {
                    self.ws.sendJSON(["t": "error", "message": error])
                }
                self.end()
                return
            }
            self.sendInfo()
        }
    }

    func sendInfo() {
        ws.sendJSON(["t": "info", "src": source.key, "title": capture.title, "app": capture.app, "codec": codec.rawValue,
                     "input": source == .test || Input.allowed,
                     "aspect": Double(capture.frameRect.width / max(capture.frameRect.height, 1))])
    }

    var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }

    func end() {
        lock.lock()
        let was = stopped
        stopped = true
        last = nil
        lock.unlock()
        guard !was else { return }
        capture.stop()
        encodeQueue.async { self.encoder.invalidate() }
        let owner = id
        DispatchQueue.main.async { Input.releaseAll(owner: owner) }
        ws.close()
        onEnd?(self)
    }

    /// A point in the picture (0...1 each way) -> Mac global coordinates.
    private func point(_ x: Double, _ y: Double) -> CGPoint {
        let r = capture.frameRect
        return CGPoint(x: r.minX + min(max(x, 0), 1) * r.width, y: r.minY + min(max(y, 0), 1) * r.height)
    }

    private func handle(_ text: String) {
        guard !isStopped, let d = text.data(using: .utf8),
              let m = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let t = m["t"] as? String else { return }
        let x = m["x"] as? Double ?? -1, y = m["y"] as? Double ?? -1
        if t == "ack" {
            onAck?()
            onAck = nil
            return
        }
        if t == "key-frame" {
            encodeQueue.async { self.encoder.requestKeyFrame() }  // before the resend, same queue
            lock.lock(); skipped = last != nil; lock.unlock()
            resendIfRoom()
            return
        }
        if source == .test {
            let desc: String?
            switch t {
            case "m": desc = (m["e"] as? String) == "move" ? nil : "mouse \(m["e"] ?? "") button \(m["b"] ?? 0)"
            case "wheel": desc = "wheel \(m["dx"] ?? 0), \(m["dy"] ?? 0)"
            case "k": desc = (m["e"] as? String) == "down" ? "key \(m["code"] ?? "") \"\(m["key"] ?? "")\"" : nil
            case "text": desc = "text \"\(m["s"] ?? "")\""
            default: desc = nil
            }
            capture.pointer(x: x, y: y, text: desc)
            return
        }
        DispatchQueue.main.async { [self] in
            guard !isStopped else { return }  // Stop revokes input at once
            switch t {
            case "m":
                let kind = m["e"] as? String ?? "move", b = m["b"] as? Int ?? 0
                let p = point(x, y)
                if kind == "down", case .window(let wid) = source, let pid = capture.pid {
                    Input.focus(window: wid, pid: pid)
                }
                Input.mouse(kind, button: b, at: p, owner: id)
            case "wheel":
                Input.scroll(dx: m["dx"] as? Double ?? 0, dy: m["dy"] as? Double ?? 0, at: point(x, y), owner: id)
            case "k":
                Input.key(code: m["code"] as? String ?? "", key: m["key"] as? String ?? "",
                          down: (m["e"] as? String) == "down", mods: m["mods"] as? [String] ?? [], owner: id)
            case "text":
                if let s = m["s"] as? String, s.count <= 4096 { Input.text(s) }
            case "release":
                Input.releaseAll(owner: id)
            case "focus":
                if case .window(let wid) = source, let pid = capture.pid { Input.focus(window: wid, pid: pid) }
            default: break
            }
        }
    }
}

func randomKey() -> String {
    var bytes = [UInt8](repeating: 0, count: 24)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}

func sameSecret(_ a: String, _ b: String) -> Bool {
    guard !a.isEmpty, a.utf8.count == b.utf8.count else { return false }
    return zip(a.utf8, b.utf8).reduce(0, { $0 | ($1.0 ^ $1.1) }) == 0
}

final class Agent {
    /// Frame Control's own key. It stays on the Mac: the Frame only ever sees
    /// single-use tickets and per-stream reconnect keys, both tied to one source.
    let token: String
    let page: URL?
    var sessions: [Int: Session] = [:] { didSet { keepDisplayAwake(!sessions.isEmpty) } }
    var nextId = 1
    let lock = NSLock()
    private var assertion: IOPMAssertionID = 0
    /// ticket -> (source, expiry, reconnect key once redeemed). A ticket opens
    /// one viewer within a minute; it may be redeemed again, for the same key,
    /// only until that viewer confirms it has the key ("ack").
    private var tickets: [String: (src: String, expiry: Date, key: String?)] = [:]
    /// reconnect key -> source, until Stop for that source.
    private var reconnectKeys: [String: String] = [:]
    /// Sources that ended for good (the window closed), for Frame Control.
    private var finished = Set<String>()

    /// While anyone watches, keep the Mac's display on: a sleeping display
    /// stops being drawn, so there'd be nothing to capture (and it would lock).
    private func keepDisplayAwake(_ on: Bool) {
        if on, assertion == 0 {
            IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                        IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                        "Frame Control is showing this Mac in a Steam Frame" as CFString, &assertion)
        } else if !on, assertion != 0 {
            IOPMAssertionRelease(assertion)
            assertion = 0
        }
    }

    init(token: String, page: URL?) {
        self.token = token
        self.page = page
    }

    /// Whether this request may open a stream of `src`: Frame Control's key,
    /// an unused ticket for that source, or a live reconnect key for it.
    private func mayStream(_ req: Request, src: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        let now = Date()
        tickets = tickets.filter { $0.value.expiry > now }
        if sameSecret(req.query["k"] ?? "", token) { return randomKey() }
        if let t = req.query["t"], let entry = tickets[t], entry.src == src {
            // A retry before the viewer got its key gets the same key back.
            let key = entry.key ?? randomKey()
            tickets[t] = (src, entry.expiry, key)
            reconnectKeys[key] = src
            return key
        }
        if let r = req.query["r"], reconnectKeys[r] == src { return r }
        return nil
    }

    /// The viewer has its reconnect key, so the ticket that led to it can't
    /// be used again (also when the ack comes over a reconnection).
    func acknowledged(key: String) {
        lock.lock(); tickets = tickets.filter { $0.value.key != key }; lock.unlock()
    }

    func handle(_ req: Request, _ c: HTTPConnection) {
        // Open to anything that reaches the port: a liveness check for the
        // tunnel, and the viewer page, which holds no secrets.
        switch (req.method, req.path) {
        case ("GET", "/ping"): return c.respond(200, text: "frame-mac-view")
        case ("GET", "/view"):
            guard let page, let body = try? Data(contentsOf: page) else { return c.respond(404, text: "no viewer page") }
            return c.respond(200, body: body, type: "text/html; charset=utf-8")
        case ("GET", "/stream"):
            guard let src = Source(req.query["src"] ?? "") else { return c.respond(400, text: "bad src") }
            guard let key = mayStream(req, src: src.key) else { return c.respond(403, text: "forbidden") }
            return stream(req, c, src: src, key: key)
        default: break
        }
        guard sameSecret(req.query["k"] ?? req.headers["x-token"] ?? "", token) else {
            return c.respond(403, text: "forbidden")
        }
        switch (req.method, req.path) {
        case ("GET", "/status"):
            lock.lock()
            let list = sessions.values.map { ["id": $0.id, "src": $0.source.key, "title": $0.capture.title, "app": $0.capture.app] }
            lock.unlock()
            var s = permissionsJSON()
            s["version"] = version
            s["streams"] = list
            lock.lock(); s["finished"] = Array(finished); lock.unlock()
            c.respond(json: s)
        case ("GET", "/windows"):
            c.respond(json: ["windows": windowsJSON(), "screen": CGPreflightScreenCaptureAccess()])
        case ("GET", "/displays"):
            c.respond(json: ["displays": displaysJSON()])
        case ("POST", "/ticket"):
            guard let src = Source(req.query["src"] ?? "") else { return c.respond(400, text: "bad src") }
            let t = randomKey()
            lock.lock()
            tickets[t] = (src.key, Date().addingTimeInterval(60), nil)
            finished.remove(src.key)
            lock.unlock()
            c.respond(json: ["ticket": t])
        case ("POST", "/close"):
            // Tell viewers to close their windows, but don't rely on them:
            // the sessions end here and can't reconnect.
            let src = req.query["src"]
            lock.lock()
            let matching = sessions.values.filter { src == nil || $0.source.key == src }
            reconnectKeys = reconnectKeys.filter { src != nil && $0.value != src }
            tickets = tickets.filter { src != nil && $0.value.src != src }  // not yet used ones too
            lock.unlock()
            for s in matching { s.ws.sendJSON(["t": "close"]) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { for s in matching { s.end() } }
            c.respond(json: ["closed": matching.count])
        case ("POST", "/permissions"):
            DispatchQueue.main.async { requestPermissions() }
            c.respond(json: permissionsJSON())
        default:
            c.respond(404, text: "not found")
        }
    }

    private func stream(_ req: Request, _ c: HTTPConnection, src: Source, key: String) {
        let codec = Codec(rawValue: req.query["codec"] ?? "h264") ?? .h264
        let maxLong = min(max(Int(req.query["max"] ?? "") ?? 1920, 320), 3840)
        let fps = min(max(Int(req.query["fps"] ?? "") ?? 60, 5), 120)
        let bpp = min(max(Double(req.query["bpp"] ?? "") ?? 0.1, 0.02), 0.5)
        guard let ws = c.upgrade(req) else { return }
        let capture: CaptureSource = src == .test ? TestSource() : SCKSource(src)
        lock.lock()
        // One live viewer per key: a reconnection (or a second use of an
        // unacknowledged ticket) replaces the one before.
        let replaced = sessions.values.filter { $0.reconnectKey == key }
        let session = Session(id: nextId, source: src, capture: capture, ws: ws, codec: codec, fps: fps,
                              bitsPerPixel: bpp, reconnectKey: key)
        nextId += 1
        sessions[session.id] = session
        lock.unlock()
        for old in replaced { old.end() }
        session.onEnd = { [weak self] s in
            guard let self else { return }
            self.lock.lock(); self.sessions[s.id] = nil; self.lock.unlock()
        }
        // The source is gone (its window closed): its viewers can't come back.
        session.onFinished = { [weak self] src in
            guard let self else { return }
            self.lock.lock()
            self.finished.insert(src)
            self.reconnectKeys = self.reconnectKeys.filter { $0.value != src }
            self.lock.unlock()
        }
        session.onAck = { [weak self] in self?.acknowledged(key: key) }
        session.start(maxLong: maxLong, fps: fps)
    }
}

func requestPermissions() {
    if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
    if !AXIsProcessTrusted() {
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    }
}

func argument(_ name: String, in args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "windows":
    printJSON(["windows": windowsJSON(), "screen": CGPreflightScreenCaptureAccess()])
case "displays":
    printJSON(["displays": displaysJSON()])
case "permissions":
    printJSON(permissionsJSON())
case "request-permissions":
    requestPermissions()
    printJSON(permissionsJSON())
case "serve":
    let port = UInt16(argument("--port", in: args) ?? "") ?? 0
    let token = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_TOKEN"] ?? ""
    guard !token.isEmpty else {
        FileHandle.standardError.write(Data("frame-mac-view: set FRAME_MAC_VIEW_TOKEN\n".utf8))
        exit(2)
    }
    let page = argument("--page", in: args).map { URL(fileURLWithPath: $0) }
    let agent = Agent(token: token, page: page)
    // Quit when the parent goes away (it holds our stdin open).
    if args.contains("--exit-on-eof") {
        DispatchQueue.global().async {
            while FileHandle.standardInput.availableData.count > 0 {}
            exit(0)
        }
    }
    let server: Server
    do {
        server = try Server(port: port) { req, c in agent.handle(req, c) }
    } catch {
        FileHandle.standardError.write(Data("frame-mac-view: \(error)\n".utf8))
        exit(1)
    }
    server.start { [server] error in
        if let error {
            FileHandle.standardError.write(Data("frame-mac-view: can't listen on 127.0.0.1:\(port): \(error)\n".utf8))
            exit(1)
        }
        print("frame-mac-view listening on 127.0.0.1:\(server.port ?? port)")
        fflush(stdout)
    }
    _ = NSApplication.shared  // AppKit for NSScreen names and app activation
    withExtendedLifetime(server) { RunLoop.main.run() }
default:
    FileHandle.standardError.write(Data("usage: frame-mac-view serve|windows|displays|permissions|request-permissions\n".utf8))
    exit(2)
}
