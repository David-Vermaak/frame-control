import CryptoKit
import XCTest
@testable import Frame_Control

final class InstallLinkTests: XCTestCase {
    func testAcceptsManifestAndURLLinks() {
        XCTAssertEqual(InstallLink("frame-control://install?manifest=https://example.com/app.json"),
                       InstallLink("frame-control://install/?manifest=https://example.com/app.json"))
        XCTAssertEqual(InstallLink("frame-control://install?url=https://example.com/a.apk")?.kind, .url)
        XCTAssertEqual(InstallLink("FRAME-CONTROL://install?manifest=https://example.com/m.json")?.target, "https://example.com/m.json")
    }

    func testRejectsAnythingElse() {
        for raw in ["frame-control://other?url=https://example.com/a.apk",
                    "frame-control://install?url=ftp://example.com/a.apk",
                    "frame-control://install?url=https://user:pw@example.com/a.apk",
                    "frame-control://install?url=https://example.com/a&manifest=https://example.com/b",
                    "frame-control://install?url=https://a.example/x&url=https://b.example/y",
                    "frame-control://install?url=",
                    "frame-control://install/deeper?url=https://example.com/a.apk",
                    "https://example.com/?url=https://example.com/a.apk",
                    "frame-control://install?url=https://example.com/" + String(repeating: "a", count: 2100)] {
            XCTAssertNil(InstallLink(raw), raw)
        }
    }
}

final class HeadsetServerTests: XCTestCase {
    func testReadsThePortTheServerPrints() {
        XCTAssertEqual(HeadsetServer.port(in: "Frame Control on http://127.0.0.1:41234  (alias: frame; Ctrl-C to stop)\n"), 41234)
        XCTAssertNil(HeadsetServer.port(in: "Traceback (most recent call last):"))
    }

    func testBundleIsInTheApp() throws {
        let bundle = try HeadsetServer.Bundle.fromApp()
        XCTAssertGreaterThan(bundle.data.count, 100_000)
        XCTAssertEqual(bundle.version.count, 16)
    }
}

final class KeyTests: XCTestCase {
    func testAuthorizedKeysLine() {
        let line = DeviceKey.authorizedKeysLine(Curve25519.Signing.PrivateKey(), comment: "frame-control@iPhone")
        let parts = line.split(separator: " ")
        XCTAssertEqual(parts.count, 3)
        XCTAssertEqual(parts[0], "ssh-ed25519")
        XCTAssertEqual(Data(base64Encoded: String(parts[1]))?.count, 51)  // string "ssh-ed25519" + 32-byte key
        XCTAssertEqual(parts[2], "frame-control@iPhone")
    }

    func testShellQuote() {
        XCTAssertEqual(shellQuote("it's"), "'it'\\''s'")
    }
}
