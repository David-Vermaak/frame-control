import SwiftUI

@main
struct FrameControlApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .task {
                    #if DEBUG
                    // Simulator testing without the pairing screen: print this device's key,
                    // and connect to FRAME_TEST_HOST with it (`simctl launch` passes
                    // SIMCTL_CHILD_FRAME_TEST_HOST through as FRAME_TEST_HOST).
                    print("FRAME_CONTROL_KEY: \(model.authorizedKeysLine)")
                    // FRAME_TEST_LANDSCAPE=1 turns the app on its side, to check the safe areas there.
                    if ProcessInfo.processInfo.environment["FRAME_TEST_LANDSCAPE"] != nil,
                       let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene {
                        scene.requestGeometryUpdate(.iOS(interfaceOrientations: .landscapeRight))
                    }
                    // FRAME_TEST_PAIR="host|user|password" runs the real password pairing.
                    if model.settings == nil, let pair = ProcessInfo.processInfo.environment["FRAME_TEST_PAIR"] {
                        let f = pair.components(separatedBy: "|")
                        if f.count == 3 { await model.pair(host: f[0], user: f[1], password: f[2]); return }
                    }
                    if model.settings == nil, let host = ProcessInfo.processInfo.environment["FRAME_TEST_HOST"] {
                        await model.useKey(host: host, user: "steamos")
                        return
                    }
                    #endif
                    if model.settings != nil { await model.connect() }
                }
                .onOpenURL { url in
                    // frame-control://install?… from a website (docs/web-install.md).
                    guard let link = InstallLink(url.absoluteString), model.pendingInstallLinks.count < 5 else { return }
                    model.pendingInstallLinks.append(link)
                }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active { model.resume() }
                }
        }
    }
}
