package com.stockguide.app.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class ServerSettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("server_settings_v1", Context.MODE_PRIVATE)
    private val secrets = KeystoreSecretStore()

    fun mode(): AppMode = if (prefs.getString("mode", "demo") == "server") AppMode.SERVER else AppMode.DEMO

    fun setMode(mode: AppMode) {
        prefs.edit().putString("mode", if (mode == AppMode.SERVER) "server" else "demo").apply()
    }

    fun loadSettings(): ServerSettings? {
        val url = prefs.getString("base_url", null)?.trim().orEmpty()
        val token = runCatching { secrets.decrypt(prefs.getString("api_token_ciphertext", null)) }.getOrNull().orEmpty()
        return if (url.isNotBlank() && token.isNotBlank()) ServerSettings(url, token) else null
    }

    fun saveSettings(settings: ServerSettings) {
        val encrypted = secrets.encrypt(settings.apiToken)
        prefs.edit().putString("base_url", settings.baseUrl.trim().trimEnd('/'))
            .putString("api_token_ciphertext", encrypted).apply()
    }

    fun savedBaseUrl(): String = prefs.getString("base_url", "http://10.0.2.2:8765").orEmpty()

    private class KeystoreSecretStore {
        private val alias = "stockguide_mobile_api_token_v1"

        fun encrypt(plain: String): String {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
            val encrypted = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
            return Base64.encodeToString(cipher.iv, Base64.NO_WRAP) + "." + Base64.encodeToString(encrypted, Base64.NO_WRAP)
        }

        fun decrypt(value: String?): String? {
            if (value.isNullOrBlank()) return null
            val parts = value.split('.', limit = 2)
            if (parts.size != 2) return null
            val iv = Base64.decode(parts[0], Base64.NO_WRAP)
            val ciphertext = Base64.decode(parts[1], Base64.NO_WRAP)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(128, iv))
            return cipher.doFinal(ciphertext).toString(Charsets.UTF_8)
        }

        private fun getOrCreateKey(): SecretKey {
            val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            (store.getKey(alias, null) as? SecretKey)?.let { return it }
            val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
            generator.init(
                KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .setRandomizedEncryptionRequired(true)
                    .build(),
            )
            return generator.generateKey()
        }

        private companion object { const val TRANSFORMATION = "AES/GCM/NoPadding" }
    }
}
