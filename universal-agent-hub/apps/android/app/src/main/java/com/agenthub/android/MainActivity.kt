package com.agenthub.android

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.net.http.SslError
import android.os.Build
import android.os.Bundle
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.webkit.SslErrorHandler
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.agenthub.android.databinding.ActivityMainBinding

/**
 * پنجره‌ی اصلی: همان UI وبِ سرویس‌شده از کامپیوتر، داخل WebView.
 *
 * * توکن با `?token=` به صفحه داده می‌شود؛ UI آن را در localStorage می‌گذارد و
 *   آدرس را از تاریخچه پاک می‌کند (تا توکن در تاریخچه‌ی WebView نماند)؛
 * * اگر سرور در دسترس نبود، به‌جای صفحه‌ی خطای WebView، پوشش «ایجنت آفلاین» با
 *   دکمه‌ی تلاش دوباره نشان داده می‌شود؛
 * * لینک‌های بیرونی در مرورگر سیستم باز می‌شوند تا این پنجره فقط هاب بماند.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs
    private var bridge: HubBridge? = null
    private var serverUrl: String = ""
    private var token: String = ""
    private var session: String? = null
    private var loadFailed = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        serverUrl = intent.getStringExtra(EXTRA_URL) ?: prefs.serverUrl.orEmpty()
        token = intent.getStringExtra(EXTRA_TOKEN) ?: prefs.token.orEmpty()
        session = intent.getStringExtra(EXTRA_SESSION)

        if (serverUrl.isBlank()) {
            startActivity(Intent(this, PairActivity::class.java))
            finish()
            return
        }

        configureWebView(binding.webView)
        binding.refresh.setColorSchemeColors(getColor(R.color.hub_primary))
        binding.refresh.setOnRefreshListener { load() }
        binding.retryButton.setOnClickListener { load() }
        load()
    }

    override fun onPause() {
        binding.webView.onPause()
        super.onPause()
    }

    override fun onResume() {
        super.onResume()
        binding.webView.onResume()
        // اگر آخرین بار سرور در دسترس نبود، با برگشت کاربر دوباره تلاش می‌کنیم
        if (loadFailed) load()
    }

    override fun onDestroy() {
        bridge?.clearRunning()
        binding.webView.destroy()
        super.onDestroy()
    }

    /** دکمه‌ی back: اول داخل تاریخچه‌ی مرورگر عقب برود، بعد برنامه. */
    @Suppress("DEPRECATION")
    override fun onBackPressed() {
        if (binding.webView.canGoBack()) binding.webView.goBack() else super.onBackPressed()
    }

    // ------------------------------------------------------------------ WebView

    @SuppressLint("SetJavaScriptEnabled")
    private fun configureWebView(webView: WebView) {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            // UI باید همیشه تازه باشد (Service Worker خودش کش را مدیریت می‌کند)
            cacheMode = WebSettings.LOAD_NO_CACHE
            mediaPlaybackRequiresUserGesture = false
            javaScriptCanOpenWindowsAutomatically = true
            textZoom = 100
        }
        bridge = HubBridge(applicationContext).also { webView.addJavascriptInterface(it, BRIDGE_NAME) }
        webView.setBackgroundColor(getColor(R.color.hub_background))
        webView.webChromeClient = object : WebChromeClient() {
            override fun onProgressChanged(view: WebView?, progress: Int) {
                binding.progress.progress = progress
                binding.progress.visibility = if (progress in 1..99) View.VISIBLE else View.GONE
            }
        }
        webView.webViewClient = HubWebViewClient()
        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true)
    }

    /** آدرس صفحه‌ی UI + توکن/سشن به‌صورت پارامتر (UI خودش آن‌ها را از URL پاک می‌کند). */
    private fun hubPageUrl(): String {
        val parsed = Uri.parse(serverUrl)
        val builder = Uri.Builder()
            .scheme(parsed.scheme ?: "http")
            .encodedAuthority(parsed.encodedAuthority ?: return serverUrl)
            .encodedPath("/")
        if (token.isNotBlank()) builder.appendQueryParameter("token", token)
        session?.takeIf { it.isNotBlank() }?.let { builder.appendQueryParameter("session", it) }
        return builder.build().toString()
    }

    private fun load() {
        loadFailed = false
        binding.offline.visibility = View.GONE
        binding.webView.loadUrl(hubPageUrl())
    }

    /** هندلرهای native برای UI (اعلان، کپی، لرزش) از طریق `window.HubNative`. */
    private inner class HubWebViewClient : WebViewClient() {

        override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
            val url = request.url ?: return false
            val sameHub = url.host != null && url.host == Uri.parse(serverUrl).host
            return when {
                sameHub -> false // همه‌ی مسیرهای هاب داخل همان WebView می‌مانند
                url.scheme == "agenthub" || url.scheme == "mailto" || url.scheme == "tel" -> openExternal(url).let { true }
                else -> openExternal(url).let { true }
            }
        }

        override fun onReceivedError(view: WebView, request: WebResourceRequest, error: WebResourceError) {
            if (request.isForMainFrame) showError()
        }

        override fun onReceivedSslError(view: WebView, handler: SslErrorHandler, error: SslError?) {
            // LAN با گواهی self-signed: فقط برای میزبان محلی ادامه می‌دهیم
            if (error != null && ServerApi.isLocalHost(Uri.parse(error.url).host)) handler.proceed() else handler.cancel()
        }
    }

    private fun openExternal(url: Uri): Unit = runCatching {
        startActivity(Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
    }.getOrDefault(Unit)

    private fun showError() {
        loadFailed = true
        binding.refresh.isRefreshing = false
        binding.offline.visibility = View.VISIBLE
        binding.offlineText.text = getString(R.string.offline_body, serverUrl)
    }

    // ------------------------------------------------------------------ منو

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        when (item.itemId) {
            R.id.action_reload -> load()
            R.id.action_reconnect -> {
                prefs.forget()
                startActivity(Intent(this, PairActivity::class.java))
                finish()
            }

            R.id.action_browser -> openExternal(Uri.parse(serverUrl))
            R.id.action_language -> openSystemLocaleSettings()
            R.id.action_about -> AlertDialog.Builder(this)
                .setTitle(BuildConfig.BRAND_NAME)
                .setMessage(R.string.about_body)
                .setPositiveButton(android.R.string.ok, null)
                .show()
        }
        return true
    }

    private fun openSystemLocaleSettings() {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            Intent(android.provider.Settings.ACTION_APP_LOCALE_SETTINGS, Uri.fromParts("package", packageName, null))
        } else {
            Intent(android.provider.Settings.ACTION_LOCALE_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null))
        }
        runCatching { startActivity(intent) }
    }

    companion object {
        private const val EXTRA_URL = "hub.url"
        private const val EXTRA_TOKEN = "hub.token"
        private const val EXTRA_SESSION = "hub.session"

        /** نام شیء‌ای که در JavaScript به‌عنوان `window.HubNative` در دسترس است. */
        private const val BRIDGE_NAME = "HubNative"

        /** ساخت Intent اتصال؛ مقادیر خالی یعنی «از تنظیمات ذخیره‌شده استفاده کن». */
        fun intent(context: Context, url: String, token: String, session: String?): Intent =
            Intent(context, MainActivity::class.java)
                .putExtra(EXTRA_URL, url)
                .putExtra(EXTRA_TOKEN, token)
                .putExtra(EXTRA_SESSION, session)
    }
}
