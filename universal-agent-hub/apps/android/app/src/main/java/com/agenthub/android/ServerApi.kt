package com.agenthub.android

import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLSession

/**
 * کلاینت سبک REST برای صفحه‌ی «اتصال» — بدون کتابخانه‌ی خارجی.
 *
 * پیش از بازکردن WebView فقط به `/api/status` نیاز داریم تا مطمئن شویم آدرس و توکن
 * درست است و ایجنت واقعاً آماده است. کار شبکه روی thread pool اجرا می‌شود و نتیجه
 * به callback داده می‌شود (فعالیت آن را روی thread اصلی post می‌کند).
 */
class ServerApi {

    /** نتیجه‌ی بررسی اتصال. */
    data class Probe(
        val ok: Boolean,
        val status: Int,
        val error: String?,
        /** آیا سرور آماده‌ی اجرای ایجنت است (کلید API تنظیم شده)؟ */
        val ready: Boolean,
        /** نام مدل فعال، اگر سرور گفت. */
        val model: String?,
        /** چند ابزار روی این سرور ثبت شده است. */
        val toolCount: Int,
        /** توکن داده‌شده رد شده است (۴۰۱/۴۰۳). */
        val unauthorized: Boolean,
    )

    fun interface Callback {
        fun onResult(result: Probe)
    }

    private val executor = Executors.newFixedThreadPool(2)

    fun close() {
        executor.shutdownNow()
    }

    /** بررسی ناهم‌زمانی `/api/status`. */
    fun probe(baseUrl: String, token: String?, callback: Callback) {
        executor.execute { callback.onResult(probeBlocking(baseUrl, token)) }
    }

    /** نسخه‌ی هم‌زمان (برای foreground service و اعلان‌ها). */
    fun probeBlocking(baseUrl: String, token: String?): Probe {
        val url = normalize(baseUrl) + "/api/status"
        return try {
            val connection = URL(url).openConnection() as HttpURLConnection
            connection.apply {
                requestMethod = "GET"
                connectTimeout = TIMEOUT_MS
                readTimeout = TIMEOUT_MS
                setRequestProperty("Accept", "application/json")
                if (!token.isNullOrBlank()) setRequestProperty("Authorization", "Bearer $token")
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val body = stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
            connection.disconnect()
            when {
                status == 401 || status == 403 -> Probe(false, status, "unauthorized", false, null, 0, true)
                status in 200..299 -> {
                    val json = runCatching { JSONObject(body) }.getOrElse { JSONObject() }
                    Probe(
                        ok = true,
                        status = status,
                        error = null,
                        ready = json.optBoolean("ready", false),
                        model = json.optJSONObject("config")?.optString("model_name")?.takeIf { it.isNotBlank() },
                        toolCount = json.optInt("tool_count", 0),
                        unauthorized = false,
                    )
                }

                else -> Probe(false, status, "http_$status", false, null, 0, false)
            }
        } catch (error: Exception) {
            Probe(false, 0, error.javaClass.simpleName, false, null, 0, false)
        }
    }

    companion object {
        private const val TIMEOUT_MS = 6_000

        /** `192.168.1.5:8765`, `host:port/` و `http://…` → همیشه `scheme://host[:port]`. */
        fun normalize(raw: String): String {
            var value = raw.trim().trimEnd('/')
            if (!value.contains("://")) value = "http://$value"
            if (value.startsWith("https://")) installLocalHostnameVerifier()
            return value
        }

        /** آیا میزبان، محلی/شبکه‌ی خصوصی است؟ (برای http بدون TLS و گواهی self-signed) */
        fun isLocalHost(host: String?): Boolean {
            val value = host?.lowercase()?.substringBefore(':').orEmpty()
            if (value.isEmpty()) return false
            if (value == "localhost" || value.endsWith(".local")) return true
            val octets = value.split(".").map { it.toIntOrNull() ?: -1 }
            if (octets.size != 4) return false
            return octets[0] == 127 || octets[0] == 10 ||
                (octets[0] == 192 && octets[1] == 168) ||
                (octets[0] == 172 && octets[1] in 16..31)
        }

        private var verifierInstalled = false

        /**
         * روی LAN معمولاً گواهی self-signed است؛ بررسی نام میزبان را فقط برای آدرس‌های
         * محلی شل می‌کنیم (برای اینترنت همان قاعده‌ی پیش‌فرض جاوا می‌ماند).
         */
        private fun installLocalHostnameVerifier() {
            if (verifierInstalled) return
            verifierInstalled = true
            HttpsURLConnection.setDefaultHostnameVerifier(LocalOnlyVerifier)
        }

        private object LocalOnlyVerifier : HostnameVerifier {
            override fun verify(hostname: String?, session: SSLSession?): Boolean = isLocalHost(hostname)
        }
    }
}
