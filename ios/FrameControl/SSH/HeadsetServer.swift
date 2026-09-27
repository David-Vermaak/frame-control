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
    /// Set once the server stops, with its last output.
    var exited: String? { lock.withLock { _exited } }
    var onExit: (@Sendable (String) -> Void)?

    private init(port: Int) { self.port = port }

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
        // Older versions: only this one is used from now on.
        _ = try? await link.run("cd \(cacheDir) && for d in */; do [ \"${d%/}\" = \(bundle.version) ] || rm -rf -- \"$d\"; done")
        return dir
    }

    /// Starts the server in dir and waits for it to say which port it took.
    static func start(in dir: String, over link: FrameLink, key: String, device: String) async throws -> HeadsetServer {
        let command = "cd \(dir) && FRAME_LOCAL=1 FRAME_UI_KEY=\(key) FRAME_DEVICE=\(shellQuote(device)) "
            + "exec python3 -I -u -B ui/server.py --port 0 --exit-on-eof 2>&1"
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
        box.onEnd = { [weak server] tail in
            guard let server else { return }
            server.lock.withLock { server._exited = tail }
            server.onExit?(tail)
        }
        return server
    }

    static func port(in text: String) -> Int? {
        guard let r = text.range(of: #"Frame Control on http://127\.0\.0\.1:(\d+)"#, options: .regularExpression) else { return nil }
        return Int(text[r].split(separator: ":").last ?? "")
    }
}

/// Hands the port from the output reader to start(), or the output if the server died first.
private final class PortWaiter: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Int, Error>?
    private var result: Result<Int, Error>?
    private var endedTail: String?
    var onEnd: (@Sendable (String) -> Void)? {
        didSet { if let tail = lock.withLock({ endedTail }) { onEnd?(tail) } }
    }

    func found(_ port: Int) { finish(.success(port)) }

    func ended(_ text: String) {
        let tail = String(text.suffix(600)).trimmingCharacters(in: .whitespacesAndNewlines)
        lock.withLock { endedTail = tail }
        finish(.failure(FrameFailure("Frame Control's server on the headset stopped: \(tail.isEmpty ? "no output" : tail)")))
        onEnd?(tail)
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
