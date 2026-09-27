import SwiftUI
import UIKit

/// First run: two steps. Wake the Frame (the app finds it by itself), then type the
/// Developer Mode password once. Everything else waits under "Other ways to connect".
struct SetupView: View {
    @ObservedObject var model: AppModel
    @StateObject private var finder = FrameFinder()
    @State private var password = ""
    @State private var manualHost = ""
    @State private var user = "steamos"
    @State private var showOther = false
    @State private var showHelp = false
    @FocusState private var passwordFocused: Bool

    /// Where Connect goes: what the finder saw, else what was typed, else frame.local.
    private var host: String {
        let typed = manualHost.trimmingCharacters(in: .whitespaces)
        return finder.found?.host ?? (typed.isEmpty ? "frame.local" : typed)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                header
                StepCard(number: 1, title: "Wake your Frame", done: finder.found != nil) { wakeStep }
                StepCard(number: 2, title: "Enter its Developer Mode password", done: false) { passwordStep }
                otherWays
            }
            .padding(20)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(Color.frameBackground)
        .onAppear {
            manualHost = model.settings?.host ?? ""
            user = model.settings?.user ?? "steamos"
            var fallback = model.settings?.host
            #if DEBUG
            fallback = ProcessInfo.processInfo.environment["FRAME_TEST_FALLBACK"] ?? fallback  // Simulator test hook
            #endif
            finder.start(fallback: fallback)
        }
        .onDisappear { finder.stop() }
        // After a few seconds of not finding it, say exactly what to check.
        .task(id: finder.since) {
            try? await Task.sleep(nanoseconds: 8_000_000_000)
            showHelp = true
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image("AppIconImage").resizable().frame(width: 56, height: 56).clipShape(RoundedRectangle(cornerRadius: 13))
            Text("Connect to your Steam Frame").font(.title2.bold())
            Text("One time only. After this, the app connects by itself whenever your Frame is awake.")
                .foregroundStyle(Color.frameMuted)
        }
    }

    // MARK: step 1

    @ViewBuilder private var wakeStep: some View {
        if let found = finder.found {
            Label {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Found your Frame").fontWeight(.semibold)
                    Text(found.name == found.host ? found.host : "\(found.name) · \(found.host)")
                        .font(.footnote).foregroundStyle(Color.frameMuted)
                }
            } icon: {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            }
        } else {
            HStack(spacing: 10) {
                ProgressView()
                Text("Looking for it on this network…").foregroundStyle(Color.frameMuted)
            }
            Text("Put the headset on, or press its power button, so it's awake.")
            if showHelp {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Still can't see it? Check:").font(.subheadline.weight(.semibold))
                    Tip(icon: "wifi", text: "The Frame and this \(model.deviceName) are on the same Wi-Fi.")
                    Tip(icon: "hammer", text: "Developer Mode is on: on the Frame, Steam Settings → System → Enable Developer Mode.")
                    Tip(icon: "network", text: "Local Network is allowed for Frame Control: \(model.deviceName) Settings → Apps → Frame Control.")
                }
                .padding(.top, 4)
            }
        }
    }

    // MARK: step 2

    @ViewBuilder private var passwordStep: some View {
        SecureField("Developer Mode password", text: $password)
            .textContentType(.password)
            .submitLabel(.go)
            .focused($passwordFocused)
            .onSubmit(connect)
            .padding(12)
            .background(Color.black.opacity(0.28), in: RoundedRectangle(cornerRadius: 10))
        Text("Haven't set one? On the Frame: Steam Settings → Developer → Set User Password. It's only used now, to let this \(model.deviceName) in; it isn't saved.")
            .font(.footnote).foregroundStyle(Color.frameMuted)
        Button(action: connect) {
            Text(finder.found == nil ? "Connect to \(host)" : "Connect")
                .fontWeight(.semibold).frame(maxWidth: .infinity).padding(.vertical, 4)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .disabled(password.isEmpty)
    }

    // MARK: everything else, out of the way

    private var otherWays: some View {
        DisclosureGroup(isExpanded: $showOther) {
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Address").font(.footnote).foregroundStyle(Color.frameMuted)
                    TextField("frame.local, an IP, or a Tailscale name", text: $manualHost)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        .onSubmit { finder.start(fallback: manualHost) }
                        .padding(10).background(Color.black.opacity(0.28), in: RoundedRectangle(cornerRadius: 8))
                    TextField("User", text: $user)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .padding(10).background(Color.black.opacity(0.28), in: RoundedRectangle(cornerRadius: 8))
                    Text("Typing an address here uses it instead of searching.").font(.caption).foregroundStyle(Color.frameMuted)
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("Already reach the Frame over SSH? Add this \(model.deviceName)'s key to ~/.ssh/authorized_keys there, then connect without a password.")
                        .font(.footnote).foregroundStyle(Color.frameMuted)
                    HStack {
                        Button("Copy key") { UIPasteboard.general.string = model.authorizedKeysLine }
                        Spacer()
                        Button("Connect with the key") {
                            let (h, u) = (host, user)
                            Task { await model.useKey(host: h, user: u) }
                        }
                    }
                }
                if let saved = model.settings {
                    Button("Forget \(saved.host)", role: .destructive) { Task { await model.forget() } }
                }
            }
            .padding(.top, 10)
        } label: {
            Text("Other ways to connect").foregroundStyle(Color.frameMuted)
        }
        .onChange(of: manualHost) { _, value in
            // A typed address replaces the search.
            if !value.trimmingCharacters(in: .whitespaces).isEmpty, finder.found?.host != value { finder.start(fallback: value) }
        }
    }

    private func connect() {
        guard !password.isEmpty else { passwordFocused = true; return }
        let (h, u, p) = (host, user, password)
        password = ""
        finder.stop()
        Task { await model.pair(host: h, user: u, password: p) }
    }
}

/// A numbered step with a tick once it's done.
struct StepCard<Content: View>: View {
    let number: Int
    let title: String
    let done: Bool
    @ViewBuilder let content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                ZStack {
                    Circle().fill(done ? Color.green : Color.frameBlue).frame(width: 26, height: 26)
                    if done { Image(systemName: "checkmark").font(.caption.bold()) } else { Text("\(number)").font(.subheadline.bold()) }
                }
                .foregroundStyle(.white)
                Text(title).font(.headline)
            }
            content
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.framePanel, in: RoundedRectangle(cornerRadius: 14))
    }
}

struct Tip: View {
    let icon: String
    let text: String

    var body: some View {
        Label { Text(text).font(.subheadline).fixedSize(horizontal: false, vertical: true) } icon: { Image(systemName: icon).foregroundStyle(Color.frameBlue) }
    }
}
