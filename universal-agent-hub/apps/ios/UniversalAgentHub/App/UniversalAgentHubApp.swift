import SwiftUI
import UserNotifications

@main
struct UniversalAgentHubApp: App {
    @StateObject private var model = HubModel()

    init() {
        Notifier.requestAuthorization()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .onOpenURL { url in model.handle(link: url) }   // agenthub://connect?url=…&token=…
        }
    }
}

/// وضعیت مشترک بین صفحه‌ی اتصال و WebView.
@MainActor
final class HubModel: ObservableObject {
    @Published var serverURL: String = Credentials.serverURL ?? ""
    @Published var token: String = Credentials.token() ?? ""
    @Published var connected: Bool = Credentials.hasConnection
    @Published var probe: ProbeResult = .idle
    /// شمارنده‌ی Reload: با هر بار زیادشدن، `updateUIView` دوباره load می‌کند.
    @Published var reloadToken: Int = 0

    private let api = ServerAPI()

    func test(url: String, token: String) async {
        probe = .checking
        let base = Credentials.normalize(url)
        probe = await api.probe(base: base, token: token.isEmpty ? nil : token)
    }

    /// ذخیره و بازکردن هاب.
    func connect(url: String, token: String, remember: Bool) {
        let base = Credentials.normalize(url)
        serverURL = base
        self.token = token
        if remember {
            Credentials.serverURL = base
            Credentials.setToken(token.isEmpty ? nil : token)
        } else {
            Credentials.forget()
        }
        connected = true
    }

    func disconnect() {
        Credentials.forget()
        serverURL = ""
        token = ""
        connected = false
        probe = .idle
    }

    func reload() {
        reloadToken += 1
    }

    /// لینک عمیق: `agenthub://connect?url=…&token=…&session=…`
    func handle(link url: URL) {
        guard url.scheme == "agenthub" else { return }
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? { query.first { $0.name == name }?.value }
        guard let server = value("url"), !server.isEmpty else { return }
        serverURL = Credentials.normalize(server)
        if let access = value("token") { token = access }
        Credentials.sessionID = value("session")
        connect(url: serverURL, token: token, remember: true)
    }

    /// آدرس ذخیره‌شده نداریم → اول صفحه‌ی اتصال را نشان بده.
    var shouldPair: Bool { Credentials.serverURL?.isEmpty ?? true }

    var pageURL: URL? {
        guard let base = Credentials.serverURL, !base.isEmpty else { return nil }
        return Credentials.hubPageURL(base: base, token: Credentials.token(), session: Credentials.sessionID)
    }
}
