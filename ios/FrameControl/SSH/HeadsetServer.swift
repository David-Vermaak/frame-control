import Citadel
import Foundation
import NIOCore

/// Frame Control's server, running on the Frame itself. The app copies the bundle
/// (ios/scripts/make_frame_bundle.py) to ~/.cache/frame-control/<version> once per
/// version, then starts ui/server.py there over SSH. It listens only on the Frame's
/// 127.0.0.1, and it exits when this SSH session ends (--exit-on-eof).
final class HeadsetServer: @unchecked Sendable {
    let port: Int
    private let lock = NSLock()
    private var _exited: String?
    private var onExit: (@Sendable (String) -> Void)?
    /// Set once the server stops, with its last output.
    var exited: String? { lock.withLock { _exited } }

    private init(port: Int) { self.port = port }

    /// Calls back once when the server stops, at once if it already has.
    func whenExited(_ callback: @escaping @Sendable (String) -> Void) {
        let already: String? = lock.withLock {
            if _exited == nil { onExit = callback }
            return _exited
        }
        if let already { callback(already) }
    }

    fileprivate func markExited(_ tail: String) {
        let callback: (@Sendable (String) -> Void)? = lock.withLock {
            guard _exited == nil else { return nil }
            _exited = tail
            defer { onExit = nil }
            return onExit
        }
        callback?(tail)
    }

    static let cacheDir = ".cache/frame-control"

    struct Bundle {
        let data: Data
        let version: String

        static func fromApp() throws -> Bundle {
            guard let url = Foundation.Bundle.main.url(forResource: "frame-bundle", withExtension: "tar.gz"),
                  let data = try? Data(contentsOf: url),
                  let vurl = Foundation.Bundle.main.url(forResource: "frame-bundle", withExtension: "version"),
                  let version = try? String(contentsOf: vurl, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
                  version.range(of: "^[0-9a-f]{16}$", options: .regularExpression) != nil else {
                throw FrameFailure("This build of the app is missing its Frame bundle")
            }
            return Bundle(data: data, version: version)
        }
    }

    /// Copies the bundle over unless this version is already there; removes older versions.
    static func deploy(_ bundle: Bundle, over link: FrameLink, progress: @escaping @Sendable (String) -> Void) async throws -> String {
        let dir = "\(cacheDir)/\(bundle.version)"
        let py = try await link.run("command -v python3 >/dev/null && python3 -c 'import sys; print(sys.version_info >= (3, 8))'")
        guard py.status == 0, py.output.hasSuffix("True") else {
            throw FrameFailure("The Frame has no Python 3.8 or later, which Frame Control needs there.")
        }
        if try await link.run("test -f \(dir)/ui/server.py").status != 0 {
            progress("Copying Frame Control to the headset")
            try await link.check("mkdir -p \(cacheDir)", "Couldn't make \(cacheDir)")
            let archive = "\(dir).tar.gz"
            try await link.upload(bundle.data, to: archive)
            progress("Unpacking")
            try await link.check("rm -rf \(dir).tmp && mkdir \(dir).tmp && tar xzf \(archive) -C \(dir).tmp && rm -f \(archive) "
                                 + "&& rm -rf \(dir) && mv \(dir).tmp \(dir)", "Couldn't unpack Frame Control on the headset")
        }
        // Another phone or iPad may be running a different version right now: a version
        // goes only when no server runs from it and it hasn't been used for two weeks
        // (this one is marked as used). Servers run by absolute path, so pgrep sees it.
        _ = try? await link.run("touch \(dir) && cd \(cacheDir) && for d in */; do d=${d%/}; "
                                + "[ \"$d\" = \(bundle.version) ] && continue; "
                                + "[ -n \"$(find \"$d\" -maxdepth 0 -mtime +14)\" ] || continue; "
                                + "pgrep -f \"$PWD/$d/\" >/dev/null && continue; rm -rf -- \"$d\"; done")
        return dir
    }

    /// Starts the server in dir and waits for it to say which port it took.
    static func start(in dir: String, over link: FrameLink, key: String, device: String) async throws -> HeadsetServer {
        let command = "cd \(dir) && FRAME_LOCAL=1 FRAME_UI_KEY=\(key) FRAME_DEVICE=\(shellQuote(device)) "
            + "exec python3 -I -u -B \"$PWD/ui/server.py\" --port 0 --exit-on-eof 2>&1"
        let stream = try await link.client.executeCommandStream(command)
        let box = PortWaiter()
        let reader = Task { () -> Void in
            var text = ""
            do {
                for try await chunk in stream {
                    switch chunk {
                    case .stdout(let b), .stderr(let b): text += String(buffer: b)
                    }
                    if text.count > 20_000 { text = String(text.suffix(10_000)) }
                    if let port = Self.port(in: text) { box.found(port) }
                }
            } catch {
                text += "\n\(error)"
            }
            box.ended(text)
        }
        let server: HeadsetServer
        do {
            server = HeadsetServer(port: try await box.wait(seconds: 30))
        } catch {
            reader.cancel()
            throw error
        }
        box.whenEnded { [weak server] tail in server?.markExited(tail) }
        return server
    }

    /// The port from the server's first line. Output arrives in chunks, so the digits
    /// only count once something follows them (the line goes on after the port).
    static func port(in text: String) -> Int? {
        guard let r = text.range(of: #"Frame Control on http://127\.0\.0\.1:[0-9]+\s"#, options: .regularExpression),
              let port = Int(text[r].dropLast().split(separator: ":").last ?? ""), (1...65535).contains(port) else { return nil }
        return port
    }
}

/// Hands the port from the output reader to start(), or the output if the server died first.
private final class PortWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Int, Error>?
    private var result: Result<Int, Error>?
    private var endedTail: String?
    private var onEnd: (@Sendable (String) -> Void)?

    /// Calls back when the output ends, at once if it already has.
    func whenEnded(_ callback: @escaping @Sendable (String) -> Void) {
        let already: String? = lock.withLock {
            if endedTail == nil { onEnd = callback }
            return endedTail
        }
        if let already { callback(already) }
    }

    func found(_ port: Int) { finish(.success(port)) }

    func ended(_ text: String) {
        let tail = String(text.suffix(600)).trimmingCharacters(in: .whitespacesAndNewlines)
        let callback: (@Sendable (String) -> Void)? = lock.withLock {
            endedTail = tail
            defer { onEnd = nil }
            return onEnd
        }
        finish(.failure(FrameFailure("Frame Control's server on the headset stopped: \(tail.isEmpty ? "no output" : tail)")))
        callback?(tail)
    }

    private func finish(_ r: Result<Int, Error>) {
        let c: CheckedContinuation<Int, Error>? = lock.withLock {
            guard result == nil else { return nil }
            result = r
            defer { continuation = nil }
            return continuation
        }
        c?.resume(with: r)
    }

    func wait(seconds: Double) async throws -> Int {
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
            self?.finish(.failure(FrameFailure("Frame Control's server on the headset didn't start within \(Int(seconds)) s")))
        }
        return try await withCheckedThrowingContinuation { c in
            let done: Result<Int, Error>? = lock.withLock {
                if let result { return result }
                continuation = c
                return nil
            }
            if let done { c.resume(with: done) }
        }
    }
}
