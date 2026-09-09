package com.agenthub.android

import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Build
import android.os.VibrationEffect
import android.os.Vibrator
import android.os.VibratorManager
import android.webkit.JavascriptInterface
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import org.json.JSONObject
import java.util.concurrent.atomic.AtomicInteger

/**
 * پل Kotlin ↔ JavaScript.
 *
 * UI وب (که از سرور سرو می‌شود) وقتی گوشی در پس‌زمینه است به `window.HubNative`
 * دست می‌یابد تا کارهای رابطی را به سیستم‌عامل بسپارد: اعلان بومی، لرزش و کپی.
 * این پل هیچ قدرتی برای اجرای کد یا فایل ندارد؛ فقط اعلان و کلیپ‌بورد.
 */
class HubBridge(private val context: Context) {

    private val nextNotificationId = AtomicInteger(NOTIFICATION_ID_BASE)

    /** اطلاعات پلتفرم — UI از آن برای انتخاب رفتار بومی استفاده می‌کند. */
    @JavascriptInterface
    fun platform(): String = "android-${Build.VERSION.SDK_INT}"

    /** نمایش اعلان؛ `payload` یک JSON با `title`، `body` و `urgent` است. */
    @JavascriptInterface
    fun notify(payload: String?): String {
        val json = runCatching { JSONObject(payload ?: "{}") }.getOrElse { JSONObject() }
        val title = json.optString("title", context.getString(R.string.notif_approval_title))
        val body = json.optString("body", "")
        val urgent = json.optBoolean("urgent", false)
        ensureChannel()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(NotificationCompat.BigTextStyle().bigText(body))
            .setAutoCancel(true)
            .setOnlyAlertOnce(!urgent)
            .setPriority(if (urgent) NotificationCompat.PRIORITY_HIGH else NotificationCompat.PRIORITY_DEFAULT)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(nextNotificationId.getAndIncrement(), notification) }
        if (urgent) vibrate(null)
        return "ok"
    }

    /** کپی متن در کلیپ‌بورد (خروجی بلند دستور، آدرس دعوت و …). */
    @JavascriptInterface
    fun copy(text: String?) {
        val manager = context.getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager ?: return
        manager.setPrimaryClip(ClipData.newPlainText(context.getString(R.string.app_name), text ?: ""))
    }

    /** لرزش؛ `pattern` فهرست میلی‌ثانیه با ویرگول (مثلاً `"0,60,80,40"`). */
    @JavascriptInterface
    fun vibrate(pattern: String?) {
        val vibrator = resolveVibrator() ?: return
        if (!vibrator.hasVibrator()) return
        val timings = pattern?.split(",")?.mapNotNull { it.trim().toLongOrNull() }?.filter { it > 0 }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val effect = if (timings != null && timings.size > 1) {
                VibrationEffect.createWaveform(timings.toLongArray(), -1)
            } else {
                VibrationEffect.createOneShot(timings?.firstOrNull() ?: 40L, VibrationEffect.DEFAULT_AMPLITUDE)
            }
            vibrator.vibrate(effect)
        } else {
            @Suppress("DEPRECATION")
            vibrator.vibrate(timings?.firstOrNull() ?: 40L)
        }
    }

    /** برچسب برنامه (UI از آن برای عنوان اعلان سفارشی استفاده می‌کند). */
    @JavascriptInterface
    fun appLabel(): String = context.getString(R.string.app_name)

    /** اعلان ماندگار «ایجنت در حال کار است» تا کاربر اتصال را نبندد. */
    fun showRunning(text: String) {
        ensureChannel()
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher_foreground)
            .setContentTitle(context.getString(R.string.notif_running))
            .setContentText(text)
            .setOngoing(true)
            .setSilent(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .setVisibility(NotificationCompat.VISIBILITY_PRIVATE)
            .build()
        runCatching { NotificationManagerCompat.from(context).notify(RUNNING_NOTIFICATION_ID, notification) }
    }

    /** پاک‌کردن اعلان «در حال کار». */
    fun clearRunning() {
        runCatching { NotificationManagerCompat.from(context).cancel(RUNNING_NOTIFICATION_ID) }
    }

    private fun ensureChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        if (manager.getNotificationChannel(CHANNEL_ID) != null) return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(R.string.notif_channel_name),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply { description = context.getString(R.string.notif_channel_desc) }
        manager.createNotificationChannel(channel)
    }

    private fun resolveVibrator(): Vibrator? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(VibratorManager::class.java)?.defaultVibrator
        } else {
            @Suppress("DEPRECATION")
            context.getSystemService(Vibrator::class.java)
        }

    companion object {
        /** کانال اعلان‌ها — در تنظیمات اندروید با همین نام دیده می‌شود. */
        const val CHANNEL_ID = "agenthub.approvals"
        private const val NOTIFICATION_ID_BASE = 1000
        private const val RUNNING_NOTIFICATION_ID = 1
    }
}
