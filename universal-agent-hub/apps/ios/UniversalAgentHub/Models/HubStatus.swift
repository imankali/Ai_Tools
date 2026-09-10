import Foundation

/// پاسخ `GET /api/status` — همان چیزی که سرور برای هر اپی می‌سازد.
///
/// فقط فیلدهایی که برای «اتصال» لازم است تعریف شده‌اند؛ `Codable` با
/// `lossy` بودن کلیدها بی‌نقص است تا تغییرات سرور اپ را نشکند.
struct HubStatus: Decodable {
    var version: String?
    var ready: Bool
    var readyHint: String?
    var toolCount: Int?
    var uiAvailable: Bool?
    var stats: Stats?
    var config: Config?
    var capabilities: Capabilities?

    struct Stats: Decodable {
        var sockets: Int?
        var runs: Int?
        var approved: Int?
        var denied: Int?
    }

    struct Config: Decodable {
        var modelName: String?
        var safetyEnabled: Bool?
        var autoConfirmAll: Bool?
    }

    struct Capabilities: Decodable {
        var directToolCalls: Bool?
        var approvals: Bool?
        var websocket: Bool?
    }

    private enum CodingKeys: String, CodingKey {
        case version, ready, toolCount = "tool_count", uiAvailable = "ui_available", stats, config, capabilities
        case readyHint = "ready_hint"
    }

    private enum ConfigKeys: String, CodingKey {
        case modelName = "model_name", safetyEnabled = "safety_enabled", autoConfirmAll = "auto_confirm_all"
    }

    private enum CapabilityKeys: String, CodingKey {
        case directToolCalls = "direct_tool_calls", approvals, websocket
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        version = try container.decodeIfPresent(String.self, forKey: .version)
        ready = try container.decodeIfPresent(Bool.self, forKey: .ready) ?? false
        readyHint = try container.decodeIfPresent(String.self, forKey: .readyHint)
        toolCount = try container.decodeIfPresent(Int.self, forKey: .toolCount)
        uiAvailable = try container.decodeIfPresent(Bool.self, forKey: .uiAvailable)
        stats = try container.decodeIfPresent(Stats.self, forKey: .stats)

        if let configContainer = try? container.nestedContainer(keyedBy: ConfigKeys.self, forKey: .config) {
            config = Config(
                modelName: try configContainer.decodeIfPresent(String.self, forKey: .modelName),
                safetyEnabled: try configContainer.decodeIfPresent(Bool.self, forKey: .safetyEnabled),
                autoConfirmAll: try configContainer.decodeIfPresent(Bool.self, forKey: .autoConfirmAll)
            )
        } else {
            config = nil
        }

        if let capContainer = try? container.nestedContainer(keyedBy: CapabilityKeys.self, forKey: .capabilities) {
            capabilities = Capabilities(
                directToolCalls: try capContainer.decodeIfPresent(Bool.self, forKey: .directToolCalls),
                approvals: try capContainer.decodeIfPresent(Bool.self, forKey: .approvals),
                websocket: try capContainer.decodeIfPresent(Bool.self, forKey: .websocket)
            )
        }
    }
}

/// نتیجه‌ی سنجش اتصال در صفحه‌ی تنظیمات.
enum ProbeResult: Equatable {
    case idle
    case checking
    case online(HubStatus)
    case unauthorized
    case unreachable(String)

    var message: String {
        switch self {
        case .idle, .checking: return ""
        case let .online(status):
            let tools = status.toolCount ?? 0
            let model = status.config?.modelName ?? "?"
            return status.ready
                ? String(format: NSLocalizedString("pair.ok.online", comment: ""), tools, model)
                : NSLocalizedString("pair.ok.offline", comment: "")
        case .unauthorized: return NSLocalizedString("pair.err.token", comment: "")
        case let .unreachable(reason): return "\(NSLocalizedString("pair.err.refused", comment: "")) (\(reason))"
        }
    }

    var isGood: Bool {
        if case .online = self { return true }
        return false
    }
}
