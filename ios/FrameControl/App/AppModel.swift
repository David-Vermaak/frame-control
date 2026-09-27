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
            phase = .failed("Enter the headset's address and user name.")
            return
        }
        await teardown()
        attempt += 1
        let mine = attempt
        phase = .connecting("Signing in to \(target.host)")
        let pin = PinnedHostKey(expected: nil)
        do {
            let link = try await FrameLink.connect(target, auth: .passwordBased(username: target.user, password: password), hostKey: pin)
            defer { Task { await link.close() } }
            guard mine == attempt else { return }
            phase = .connecting("Adding this \(deviceName)'s key")
            let line = authorizedKeysLine
            try await link.check("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
                                 + "(grep -qxF \(shellQuote(line)) ~/.ssh/authorized_keys || echo \(shellQuote(line)) >> ~/.ssh/authorized_keys)",
                                 "Couldn't add the key on the Frame")
            guard let seen = pin.seen else { throw FrameFailure("The Frame didn't show a host key") }
            UserDefaults.standard.set(seen, forKey: Self.hostKeyKey)
            UserDefaults.standard.set(try JSONEncoder().encode(target), forKey: Self.settingsKey)
            settings = target
        } catch {
            guard mine == attempt else { return }
            phase = .failed((error as? FrameFailure)?.message ?? FrameLink.describe(error, host: target.host))
            return
        }
        await connect()
    }

    /// For someone who added this phone's key to the Frame themselves: no password.
    /// The Frame's host key is recorded on this first connection.
    func useKey(host: String, user: String) async {
        guard let target = Self.parse(host: host, user: user) else {
            phase = .failed("Enter the headset's address and user name.")
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
        await teardown()
        UserDefaults.standard.removeObject(forKey: Self.settingsKey)
        UserDefaults.standard.removeObject(forKey: Self.hostKeyKey)
        settings = nil
        phase = .setup
    }

    func showSetup() {
        Task { await teardown() }
        phase = .setup
    }

    // MARK: connecting

    /// quiet: a background retry, which leaves the failure screen up until it works.
    func connect(quiet: Bool = false) async {
        guard let settings else {
            phase = .setup
            return
        }
        await teardown()
        attempt += 1
        let mine = attempt
        func step(_ s: String) { if mine == attempt && !quiet { phase = .connecting(s) } }
        step("Connecting to \(settings.host)")
        do {
            let bundle = try HeadsetServer.Bundle.fromApp()
            let auth = SSHAuthenticationMethod.ed25519(username: settings.user, privateKey: DeviceKey.loadOrCreate())
            let pin = PinnedHostKey(expected: hostKey)
            let link = try await FrameLink.connect(settings, auth: auth, hostKey: pin)
            guard mine == attempt else { await link.close(); return }
            self.link = link
            if hostKey == nil, let seen = pin.seen { UserDefaults.standard.set(seen, forKey: Self.hostKeyKey) }
            let dir = try await HeadsetServer.deploy(bundle, over: link) { s in Task { @MainActor in step(s) } }
            step("Starting Frame Control on the headset")
            let key = Self.randomKey()
            let server = try await HeadsetServer.start(in: dir, over: link, key: key, device: deviceName)
            self.server = server
            let forwarder = try await PortForwarder.start(over: link, to: server.port)
            self.forwarder = forwarder
            guard mine == attempt else { return }
            server.onExit = { [weak self] tail in
                Task { @MainActor in self?.lost(mine, "Frame Control on the headset stopped. \(tail.suffix(200))") }
            }
            readySince = Date()
            phase = .ready(URL(string: "http://127.0.0.1:\(forwarder.localPort)/?key=\(key)")!)
        } catch {
            guard mine == attempt else { return }
            await teardown()
            phase = .failed((error as? FrameFailure)?.message ?? FrameLink.describe(error, host: settings.host))
            // Keep trying quietly while the app is open: the Frame may just be asleep.
            // A new task each time, so retrying for hours doesn't nest awaits.
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard let self, mine == self.attempt, case .failed = self.phase,
                      UIApplication.shared.applicationState == .active else { return }
                await self.connect(quiet: true)
            }
        }
    }

    /// Called when the app comes back to the foreground: iOS may have dropped the
    /// connection while it was in the background.
    func resume() {
        switch phase {
        case .ready:
            if link?.isConnected != true || server?.exited != nil { Task { await connect() } }
        case .failed:
            if settings != nil { Task { await connect() } }
        default:
            break
        }
    }

    private var readySince = Date.distantPast

    private func lost(_ which: Int, _ why: String) {
        guard which == attempt, case .ready = phase else { return }
        // Restart it once; if it dies again straight away, say so instead of looping.
        if Date().timeIntervalSince(readySince) < 20 {
            Task { await teardown() }
            phase = .failed(why)
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
