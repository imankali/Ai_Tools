import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// فرم اتصال: آدرس سرور + توکن، با سنجش زنده.
struct PairingView: View {
    @EnvironmentObject private var model: HubModel
    @State private var url: String = ""
    @State private var token: String = ""
    @State private var remember: Bool = true
    @State private var busy: Bool = false
    @FocusState private var focus: Field

    private enum Field { case url, token }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text(NSLocalizedString("pair.title", comment: ""))
                        .font(.title2.weight(.semibold))
                        .padding(.top, 8)

                    field(title: NSLocalizedString("pair.server", comment: ""), text: $url, field: .url,
                          placeholder: NSLocalizedString("pair.server.placeholder", comment: ""))
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled(true)

                    SecureField(NSLocalizedString("pair.token", comment: ""), text: $token)
                        .textFieldStyle(.roundedBorder)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled(true)
                        .focused($focus, equals: .token)

                    HStack(spacing: 12) {
                        Button { paste() } label: { Label("Paste", systemImage: "doc.on.clipboard") }
                            .buttonStyle(.bordered)
                        Button { Task { await test() } } label: {
                            Text(busy ? NSLocalizedString("pair.testing", comment: "") : NSLocalizedString("pair.test", comment: ""))
                        }
                        .buttonStyle(.bordered)
                        .disabled(busy)
                        Spacer()
                    }

                    Toggle(NSLocalizedString("pair.remember", comment: ""), isOn: $remember)
                        .toggleStyle(.switch)

                    if !model.probe.message.isEmpty {
                        Text(model.probe.message)
                            .font(.footnote)
                            .foregroundStyle(model.probe.isGood ? Color.green : Color.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    Button {
                        Task {
                            await test()
                            // حتی اگر آماده نیست (کلید مدل تنظیم نشده) اجازه‌ی ورود بده:
                            // تنظیمش هم داخل همان UI انجام می‌شود
                            if model.probe.isGood { connect() }
                        }
                    } label: {
                        Text(NSLocalizedString("pair.connect", comment: ""))
                            .font(.headline)
                            .frame(maxWidth: .infinity)
                            .padding(.vertical, 6)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)

                    VStack(alignment: .leading, spacing: 6) {
                        Text(NSLocalizedString("pair.command", comment: "")).font(.caption).bold()
                        Text("agent-hub --serve --lan --print-token")
                            .font(.system(.caption, design: .monospaced))
                            .padding(8)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color(white: 0.12), in: RoundedRectangle(cornerRadius: 8))
                            .onTapGesture { copy("agent-hub --serve --lan --print-token") }
                        Text(NSLocalizedString("pair.security", comment: ""))
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.top, 6)
                }
                .padding(20)
            }
            .navigationTitle(NSLocalizedString("app.title", comment: ""))
            .onAppear(perform: loadSaved)
            #if canImport(UIKit)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button { paste() } label: { Image(systemName: "doc.on.clipboard") }
                }
            }
            #endif
        }
    }

    @ViewBuilder
    private func field(title: String, text: Binding<String>, field: Field, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            TextField(placeholder, text: text).textFieldStyle(.roundedBorder).focused($focus, equals: field)
        }
    }

    private func loadSaved() {
        url = Credentials.serverURL ?? url
        token = Credentials.token() ?? token
    }

    private func paste() {
        #if canImport(UIKit)
        let text = UIPasteboard.general.string?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else { return }
        if text.contains("://") || text.range(of: "^[0-9].*:[0-9]+$", options: .regularExpression) != nil {
            url = text
            focus = .token
        } else {
            token = text
        }
        #endif
    }

    private func copy(_ text: String) {
        #if canImport(UIKit)
        UIPasteboard.general.string = text
        #endif
    }

    private func test() async {
        busy = true
        await model.test(url: url, token: token)
        busy = false
    }

    private func connect() {
        model.connect(url: url, token: token, remember: remember)
    }
}
