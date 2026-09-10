import Foundation
import Security

/// نگهداری «آدرس سرور» و «توکن دسترسی» در Keychain (نه UserDefaults).
///
/// آدرس عمومی است و در UserDefaults می‌ماند تا سریع خوانده شود؛ توکن همیشه
/// در Keychain با `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` است:
/// روی بکاپ iCloud و دستگاه جدید منتقل نمی‌شود.
enum Credentials {
    private static let defaults = UserDefaults.standard
    private static let urlKey = "hub.serverURL"
    private static let sessionKey = "hub.sessionID"
    private static let service = "com.agenthub.ios.token"
    private static let account = "hub-access-token"

    static var serverURL: String? {
        get { defaults.string(forKey: urlKey)?.trimmingCharacters(in: CharacterSet(charactersIn: "/ ")) }
        set {
            if let value = newValue, !value.isEmpty {
                defaults.set(normalize(value), forKey: urlKey)
            } else {
                defaults.removeObject(forKey: urlKey)
            }
        }
    }

    /// شناسه‌ای session که کاربر آخرین بار داشته (برای ادامه‌ی گفت‌وگو).
    static var sessionID: String? {
        get { defaults.string(forKey: sessionKey) }
        set { defaults.set(newValue, forKey: sessionKey) }
    }

    static var hasConnection: Bool { serverURL?.isEmpty == false }

    /// توکن؛ `nil` یعنی «بدون احراز هویت» (شبکه‌ی کاملاً مطمئن).
    static func token() -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func setToken(_ value: String?) {
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        guard let value, !value.isEmpty else {
            SecItemDelete(base as CFDictionary)
            return
        }
        let attributes: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        if SecItemCopyMatching(base as CFDictionary, nil) == errSecSuccess {
            SecItemUpdate(base as CFDictionary, attributes as CFDictionary)
        } else {
            var add = base
            attributes.forEach { add[$0] = $1 }
            SecItemAdd(add as CFDictionary, nil)
        }
    }

    /// پاک‌کردن هر چیزی که روی این دستگاه ذخیره شده.
    static func forget() {
        defaults.removeObject(forKey: urlKey)
        defaults.removeObject(forKey: sessionKey)
        SecItemDelete([
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ] as CFDictionary)
    }

    /// `192.168.1.5:8765` → `http://192.168.1.5:8765` (بدون / انتهایی).
    static func normalize(_ raw: String) -> String {
        var value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while value.hasSuffix("/") { value.removeLast() }
        if !value.contains("://") { value = "http://" + value }
        return value
    }

    /// آدرس صفحه‌ی UI با پارامترهای یک‌بارمصرف (UI بعد از خواندن از تاریخچه پاک می‌کند).
    static func hubPageURL(base: String, token: String?, session: String?) -> URL? {
        guard var components = URLComponents(string: base) else { return nil }
        components.path = "/"
        var items: [URLQueryItem] = []
        if let token, !token.isEmpty { items.append(URLQueryItem(name: "token", value: token)) }
        if let session, !session.isEmpty { items.append(URLQueryItem(name: "session", value: session)) }
        components.queryItems = items.isEmpty ? nil : items
        return components.url
    }
}
