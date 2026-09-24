import AppKit
import UserNotifications

/// Notifications for the moments that go unseen once you've switched to
/// something else: JARVIS needs your OK (or you, for a sign-in), or a task
/// has finished. Only posted while JARVIS's window isn't the one you're
/// using; clicking one brings it forward.
final class Notifier: NSObject, UNUserNotificationCenterDelegate {
    var onOpen: (() -> Void)?

    func setUp() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    func post(title: String, body: String, identifier: String, sound: Bool = true) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        if sound {
            content.sound = .default
        }
        let request = UNNotificationRequest(identifier: identifier.isEmpty ? UUID().uuidString : identifier,
                                            content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async { self.onOpen?() }
        completionHandler()
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler:
                                    @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .sound])
    }
}
