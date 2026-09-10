import SwiftUI

/// پوسته‌ی هاب: WebView + نوار ابزار + پوشش آفلاین.
struct HubContainerView: View {
    @EnvironmentObject private var model: HubModel
    @State private var failed = false
    @State private var showAbout = false

    var body: some View {
        NavigationStack {
            ZStack {
                if let url = model.pageURL {
                    HubWebView(url: url, reloadToken: model.reloadToken, failed: $failed)
                        .ignoresSafeArea(edges: .bottom)
                }

                if failed {
                    offlineCard
                }
            }
            .navigationTitle(Text(NSLocalizedString("app.title", comment: "")))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItemGroup(placement: .navigationBarTrailing) {
                    Button { model.reload(); failed = false } label: { Image(systemName: "arrow.clockwise") }
                    Menu {
                        Button(NSLocalizedString("hub.reload", comment: "")) { model.reload() }
                        Button(NSLocalizedString("hub.browser", comment: "")) { openInSafari() }
                        Divider()
                        Button(NSLocalizedString("hub.reconnect", comment: ""), role: .destructive) {
                            model.disconnect()
                        }
                        Divider()
                        Button(NSLocalizedString("hub.about", comment: "")) { showAbout = true }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                }
            }
            .sheet(isPresented: $showAbout) {
                VStack(spacing: 14) {
                    Text("Universal Agent Hub").font(.headline)
                    Text(NSLocalizedString("about.body", comment: ""))
                        .font(.callout)
                        .multilineTextAlignment(.center)
                    Button("OK") { showAbout = false }
                }
                .padding(24)
                .presentationDetents([.height(220)])
            }
        }
    }

    private var offlineCard: some View {
        VStack(spacing: 12) {
            Image(systemName: "wifi.slash").font(.largeTitle)
            Text(NSLocalizedString("hub.offline", comment: "")).font(.title3.bold())
            Text(String(format: NSLocalizedString("hub.offline.body", comment: ""), model.serverURL))
                .font(.footnote)
                .multilineTextAlignment(.center)
            Button(NSLocalizedString("hub.retry", comment: "")) {
                failed = false
                model.reload()
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(24)
        .frame(maxWidth: 380)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 18))
        .padding()
    }

    private func openInSafari() {
        #if canImport(UIKit)
        if let url = model.pageURL { UIApplication.shared.open(url) }
        #endif
    }

}
