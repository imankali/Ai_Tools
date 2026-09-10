package com.agenthub.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.agenthub.android.databinding.ActivityPairBinding

/**
 * صفحه‌ی «اتصال به کامپیوتر».
 *
 * کاربر آدرس سرور و توکن دسترسی را وارد می‌کند (یا از کلیپ‌بورد/لینک `agenthub://`
 * می‌گیرد)؛ اتصال با `GET /api/status` سنجیده می‌شود و بعد WebView باز می‌شود.
 *
 * نکته‌ی امنیتی: اینجا هرگز کلید API مدل پرسیده نمی‌شود. کلید مدل روی همان
 * کامپیوتر می‌ماند (از تب Keys در UI) و توکن فقط حقِ دسترسی به همان هاب است.
 */
class PairActivity : AppCompatActivity() {

    private lateinit var binding: ActivityPairBinding
    private lateinit var prefs: Prefs
    private val api = ServerApi()
    private val mainHandler = Handler(Looper.getMainLooper())
    private var pendingSession: String? = null

    /** اجازه‌ی اعلان (اندروید ۱۳ به بعد). رد کردنش هم چیزی را خراب نمی‌کند. */
    private val notificationPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* نتیجه اختیاری است */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = Prefs(this)
        binding = ActivityPairBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setTitle(R.string.pair_title)

        binding.commandText.typeface = Typeface.MONOSPACE
        binding.commandText.setTextIsSelectable(true)
        binding.commandText.setOnClickListener { copyToClipboard(binding.commandText.text.toString()) }
        binding.pasteButton.setOnClickListener { pasteFromClipboard() }
        binding.testButton.setOnClickListener { testConnection(autoConnect = false) }
        binding.connectButton.setOnClickListener { testConnection(autoConnect = true) }

        val saved = prefs.serverUrl
        if (!saved.isNullOrBlank()) {
            binding.serverUrl.setText(saved)
            prefs.token?.let { binding.accessToken.setText(it) }
            binding.remember.isChecked = true
        } else {
            binding.serverUrl.setText(BuildConfig.DEFAULT_SERVER_URL)
        }

        consumeDeepLink(intent)
        requestNotificationPermissionIfNeeded()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        consumeDeepLink(intent)
    }

    override fun onDestroy() {
        api.close()
        super.onDestroy()
    }

    /** لینک `agenthub://connect?url=…&token=…&session=…` (QR، پیام، ایمیل). */
    private fun consumeDeepLink(intent: Intent?) {
        val data: Uri = intent?.data ?: return
        if (data.scheme != "agenthub" || data.host != "connect") return
        data.getQueryParameter("url")?.takeIf { it.isNotBlank() }?.let { binding.serverUrl.setText(it) }
        data.getQueryParameter("token")?.let { binding.accessToken.setText(it) }
        pendingSession = data.getQueryParameter("session")?.takeIf { it.isNotBlank() }
        if (!data.getQueryParameter("url").isNullOrBlank()) testConnection(autoConnect = true)
    }

    private fun pasteFromClipboard() {
        val manager = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        val text = manager.primaryClip?.takeIf { it.itemCount > 0 }?.getItemAt(0)?.coerceToText(this)?.toString().orEmpty().trim()
        if (text.isEmpty()) {
            toast(getString(R.string.pair_clipboard_empty))
            return
        }
        // آدرس‌ها را در فیلد آدرس و بقیه را در فیلد توکن می‌گذاریم
        if (text.contains("://") || Regex("^[\\d.]+(:\\d+)?$").matches(text.substringBefore("/"))) {
            binding.serverUrl.setText(text.lineSequence().first { it.isNotBlank() }.trim())
        } else {
            binding.accessToken.setText(text.lineSequence().first { it.isNotBlank() }.trim())
        }
    }

    private fun copyToClipboard(value: String) {
        val manager = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        manager.setPrimaryClip(ClipData.newPlainText(getString(R.string.app_name), value))
        toast(getString(R.string.copied))
    }

    /** معتبرسازی ساده: فقط `scheme://host[:port]` (یا host:port) می‌پذیریم. */
    private fun validatedUrl(): String? {
        val raw = binding.serverUrl.text.toString().trim()
        if (raw.isEmpty() || raw.any { it.isWhitespace() }) return null
        val hostPart = raw.substringAfter("://", raw).substringBefore("/")
        if (!hostPart.contains(":")) return null // میزبان بدون پورت، فقط با http(s) معنادار است
        val host = hostPart.substringBefore(":")
        val port = hostPart.substringAfter(":").toIntOrNull() ?: return null
        if (host.isEmpty() || port !in 1..65_535) return null
        return ServerApi.normalize(raw)
    }

    private fun testConnection(autoConnect: Boolean) {
        val base = validatedUrl()
        if (base == null) {
            setStatus(getString(R.string.pair_err_badurl), error = true)
            return
        }
        val token = binding.accessToken.text.toString().trim()
        setBusy(busy = true)
        setStatus(getString(R.string.pair_testing), error = false)

        api.probe(base, token.ifBlank { null }) { result ->
            mainHandler.post {
                setBusy(busy = false)
                when {
                    result.ok && result.ready -> {
                        setStatus(getString(R.string.pair_ok_online, result.toolCount, result.model ?: "?"), error = false)
                        if (autoConnect) openHub(base, token)
                    }

                    // سرور سالم است ولی هنوز کلید مدل تنظیم نشده — بگذارید وارد شود
                    result.ok -> {
                        setStatus(getString(R.string.pair_ok_offline), error = false)
                        if (autoConnect) openHub(base, token)
                    }

                    result.unauthorized -> setStatus(getString(R.string.pair_err_unauthorized), error = true)
                    else -> setStatus(getString(R.string.pair_err_refused), error = true)
                }
            }
        }
    }

    private fun setBusy(busy: Boolean) {
        binding.progress.visibility = if (busy) View.VISIBLE else View.GONE
        binding.testButton.isEnabled = !busy
        binding.connectButton.isEnabled = !busy
    }

    private fun openHub(base: String, token: String) {
        if (binding.remember.isChecked) {
            prefs.serverUrl = base
            prefs.token = token.ifBlank { null }
        } else {
            prefs.forget()
        }
        startActivity(MainActivity.intent(this, base, token, pendingSession))
        finish()
    }

    private fun setStatus(text: String, error: Boolean) {
        binding.statusText.text = text
        binding.statusText.setTextColor(ContextCompat.getColor(this, if (error) R.color.hub_danger else R.color.hub_ok))
    }

    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as? android.app.NotificationManager
        if (manager?.areNotificationsEnabled() == true) return
        notificationPermission.launch(android.Manifest.permission.POST_NOTIFICATIONS)
    }
}
