import SwiftUI

struct RootView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        ZStack {
            Color.frameBackground.ignoresSafeArea()
            switch model.phase {
            case .setup:
                SetupView(model: model)
            case .connecting(let step):
                ConnectingView(step: step, host: model.settings?.host) { model.showSetup() }
            case .failed(let message):
                FailedView(message: message, canRetry: model.settings != nil, retrying: model.retrying, needsPairing: model.needsPairing,
                           retry: { Task { await model.connect() } }, change: { model.showSetup() })
            case .ready(let url):
                WebShell(url: url, model: model).ignoresSafeArea()
            }
        }
        .preferredColorScheme(.dark)
        .tint(.frameBlue)
    }
}

extension Color {
    static let frameBackground = Color(red: 0.055, green: 0.078, blue: 0.106)
    static let framePanel = Color(red: 0.118, green: 0.137, blue: 0.161)
    static let frameBlue = Color(red: 0.102, green: 0.624, blue: 1.0)
    static let frameMuted = Color(red: 0.561, green: 0.596, blue: 0.627)
}

struct ConnectingView: View {
    let step: String
    let host: String?
    let cancel: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            Image("AppIconImage").resizable().frame(width: 76, height: 76).clipShape(RoundedRectangle(cornerRadius: 17))
            ProgressView().controlSize(.large)
            Text(step).font(.headline).multilineTextAlignment(.center)
            if let host { Text(host).font(.subheadline).foregroundStyle(Color.frameMuted) }
            Button("Change headset", action: cancel).padding(.top, 8)
        }
        .padding(32)
    }
}

struct FailedView: View {
    let message: String
    let canRetry: Bool
    let retrying: Bool
    let needsPairing: Bool
    let retry: () -> Void
    let change: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: needsPairing ? "lock.trianglebadge.exclamationmark" : "wifi.exclamationmark")
                .font(.system(size: 44)).foregroundStyle(.orange)
            Text(needsPairing ? "Pair with the Frame again" : "Can't reach the Frame").font(.title3.bold())
            Text(message).multilineTextAlignment(.center).foregroundStyle(Color.frameMuted)
            if retrying { Text("Trying again every few seconds.").font(.footnote).foregroundStyle(Color.frameMuted) }
            if needsPairing {
                Button("Pair again", action: change).buttonStyle(.borderedProminent).controlSize(.large)
            } else if canRetry {
                Button("Try again", action: retry).buttonStyle(.borderedProminent).controlSize(.large)
            }
            if !needsPairing { Button(canRetry ? "Change headset" : "Back", action: change) }
        }
        .padding(32)
        .frame(maxWidth: 480)
    }
}
