import SwiftUI

/// ریشه‌ی برنامه: بین صفحه‌ی اتصال و هاب جابه‌جا می‌شود.
struct RootView: View {
    @EnvironmentObject private var model: HubModel

    var body: some View {
        Group {
            if model.shouldPair {
                PairingView()
            } else {
                HubContainerView()
            }
        }
        .animation(.easeInOut(duration: 0.2), value: model.shouldPair)
    }
}
