import Citadel
import Foundation
import NIOCore
import NIOPosix
import NIOSSH

/// Listens on this phone's 127.0.0.1 and carries each connection to a port on the
/// Frame's 127.0.0.1 through the SSH session (ssh -L). The web view loads the
/// server from here; every API request still needs the session's key.
final class PortForwarder: @unchecked Sendable {
    private let channel: Channel
    let localPort: Int

    private init(channel: Channel, localPort: Int) {
        self.channel = channel
        self.localPort = localPort
    }

    static func start(over link: FrameLink, to remotePort: Int) async throws -> PortForwarder {
        let client = link.client
        // The listener shares the SSH connection's event loop, so the glue between
        // each pair of channels never crosses threads.
        let bootstrap = ServerBootstrap(group: client.eventLoop)
            .serverChannelOption(ChannelOptions.socketOption(.so_reuseaddr), value: 1)
            .childChannelOption(ChannelOptions.allowRemoteHalfClosure, value: true)
            // Nothing is read from the web view until the SSH side is ready for it.
            .childChannelOption(ChannelOptions.autoRead, value: false)
            .childChannelInitializer { inbound in
                inbound.eventLoop.makeFutureWithTask {
                    let (local, remote) = GlueHandler.matchedPair()
                    try await inbound.pipeline.addHandler(local).get()
                    let origin = try inbound.remoteAddress ?? SocketAddress(ipAddress: "127.0.0.1", port: 0)
                    _ = try await client.createDirectTCPIPChannel(
                        using: SSHChannelType.DirectTCPIP(targetHost: "127.0.0.1", targetPort: remotePort, originatorAddress: origin)
                    ) { channel in channel.pipeline.addHandler(remote) }
                    try await inbound.setOption(ChannelOptions.autoRead, value: true).get()
                }
            }
        let channel = try await bootstrap.bind(host: "127.0.0.1", port: 0).get()
        guard let port = channel.localAddress?.port else { throw FrameFailure("Couldn't open a local port") }
        return PortForwarder(channel: channel, localPort: port)
    }

    func stop() {
        channel.close(promise: nil)
    }
}

/// Joins two channels: what one reads, the other writes, with backpressure and
/// half-close passed across (the pattern from SwiftNIO's examples).
final class GlueHandler: ChannelDuplexHandler, @unchecked Sendable {
    typealias InboundIn = NIOAny
    typealias OutboundIn = NIOAny
    typealias OutboundOut = NIOAny

    private var partner: GlueHandler?
    private var context: ChannelHandlerContext?
    private var pendingRead = false

    static func matchedPair() -> (GlueHandler, GlueHandler) {
        let a = GlueHandler(), b = GlueHandler()
        a.partner = b
        b.partner = a
        return (a, b)
    }

    private func partnerWrite(_ data: NIOAny) { context?.write(data, promise: nil) }
    private func partnerFlush() { context?.flush() }
    private func partnerWriteEOF() { context?.close(mode: .output, promise: nil) }
    private func partnerClose() { context?.close(promise: nil) }
    private var partnerWritable: Bool { context?.channel.isWritable ?? false }

    private func partnerBecameWritable() {
        if pendingRead {
            pendingRead = false
            context?.read()
        }
    }

    func handlerAdded(context: ChannelHandlerContext) { self.context = context }

    func handlerRemoved(context: ChannelHandlerContext) {
        self.context = nil
        partner = nil
    }

    func channelRead(context: ChannelHandlerContext, data: NIOAny) { partner?.partnerWrite(data) }
    func channelReadComplete(context: ChannelHandlerContext) { partner?.partnerFlush() }
    func channelInactive(context: ChannelHandlerContext) { partner?.partnerClose() }

    func userInboundEventTriggered(context: ChannelHandlerContext, event: Any) {
        if let e = event as? ChannelEvent, case .inputClosed = e {
            partner?.partnerWriteEOF()
        }
        context.fireUserInboundEventTriggered(event)
    }

    func errorCaught(context: ChannelHandlerContext, error: Error) {
        partner?.partnerClose()
    }

    func channelWritabilityChanged(context: ChannelHandlerContext) {
        if context.channel.isWritable { partner?.partnerBecameWritable() }
    }

    func read(context: ChannelHandlerContext) {
        if let partner, partner.partnerWritable {
            context.read()
        } else {
            pendingRead = true
        }
    }
}
