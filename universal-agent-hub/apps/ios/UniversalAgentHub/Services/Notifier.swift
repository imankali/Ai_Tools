import AudioToolbox
import Foundation
import UserNotifications

/// اعلان‌های بومی برای «تأیید لازم است» و «ایجنت در حال کار است».
enum Notifier {
    static func requestAuthorization() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    /// `payload` همان JSONی است که UI با `window.webkit.messageHandlers.HubNative.postMessage` می‌فرستد.
    static func handle(payload: [String: Any]) {
        let title = (payload["title"] as? String) ?? NSLocalizedString("notif.approval", comment: "")
        let body = (payload["body"] as? String) ?? ""
        let urgent = (payload["urgent"] as? Bool) ?? false
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = String(body.prefix(400))
        content.sound = urgent ? .defaultCritical : .default
        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil // فوراً
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
        if urgent {
            // لرزش روی آیفون؛ روی iPad بی‌اثر است
            AudioServicesPlaySystemSound(1521)
        }
    }
}
