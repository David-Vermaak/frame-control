import SwiftUI
import UIKit

/// Pairing: the Developer Mode password is used once, to add this phone's own
/// key to the Frame. It isn't stored.
struct SetupView: View {
    @ObservedObject var model: AppModel
    @State private var host = ""
    @State private var user = "steamos"
    @State private var password = ""
    @FocusState private var focus: Field?
    private enum Field { case host, user, password }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(alignment: .leading, spacing: 10) {
                        Image("AppIconImage").resizable().frame(width: 64, height: 64).clipShape(RoundedRectangle(cornerRadius: 14))
                        Text("Connect to your Steam Frame").font(.title2.bold())
                        Text("See what the headset sees, install games and Android apps, and send files and text, from this \(model.deviceName).")
                            .foregroundStyle(Color.frameMuted)
                    }
                    .padding(.vertical, 6)
                    .listRowBackground(Color.clear)
                }
                Section {
                    Label("On the Frame, open Steam Settings → System and turn on Developer Mode.", systemImage: "1.circle")
                    Label("Then Developer → Set User Password.", systemImage: "2.circle")
                    Label("Enter the headset's address and that password here, once.", systemImage: "3.circle")
                } header: { Text("Before you start") }
                Section {
                    TextField("frame.local or 192.168.1.20", text: $host)
                        .textContentType(.URL).keyboardType(.URL).autocorrectionDisabled().textInputAutocapitalization(.never)
                        .focused($focus, equals: .host).submitLabel(.next).onSubmit { focus = .password }
                    TextField("User", text: $user)
                        .autocorrectionDisabled().textInputAutocapitalization(.never).focused($focus, equals: .user)
                    SecureField("Developer Mode password", text: $password)
                        .textContentType(.password).focused($focus, equals: .password).submitLabel(.go).onSubmit(pair)
                } header: { Text("Headset") } footer: {
                    Text("The password is only used to add this \(model.deviceName)'s own SSH key to the Frame; it isn't saved. The Frame and this \(model.deviceName) need to be on the same network, or both on Tailscale.")
                }
                Section {
                    Button(action: pair) {
                        Text("Pair").frame(maxWidth: .infinity).fontWeight(.semibold)
                    }
                    .disabled(host.trimmingCharacters(in: .whitespaces).isEmpty || password.isEmpty)
                    if model.settings != nil {
                        Button("Use \(model.settings!.host) again") { Task { await model.connect() } }
                        Button("Forget this headset", role: .destructive) { Task { await model.forget() } }
                    }
                }
                Section {
                    Text(model.authorizedKeysLine)
                        .font(.system(.caption2, design: .monospaced)).lineLimit(3).textSelection(.enabled)
                    Button("Copy this \(model.deviceName)'s key") { UIPasteboard.general.string = model.authorizedKeysLine }
                    Button("Connect with the key") {
                        let (h, u) = (host, user)
                        Task { await model.useKey(host: h, user: u) }
                    }
                    .disabled(host.trimmingCharacters(in: .whitespaces).isEmpty)
                } header: { Text("Or add the key yourself") } footer: {
                    Text("If you already reach the Frame over SSH, add this line to ~/.ssh/authorized_keys there, then connect without a password.")
                }
            }
            .scrollContentBackground(.hidden)
            .background(Color.frameBackground)
            .navigationTitle("Frame Control")
            .navigationBarTitleDisplayMode(.inline)
        }
        .onAppear {
            host = model.settings?.host ?? "frame.local"
            user = model.settings?.user ?? "steamos"
        }
    }

    private func pair() {
        let (h, u, p) = (host, user, password)
        password = ""
        Task { await model.pair(host: h, user: u, password: p) }
    }
}
