import Foundation

/// frame-control://install?manifest=URL or ?url=URL (docs/web-install.md), the same
/// first filter as app/install-link.js. The server on the Frame applies the full
/// rules (HTTPS, no private addresses, redirects) before fetching anything.
struct InstallLink: Equatable {
    enum Kind: String { case manifest, url }
    let kind: Kind
    let target: String

    static let scheme = "frame-control"
    private static let maxLink = 4096
    private static let maxURL = 2048

    init?(_ raw: String) {
        guard raw.count <= Self.maxLink, raw.lowercased().hasPrefix("\(Self.scheme):"),
              let link = URLComponents(string: raw), link.scheme?.lowercased() == Self.scheme,
              link.host?.lowercased() == "install", ["", "/"].contains(link.path) else { return nil }
        let items = link.queryItems ?? []
        guard items.count == 1, let item = items.first, let kind = Kind(rawValue: item.name),
              let target = item.value, !target.isEmpty, target.count <= Self.maxURL,
              let url = URLComponents(string: target), ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
              url.host?.isEmpty == false, url.user == nil, url.password == nil else { return nil }
        self.kind = kind
        self.target = target
    }
}
