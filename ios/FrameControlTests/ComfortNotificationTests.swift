import XCTest
import UserNotifications
@testable import Frame_Control

final class ComfortNotificationTests: XCTestCase {
    func testNotificationContentAndBounds() {
        let content = WebShell.Coordinator.notificationContent("Time for a break")
        XCTAssertEqual(content?.title, "Frame Control")
        XCTAssertEqual(content?.body, "Time for a break")
        XCTAssertNotNil(content?.sound)
        XCTAssertNil(WebShell.Coordinator.notificationContent(""))
        XCTAssertNil(WebShell.Coordinator.notificationContent(String(repeating: "x", count: 501)))
    }
}
