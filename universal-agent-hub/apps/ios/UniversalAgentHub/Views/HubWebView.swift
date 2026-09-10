import SwiftUI
import WebKit
/// `WKWebView` درون SwiftUI، با پل `HubNative` برای اعلان/کپی.
///
/// * توکن فقط یک‌بار در URL می‌آید و UI آن را از تاریخچه پاک می‌کند؛
/// * کش `LOAD_NO_CACHE` نیست — Service Worker خودش استراتژی کش را دارد، ولی
///   کش HTTP را کوتاه نگه می‌داریم تا آپدیت UI دیر نشود؛
/// * اگر بارگذاری اصلی شکست بخورد، روی پوشش «آفلاین» پیام می‌دهیم.
struct HubWebView: UIViewRepresentable {
    let url: URL
    let reloadToken: Int
    @Binding var failed: Bool

    func makeCoordinator() -> Coordinator { Coordinator(failed: $failed) }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.defaultWebpagePreferences.allowsContentJavaScript = true
        configuration.websiteDataStore = .default()
        configuration.userContentController.add(context.coordinator, name: HubWebView.bridgeName)

        // `window.HubNative` — همان قراردادی که نسخه‌ی اندروید با JavascriptInterface می‌دهد
        let script = WKUserScript(
            source: HubWebView.bridgeJS,
            injectionTime: .atDocumentStart,
            forMainFrameOnly: true
        )
        configuration.userContentController.addUserScript(script)

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsLinkPreview = false
        webView.isOpaque = false
        webView.backgroundColor = .clear
        #if DEBUG
        if #available(iOS 16.4, *) { webView.isInspectable = true }
        #endif
        context.coordinator.webView = webView
        return webView
    }

    func updateUIView(_ webView: WKWebView, context: Context) {
        if context.coordinator.lastLoaded != url.absoluteString + "|\(reloadToken)" {
            context.coordinator.lastLoaded = url.absoluteString + "|\(reloadToken)"
            failed = false
            webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
        }
    }

    static func dismantleUIView(_ webView: WKWebView, coordinator: Coordinator) {
        webView.configuration.userContentController.removeScriptMessageHandler(forName: bridgeName)
        webView.stopLoading()
    }

    /// نام پیام‌رسان بومی (در app.js هم همین نام چک می‌شود).
    static let bridgeName = "HubNative"

    /// شیمی JS که `window.HubNative` را با API اندروید یکسان می‌کند.
    static let bridgeJS = """
    window.HubNative = {
      platform: function () { return 'ios'; },
      notify: function (payload) {
        try { window.webkit.messageHandlers.HubNative.postMessage(payload); } catch (e) {}
      },
      copy: function (text) {
        try { window.webkit.messageHandlers.HubNative.postMessage(JSON.stringify({ action: 'copy', text: text })); } catch (e) {}
      },
      vibrate: function () {
        try { window.webkit.messageHandlers.HubNative.postMessage(JSON.stringify({ action: 'vibrate' })); } catch (e) {}
      }
    };
    """

    final class Coordinator: NSObject, WKNavigationDelegate, WKScriptMessageHandler {
        weak var webView: WKWebView?
        var lastLoaded = ""
        private let failed: Binding<Bool>

        init(failed: Binding<Bool>) { self.failed = failed }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            DispatchQueue.main.async { self.failed.wrappedValue = true }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            DispatchQueue.main.async { self.failed.wrappedValue = true }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            DispatchQueue.main.async { self.failed.wrappedValue = false }
        }

        /// لینک‌های بیرونی را در سافاری باز می‌کنیم تا صفحه فقط هاب بماند.
        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            guard let target = navigationAction.request.url else { return decisionHandler(.allow) }
            if target.scheme == "http" || target.scheme == "https" { return decisionHandler(.allow) }
            // طرح‌های دیگر (mailto, tel, agenthub, itms…) را سیستم‌عامل هندل می‌کند
            #if canImport(UIKit)
            UIApplication.shared.open(target)
            #endif
            decisionHandler(.cancel)
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.name == HubWebView.bridgeName else { return }
            if let body = message.body as? String, let data = body.data(using: .utf8),
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                switch json["action"] as? String {
                case "copy":
                    #if canImport(UIKit)
                    UIPasteboard.general.string = (json["text"] as? String) ?? ""
                    #endif
                case "vibrate":
                    #if canImport(UIKit)
                    AudioServicesPlaySystemSound(1521)
                    #endif
                default:
                    Notifier.handle(payload: json)
                }
                return
            }
            if let dict = message.body as? [String: Any] { Notifier.handle(payload: dict) }
        }
    }
}

#if canImport(UIKit)
import UIKit
import AudioToolbox
#endif
