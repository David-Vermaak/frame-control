import Citadel
import Foundation
import SwiftUI
import UIKit

/// The app's one piece of state: which headset, and how far along connecting to it is.
@MainActor
final class AppModel: ObservableObject {
    enum Phase: Equatable {
        case setup
        case connecting(String)
        case ready(URL)
        case failed(String)
    }

    @Published private(set) var phase: Phase
    @Published private(set) var settings: FrameSettings?
    /// Install links that arrived before the page was ready for them.
    @Published var pendingInstallLinks: [InstallLink] = []

    private var link: FrameLink?
    private var server: HeadsetServer?
    private var forwarder: PortForwarder?
    private var attempt = 0

    private static let settingsKey = "frame.settings"
    private static let hostKeyKey = "frame.hostKey"

    init() {
        let saved = UserDefaults.standard.data(forKey: Self.settingsKey).flatMap { try? JSONDecoder().decode(FrameSettings.self, from: $0) }
        settings = saved
        phase = saved == nil ? .setup : .connecting("Connecting")
    }

    var deviceName: String { UIDevice.current.userInterfaceIdiom == .pad ? "iPad" : "iPhone" }
    private var hostKey: String? { UserDefaults.standard.string(forKey: Self.hostKeyKey) }

    // MARK: pairing

    /// First time: log in with the Developer Mode password, add this phone's key to
    /// ~/.ssh/authorized_keys, record the Frame's host key, then connect with the key.
    func pair(host: String, user: String, password: String) async {
        guard let target = Self.parse(host: host, user: user) else {
            fail("Enter the headset's address and user name.", retry: false)
            return
        }
        invalidate()
        let mine = attempt
        await teardown()
        guard mine == attempt else { return }
        phase = .connecting("Signing in to \(target.host)")
        let pin = PinnedHostKey(expected: nil)
        do {
            let link = try await FrameLink.connect(target, auth: .passwordBased(username: target.user, password: password), hostKey: pin)
            defer { Task { await link.close() } }
            guard mine == attempt else { return }
            phase = .connecting("Adding this \(deviceName)'s key")
            let line = authorizedKeysLine
            // A file whose last line has no newline would otherwise swallow the key.
            let file = "~/.ssh/authorized_keys"
            try await link.check("umask 077; mkdir -p ~/.ssh && touch \(file) && "
                                 + "{ grep -qxF \(shellQuote(line)) \(file) || { "
                                 + "[ -s \(file) ] && [ -n \"$(tail -c 1 \(file))\" ] && printf '\\n' >> \(file); "
                                 + "printf '%s\\n' \(shellQuote(line)) >> \(file); }; }",
                                 "Couldn't add the key on the Frame")
            guard mine == attempt else { return }  // cancelled meanwhile: save nothing
            guard let seen = pin.seen else { throw FrameFailure("The Frame didn't show a host key") }
            UserDefaults.standard.set(seen, forKey: Self.hostKeyKey)
            UserDefaults.standard.set(try JSONEncoder().encode(target), forKey: Self.settingsKey)
            settings = target
        } catch {
            guard mine == attempt else { return }
            let failure = error as? FrameFailure
            fail(failure?.message ?? FrameLink.describe(error, host: target.host), retry: false,
                 needsPairing: failure?.needsPairing ?? false)
            return
        }
        await connect()
    }

    /// For someone who added this phone's key to the Frame themselves: no password.
    /// The Frame's host key is recorded on this first connection.
    func useKey(host: String, user: String) async {
        guard let target = Self.parse(host: host, user: user) else {
            fail("Enter the headset's address and user name.", retry: false)
            return
        }
        UserDefaults.standard.removeObject(forKey: Self.hostKeyKey)
        UserDefaults.standard.set(try? JSONEncoder().encode(target), forKey: Self.settingsKey)
        settings = target
        await connect()
    }

    /// "host", "host:port" or "[v6]:port", plus a user name.
    static func parse(host: String, user: String) -> FrameSettings? {
        var target = FrameSettings(host: host.trimmingCharacters(in: .whitespaces), user: user.trimmingCharacters(in: .whitespaces))
        if target.host.hasPrefix("["), let close = target.host.firstIndex(of: "]") {
            let rest = target.host[target.host.index(after: close)...]
            if rest.hasPrefix(":"), let port = Int(rest.dropFirst()) { target.port = port }
            target.host = String(target.host[target.host.index(after: target.host.startIndex)..<close])
        } else if target.host.filter({ $0 == ":" }).count == 1, let colon = target.host.lastIndex(of: ":"),
                  let port = Int(target.host[target.host.index(after: colon)...]) {
            target.port = port
            target.host = String(target.host[..<colon])
        }
        guard !target.host.isEmpty, !target.user.isEmpty, (1...65535).contains(target.port) else { return nil }
        return target
    }

    /// This phone's line for ~/.ssh/authorized_keys on the Frame.
    var authorizedKeysLine: String {
        DeviceKey.authorizedKeysLine(DeviceKey.loadOrCreate(), comment: "frame-control@\(deviceName)")
    }

    /// Forget the headset: back to the pairing screen. The Frame keeps the key line;
    /// remove it from ~/.ssh/authorized_keys there to revoke this phone.
    func forget() async {
        invalidate()
        await teardown()
        UserDefaults.standard.removeObject(forKey: Self.settingsKey)
        UserDefaults.standard.removeObject(forKey: Self.hostKeyKey)
        settings = nil
        phase = .setup
    }

    func showSetup() {
        invalidate()
        Task { await teardown() }
        phase = .setup
    }

    /// Whether the failure screen is retrying on its own.
    @Published private(set) var retrying = false

    // MARK: connecting

    /// Every connection attempt has a number; anything that finishes after a newer
    /// attempt started (or the user went back to setup) closes what it made and stops.
    private func invalidate() {
        attempt += 1
        retrying = false
    }

    /// quiet: a background retry, which leaves the failure screen up until it works.
    func connect(quiet: Bool = false) async {
        guard let settings else {
            phase = .setup
            return
        }
        invalidate()
        let mine = attempt
        await teardown()
        func current() -> Bool { mine == attempt }
        func step(_ s: String) { if current() && !quiet { phase = .connecting(s) } }
        step("Connecting to \(settings.host)")
        var link: FrameLink?
        var forwarder: PortForwarder?
        do {
            let bundle = try HeadsetServer.Bundle.fromApp()
            let auth = SSHAuthenticationMethod.ed25519(username: settings.user, privateKey: DeviceKey.loadOrCreate())
            let pin = PinnedHostKey(expected: hostKey)
            let l = try await FrameLink.connect(settings, auth: auth, hostKey: pin)
            link = l
            guard current() else { throw CancellationError() }
            if hostKey == nil, let seen = pin.seen { UserDefaults.standard.set(seen, forKey: Self.hostKeyKey) }
            let dir = try await HeadsetServer.deploy(bundle, over: l) { s in Task { @MainActor in step(s) } }
            guard current() else { throw CancellationError() }
            step("Starting Frame Control on the headset")
            let key = Self.randomKey()
            let server = try await HeadsetServer.start(in: dir, over: l, key: key, device: deviceName)
            guard current() else { throw CancellationError() }
            let f = try await PortForwarder.start(over: l, to: server.port)
            forwarder = f
            guard current() else { throw CancellationError() }
            if let tail = server.exited {  // stopped while the tunnel was opening
                throw FrameFailure("Frame Control on the headset stopped. \(tail.suffix(200))")
            }
            // Only now does this attempt's connection become the app's.
            self.link = l
            self.server = server
            self.forwarder = f
            readySince = Date()
            var page = "http://127.0.0.1:\(f.localPort)/?key=\(key)"
            #if DEBUG
            // Test hooks for the Simulator: open on a given tab, and leave the URL where
            // a test can drive the same tunnel (`simctl get_app_container … data`).
            if let tab = ProcessInfo.processInfo.environment["FRAME_TEST_PAGE"] { page += "#\(tab)" }
            if let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first {
                try? page.write(to: dir.appendingPathComponent("frame-test-url.txt"), atomically: true, encoding: .utf8)
            }
            #endif
            phase = .ready(URL(string: page)!)
            // Runs at once if it stopped in the moment since the check above.
            server.whenExited { [weak self] tail in
                Task { @MainActor in self?.lost(mine, "Frame Control on the headset stopped. \(tail.suffix(200))") }
            }
            watchHealth(mine)
        } catch {
            forwarder?.stop()
            if let link { await link.close() }  // ends its server too
            guard current(), !(error is CancellationError) else { return }
            let failure = error as? FrameFailure
            fail(failure?.message ?? FrameLink.describe(error, host: settings.host), retry: !(failure?.needsPairing ?? false),
                 needsPairing: failure?.needsPairing ?? false)
        }
    }

    /// Whether the last failure needs the user to pair again rather than wait.
    @Published private(set) var needsPairing = false

    private func fail(_ message: String, retry: Bool, needsPairing: Bool = false) {
        self.needsPairing = needsPairing
        phase = .failed(message)
        retrying = retry && settings != nil
        guard retrying else { return }
        // Keep trying quietly while the app is open: the Frame may just be asleep.
        // A new task each time, so retrying for hours doesn't nest awaits.
        let mine = attempt
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            guard let self, mine == self.attempt, case .failed = self.phase,
                  UIApplication.shared.applicationState == .active else { return }
            await self.connect(quiet: true)
        }
    }

    /// While connected, check every 20 s that the SSH session still answers: a
    /// network change can leave it looking open while nothing gets through.
    private func watchHealth(_ mine: Int) {
        Task { [weak self] in
            while true {
                try? await Task.sleep(nanoseconds: 20_000_000_000)
                guard let self, mine == self.attempt, case .ready = self.phase else { return }
                if UIApplication.shared.applicationState != .active { continue }
                if await !(self.link?.answers() ?? false) {
                    guard mine == self.attempt else { return }
                    await self.connect(quiet: true)
                    return
                }
            }
        }
    }

    /// Called when the app comes back to the foreground: iOS may have dropped the
    /// connection, or left it looking open, while it was in the background.
    func resume() {
        switch phase {
        case .ready:
            let mine = attempt
            Task {
                let ok = await link?.answers() ?? false
                if (!ok || server?.exited != nil), mine == attempt { await connect() }
            }
        case .failed:
            // A changed identity or a refused login needs the user, not another try.
            if settings != nil, !needsPairing { Task { await connect() } }
        default:
            break
        }
    }

    private var readySince = Date.distantPast

    private func lost(_ which: Int, _ why: String) {
        guard which == attempt, case .ready = phase else { return }
        // Restart it once; if it dies again straight away, say so instead of looping.
        if Date().timeIntervalSince(readySince) < 20 {
            invalidate()
            Task { await teardown() }
            fail(why, retry: false)
        } else {
            Task { await connect() }
        }
    }

    private func teardown() async {
        forwarder?.stop()
        forwarder = nil
        server = nil
        if let link {
            self.link = nil
            await link.close()  // ends the server too: its stdin closes
        }
    }

    private static func randomKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}
