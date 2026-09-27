import Citadel
import CryptoKit
import Foundation
import NIOCore
import NIOSSH

/// Where the Frame is and who to log in as.
struct FrameSettings: Codable, Equatable {
    var host: String
    var port: Int = 22
    var user: String = "steamos"
}

struct FrameFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

/// Trust on first use: pairing records the Frame's host key; later connections
/// accept that key and nothing else, as ssh's known_hosts does.
final class PinnedHostKey: NIOSSHClientServerAuthenticationDelegate, @unchecked Sendable {
    struct Changed: Error {}
    let expected: String?
    private let lock = NSLock()
    private var _seen: String?
    var seen: String? { lock.withLock { _seen } }

    init(expected: String?) { self.expected = expected }

    func validateHostKey(hostKey: NIOSSHPublicKey, validationCompletePromise: EventLoopPromise<Void>) {
        let key = String(openSSHPublicKey: hostKey)
        lock.withLock { _seen = key }
        if expected == nil || expected == key {
            validationCompletePromise.succeed(())
        } else {
            validationCompletePromise.fail(Changed())
        }
    }
}

/// One SSH connection to the Frame, and the few things the app does over it.
final class FrameLink: @unchecked Sendable {
    let client: SSHClient

    private init(client: SSHClient) { self.client = client }

    static func connect(_ settings: FrameSettings, auth: SSHAuthenticationMethod, hostKey: PinnedHostKey) async throws -> FrameLink {
        do {
            let client = try await SSHClient.connect(
                host: settings.host, port: settings.port, authenticationMethod: auth,
                hostKeyValidator: .custom(hostKey), reconnect: .never, connectTimeout: .seconds(8))
            return FrameLink(client: client)
        } catch {
            throw FrameFailure(describe(error, host: settings.host))
        }
    }

    /// The plain-language reason a connection failed, like the desktop server's messages.
    static func describe(_ error: Error, host: String) -> String {
        if error is PinnedHostKey.Changed {
            return "The Frame's SSH identity changed (after a reinstall, or a different device at \(host)). Pair again."
        }
        let text = String(describing: error)
        if text.contains("allAuthenticationOptionsFailed") || text.contains("authentication") {
            return "The Frame didn't accept the login. Pair again, and check the Developer Mode password."
        }
        if ["timeout", "Timeout", "timed out", "Host is down", "No route to host", "Network is unreachable",
            "errno: 64", "errno: 65", "errno: 51", "errno: 60"].contains(where: text.contains) {
            return "The Frame isn't answering at \(host). It may be asleep, switched off, or on another network."
        }
        if text.contains("refused") || text.contains("ECONNREFUSED") {
            return "The Frame refused the connection at \(host). Check Developer Mode is still on."
        }
        if text.contains("NXDOMAIN") || text.contains("resolve") || text.contains("unknownHost") || text.contains("NoAddress") {
            return "Can't find \(host) on the network. Check the address, and that the Frame is on the same network."
        }
        return "Couldn't connect to \(host): \(text)"
    }

    var isConnected: Bool { client.isConnected }

    func close() async {
        try? await client.close()
    }

    /// Runs a shell command; returns its combined output and exit status.
    func run(_ command: String) async throws -> (output: String, status: Int) {
        // stderr joins stdout (Citadel treats any stderr as a failure), and the
        // status comes back as the last line so a non-zero exit isn't an exception.
        let buffer = try await client.executeCommand("{ \(command)\n} 2>&1; echo \"@@rc=$?\"")
        var text = String(buffer: buffer)
        var status = 0
        if let range = text.range(of: "@@rc=", options: .backwards) {
            status = Int(text[range.upperBound...].trimmingCharacters(in: .whitespacesAndNewlines)) ?? -1
            text = String(text[..<range.lowerBound])
        }
        return (text.trimmingCharacters(in: .whitespacesAndNewlines), status)
    }

    /// Runs a command that must succeed; its output, or a FrameFailure with it.
    @discardableResult
    func check(_ command: String, _ what: String) async throws -> String {
        let r = try await run(command)
        guard r.status == 0 else { throw FrameFailure("\(what): \(r.output.isEmpty ? "exit \(r.status)" : r.output)") }
        return r.output
    }

    /// Writes data to a path relative to the home directory.
    func upload(_ data: Data, to path: String) async throws {
        let sftp = try await client.openSFTP()
        do {
            try await sftp.withFile(filePath: path, flags: [.write, .create, .truncate]) { file in
                try await file.write(ByteBuffer(bytes: data))
            }
            try? await sftp.close()
        } catch {
            try? await sftp.close()
            throw error
        }
    }
}

func shellQuote(_ s: String) -> String {
    "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
}
