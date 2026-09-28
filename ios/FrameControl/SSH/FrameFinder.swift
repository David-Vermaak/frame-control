import Foundation
import Network

/// Finds the Frame on the local network so nobody has to type its address.
/// A Frame in Developer Mode advertises Valve's devkit service over Bonjour
/// (`_steamos-devkit._tcp`); failing that, `fallback` (the saved address, or
/// frame.local) is checked by opening its SSH port. Both repeat until stopped.
@MainActor
final class FrameFinder: ObservableObject {
    struct Found: Equatable {
        let host: String   // what to connect to
        let name: String   // what to call it
    }

    @Published private(set) var found: Found?
    /// When the search began, to tell "still looking" from "can't find it".
    @Published private(set) var since = Date()

    private var browser: NWBrowser?
    private var probeTask: Task<Void, Never>?
    private var fallback = "frame.local"

    func start(fallback: String?) {
        stop()
        self.fallback = (fallback?.isEmpty == false ? fallback : nil) ?? "frame.local"
        found = nil
        since = Date()
        browse()
        probeTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                let host = self.fallback
                if self.found == nil, await Self.sshAnswers(host: host) {
                    self.found = Found(host: host, name: host)
                }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    func stop() {
        browser?.cancel()
        browser = nil
        probeTask?.cancel()
        probeTask = nil
    }

    private func browse() {
        let browser = NWBrowser(for: .bonjour(type: "_steamos-devkit._tcp", domain: nil), using: .tcp)
        browser.browseResultsChangedHandler = { [weak self] results, _ in
            for result in results {
                guard case let .service(name, _, _, _) = result.endpoint else { continue }
                Self.resolve(result.endpoint) { host in
                    Task { @MainActor in
                        guard let self, let host else { return }
                        // A found device is used over the fallback probe.
                        self.found = Found(host: host, name: name)
                    }
                }
            }
        }
        browser.start(queue: .main)
        self.browser = browser
    }

    /// The device's IP address: connect to the service and read where it went.
    nonisolated private static func resolve(_ endpoint: NWEndpoint, done: @escaping @Sendable (String?) -> Void) {
        let connection = NWConnection(to: endpoint, using: .tcp)
        let once = Once()
        connection.stateUpdateHandler = { state in
            switch state {
            case .ready:
                var host: String?
                if case let .hostPort(h, _)? = connection.currentPath?.remoteEndpoint {
                    host = "\(h)".components(separatedBy: "%").first  // drop an IPv6 interface suffix
                }
                connection.cancel()
                if once.claim() { done(host) }
            case .failed, .cancelled:
                if once.claim() { done(nil) }
            default:
                break
            }
        }
        connection.start(queue: .global())
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) {
            connection.cancel()
            if once.claim() { done(nil) }
        }
    }

    /// Whether something answers on the SSH port of "host" or "host:port" within a few seconds.
    nonisolated static func sshAnswers(host address: String) async -> Bool {
        var host = address, port: UInt16 = 22
        if let target = AppModel.parse(host: address, user: "steamos") {
            host = target.host
            port = UInt16(target.port)
        }
        return await sshAnswers(host: host, port: port)
    }

    nonisolated static func sshAnswers(host: String, port: UInt16) async -> Bool {
        await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            let connection = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port) ?? 22, using: .tcp)
            let once = Once()
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    connection.cancel()
                    if once.claim() { c.resume(returning: true) }
                case .failed, .waiting:
                    connection.cancel()
                    if once.claim() { c.resume(returning: false) }
                default:
                    break
                }
            }
            connection.start(queue: .global())
            DispatchQueue.global().asyncAfter(deadline: .now() + 3) {
                connection.cancel()
                if once.claim() { c.resume(returning: false) }
            }
        }
    }
}
