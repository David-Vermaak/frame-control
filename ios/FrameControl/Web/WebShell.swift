import SwiftUI
import UIKit
import WebKit

/// The Frame Control page, served by the server on the headset, in a web view.
/// window.frameApp (the same bridge the desktop app's preload.js provides) lets
/// the page use the phone: clipboard, saving images, other apps, install links.
struct WebShell: UIViewRepresentable {
    let url: URL
    @ObservedObject var model: AppModel

    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let content = WKUserContentController()
        content.addUserScript(WKUserScript(source: Self.bridge, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        content.addScriptMessageHandler(context.coordinator, contentWorld: .page, name: "frameApp")
        config.userContentController = content
        config.allowsInlineMediaPlayback = true
        let web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.isOpaque = false
        web.backgroundColor = UIColor(red: 0.055, green: 0.078, blue: 0.106, alpha: 1)
        web.scrollView.backgroundColor = web.backgroundColor
        web.scrollView.contentInsetAdjustmentBehavior = .never  // the page pads for the safe area itself
        web.allowsBackForwardNavigationGestures = false
        #if DEBUG
        web.isInspectable = true
        #endif
        context.coordinator.web = web
        web.load(URLRequest(url: url))
        return web
    }

    func updateUIView(_ web: WKWebView, context: Context) {
        if context.coordinator.loaded != url {
            context.coordinator.loaded = url
            web.load(URLRequest(url: url))
        }
        context.coordinator.deliverInstallLinks()
    }

    static let bridge = """
    (() => {
      const call = (name, arg) => window.webkit.messageHandlers.frameApp.postMessage({ name, arg: arg ?? null });
      let installCb = null;
      window.frameApp = {
        platform: "ios",
        readClipboard: () => call("readClipboard"),
        setUpConnection: () => call("setUpConnection"),
        open: (what) => call("open", what),
        saveImages: (images) => call("saveImages", images),
        onInstallLink: (cb) => { installCb = cb; return call("installLinkReady"); },
      };
      window.__frameInstallLink = (req) => { if (installCb) installCb(req); };
    })();
    """

    final class Coordinator: NSObject, WKScriptMessageHandlerWithReply, WKNavigationDelegate, WKUIDelegate {
        let model: AppModel
        weak var web: WKWebView?
        var loaded: URL?
        private var installReady = false

        init(model: AppModel) { self.model = model }

        // MARK: bridge

        @MainActor
        func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage,
                                   replyHandler: @escaping (Any?, String?) -> Void) {
            guard let body = message.body as? [String: Any], let name = body["name"] as? String else {
                return replyHandler(nil, "bad message")
            }
            let arg = body["arg"]
            switch name {
            case "readClipboard":
                replyHandler(UIPasteboard.general.string ?? "", nil)
            case "setUpConnection":
                model.showSetup()
                replyHandler(nil, nil)
            case "open":
                let result = open(arg as? String ?? "")
                replyHandler(result.message.map { ["message": $0] }, result.error)
            case "saveImages":
                let images = (arg as? [[String: Any]] ?? []).compactMap { item -> UIImage? in
                    guard let b64 = item["data"] as? String, let data = Data(base64Encoded: b64) else { return nil }
                    return UIImage(data: data)
                }
                guard !images.isEmpty else { return replyHandler(nil, "No images to save") }
                share(images)
                replyHandler(["message": "Choose Save Image to keep \(images.count == 1 ? "it" : "them") in Photos"], nil)
            case "installLinkReady":
                installReady = true
                deliverInstallLinks()
                replyHandler(nil, nil)
            default:
                replyHandler(nil, "unknown request \(name)")
            }
        }

        @MainActor
        func deliverInstallLinks() {
            guard installReady, let web, !model.pendingInstallLinks.isEmpty else { return }
            let links = model.pendingInstallLinks
            model.pendingInstallLinks = []
            for link in links {
                let req = ["kind": link.kind.rawValue, "target": link.target]
                guard let json = try? JSONSerialization.data(withJSONObject: req), let text = String(data: json, encoding: .utf8) else { continue }
                web.evaluateJavaScript("window.__frameInstallLink(\(text))")
            }
        }

        /// SSH, SFTP, Steam Link and remote desktop open in the apps that handle them.
        @MainActor
        private func open(_ what: String) -> (message: String?, error: String?) {
            guard let s = model.settings else { return (nil, "Not paired with a Frame") }
            let host = s.host.contains(":") ? "[\(s.host)]" : s.host
            let target: (url: String, app: String, store: String)
            switch what {
            case "terminal": target = ("ssh://\(s.user)@\(host):\(s.port)", "an SSH app such as Blink Shell or Termius", "https://apps.apple.com/search?term=ssh")
            case "sftp": target = ("sftp://\(s.user)@\(host):\(s.port)", "an SFTP app such as Termius or Secure ShellFish", "https://apps.apple.com/search?term=sftp")
            case "steamlink": target = ("steamlink://", "Steam Link", "https://apps.apple.com/app/steam-link/id1246969117")
            case "rdp": target = ("rdp://full%20address=s:\(s.host):3389", "Windows App (Microsoft Remote Desktop)", "https://apps.apple.com/app/windows-app/id714464092")
            default: return (nil, "Can't open \(what) on this \(model.deviceName)")
            }
            guard let url = URL(string: target.url) else { return (nil, "Bad address") }
            if UIApplication.shared.canOpenURL(url) {
                UIApplication.shared.open(url)
                return ("Opening \(target.app)", nil)
            }
            if let store = URL(string: target.store) { UIApplication.shared.open(store) }
            return (nil, "Install \(target.app) to open this; opening the App Store")
        }

        @MainActor
        private func share(_ images: [UIImage]) {
            guard let web, let root = web.window?.rootViewController else { return }
            let sheet = UIActivityViewController(activityItems: images, applicationActivities: nil)
            sheet.popoverPresentationController?.sourceView = web
            sheet.popoverPresentationController?.sourceRect = CGRect(x: web.bounds.midX, y: web.bounds.midY, width: 1, height: 1)
            (root.presentedViewController ?? root).present(sheet, animated: true)
        }

        // MARK: navigation: the app's page stays here; other sites open in Safari

        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.cancel) }
            if url.host == "127.0.0.1" || url.scheme == "about" || url.scheme == "blob" || url.scheme == "data" {
                return decisionHandler(.allow)
            }
            UIApplication.shared.open(url)
            decisionHandler(.cancel)
        }

        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url { UIApplication.shared.open(url) }  // target="_blank" links
            return nil
        }

        // MARK: alert() and confirm(), which the page uses before removing things

        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            present(message, actions: [UIAlertAction(title: "OK", style: .default) { _ in completionHandler() }], fallback: completionHandler)
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            present(message, actions: [
                UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) },
                UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) },
            ], fallback: { completionHandler(false) })
        }

        private func present(_ message: String, actions: [UIAlertAction], fallback: @escaping () -> Void) {
            guard let root = web?.window?.rootViewController else { return fallback() }
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            actions.forEach(alert.addAction)
            (root.presentedViewController ?? root).present(alert, animated: true)
        }
    }
}
