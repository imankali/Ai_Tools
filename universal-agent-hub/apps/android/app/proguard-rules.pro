# Universal Agent Hub — ProGuard/R8 rules
# پل JavaScript↔Kotlin با رفلکس کار می‌کند؛ نامش نباید تغییر کند.
-keepclassmembers class com.agenthub.android.HubBridge { public *; }
-keepattributes JavascriptInterface
-keepattributes *Annotation*
# مدل‌های JSON (org.json) نیازی به keep ندارند؛ فقط WebViewClient ها
-keep class android.webkit.** { *; }
-dontwarn org.apache.http.**
