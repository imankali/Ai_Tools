package com.agenthub.android

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * ذخیره‌ی امن «آدرس سرور» و «توکن».
 *
 * به‌جای SharedPreferences ساده، مقدارها با کلیدی که در Android Keystore (سخت‌افزار/TEE)
 * ساخته می‌شود AES-GCM رمزنگاری می‌شوند؛ یعنی روت‌شدن دستگاه + خواندن فایل هم
 * توکن را لو نمی‌دهد. اگر Keystore در دسترس نباشد، بی‌صدا به حالت ساده برمی‌گردیم
 * (روی emulator‌های قدیمی) تا برنامه نشکند.
 */
class Prefs(context: Context) {

    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    private val fallback = context.getSharedPreferences(PREFS_PLAIN, Context.MODE_PRIVATE)

    /** آدرس پایه‌ی سرور بدون / انتهایی (مثلاً http://192.168.1.10:8765). */
    var serverUrl: String?
        get() = prefs.getString(KEY_URL, null) ?: fallback.getString(KEY_URL, null)
        set(value) {
            prefs.edit().putString(KEY_URL, value?.trimEnd('/')).apply()
        }

    /** توکن دسترسی؛ رشته‌ی خالی یعنی «بدون احراز هویت» (شبکه‌ی مطمئن). */
    var token: String?
        get() = decode(prefs.getString(KEY_TOKEN_ENC, null)) ?: fallback.getString(KEY_TOKEN, null)
        set(value) {
            val editor = prefs.edit()
            if (value.isNullOrBlank()) {
                editor.remove(KEY_TOKEN_ENC)
            } else {
                encode(value)?.let { editor.putString(KEY_TOKEN_ENC, it) }
            }
            editor.apply()
            fallback.edit().putString(KEY_TOKEN, if (encode(value) == null) value else null).apply()
        }

    /** آیا کاربر قبلاً اتصال را ذخیره کرده است؟ */
    fun hasConnection(): Boolean = !serverUrl.isNullOrBlank()

    fun forget() {
        prefs.edit().clear().apply()
        fallback.edit().clear().apply()
    }

    // ------------------------------------------------------------------- رمزنگاری

    private fun encode(plain: String): String? =
        runCatching {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, secretKey())
            val iv = cipher.iv
            Base64.encodeToString(iv + cipher.doFinal(plain.toByteArray(Charsets.UTF_8)), Base64.NO_WRAP)
        }.getOrNull()

    private fun decode(blob: String?): String? =
        blob?.let {
            runCatching {
                val raw = Base64.decode(it, Base64.NO_WRAP)
                val iv = raw.copyOfRange(0, IV_LENGTH)
                val body = raw.copyOfRange(IV_LENGTH, raw.size)
                val cipher = Cipher.getInstance(TRANSFORMATION)
                cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(TAG_BITS, iv))
                String(cipher.doFinal(body), Charsets.UTF_8)
            }.getOrNull()
        }

    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build()
        )
        val key = generator.generateKey()
        keyStore.setEntry(KEY_ALIAS, KeyStore.SecretKeyEntry(key), null)
        return key
    }

    private companion object {
        const val PREFS = "hub_prefs"
        const val PREFS_PLAIN = "hub_prefs_plain"
        const val KEY_URL = "server_url"
        const val KEY_TOKEN_ENC = "token_enc"
        const val KEY_TOKEN = "token"
        const val KEY_ALIAS = "hub_token_key"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_LENGTH = 12
        const val TAG_BITS = 128
    }
}
