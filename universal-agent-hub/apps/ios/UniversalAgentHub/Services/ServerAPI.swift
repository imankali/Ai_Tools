import Foundation

/// سنجش اتصال با `GET /api/status` — بدون کتابخانه‌ی خارجی.
///
/// روی شبکه‌ی محلی ممکن است گواهی self-signed باشد؛ برای میزبان‌های خصوصی
/// `URLSession` را در حالت «تحمل» می‌گذاریم (برای اینترنت، همان بررسی پیش‌فرض).
final class ServerAPI: NSObject, URLSessionDelegate {
    enum Failure: LocalizedError {
        case badURL
        case http(Int)
        case decoding(String)

        var errorDescription: String? {
            switch self {
            case .badURL: return NSLocalizedString("pair.err.url", comment: "")
            case let .http(code): return "HTTP \(code)"
            case let .decoding(text): return text
            }
        }
    }

    private lazy var session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 8
        configuration.waitsForConnectivity = false
        configuration.allowsCellularAccess = true
        configuration.httpAdditionalHeaders = ["Accept": "application/json"]
        return URLSession(configuration: configuration, delegate: self, delegateQueue: nil)
    }()

    /// بررسی وضعیت سرور. خطای شبکه را به `ProbeResult` تبدیل می‌کند (throw نمی‌کند).
    func probe(base: String, token: String?) async -> ProbeResult {
        guard let url = URL(string: base + "/api/status") else { return .unreachable("bad url") }
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        if let token, !token.isEmpty { request.setValue("Bearer " + token, forHTTPHeaderField: "Authorization") }
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { return .unreachable("no response") }
            if http.statusCode == 401 || http.statusCode == 403 { return .unauthorized }
            guard (200...299).contains(http.statusCode) else { return .unreachable("HTTP \(http.statusCode)") }
            do {
                let status = try JSONDecoder().decode(HubStatus.self, from: data)
                return .online(status)
            } catch {
                return .unreachable("bad json: " + String(describing: error))
            }
        } catch {
            return .unreachable(error.localizedDescription)
        }
    }

    /// گواهی self-signed روی LAN: فقط برای میزبان خصوصی قبول می‌شود.
    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              ServerAPI.isPrivateHost(challenge.protectionSpace.host),
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }

    /// `10.x`, `192.168.x`, `172.16–31.x`, `localhost`, `*.local`.
    static func isPrivateHost(_ host: String) -> Bool {
        let lowered = host.lowercased()
        if lowered == "localhost" || lowered.hasSuffix(".local") { return true }
        let parts = lowered.split(separator: ".")
        guard parts.count == 4, let first = Int(parts[0]), let second = Int(parts[1]) else { return false }
        return first == 10 || first == 127 || (first == 192 && second == 168) || (first == 172 && (16...31).contains(second))
    }
}
