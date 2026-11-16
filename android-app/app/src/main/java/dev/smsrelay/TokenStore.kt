package dev.smsrelay

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * The secrets of the app.
 *
 * Two values live here:
 *
 *  - the long lived device token, which authorises every request to the
 *    relay backend. It is a bearer credential: whoever has it can use the
 *    device.
 *  - the short lived claim secret of a running pairing.
 *
 * Both are sealed with AES-GCM under a key that lives in the Android
 * Keystore and never leaves it. The ciphertext sits in a private
 * preferences file; the plain value never touches the disk.
 *
 * The store is deliberately small and hand written: the platform Keystore
 * is enough for one key and one cipher, and the app does not need a
 * third party crypto library for that.
 *
 * Nothing in here is ever logged.
 */
class TokenStore(context: Context) {

    private val prefs: SharedPreferences = context.applicationContext
        .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** The device token of this phone, or an empty string. */
    var token: String
        get() = read(KEY_TOKEN)
        set(value) = write(KEY_TOKEN, value.trim())

    /** Drops the token: the device has to pair again. */
    fun clearToken() {
        prefs.edit().remove(KEY_TOKEN).apply()
    }

    /** The secret of a running pairing, or an empty string. */
    var claimSecret: String
        get() = read(KEY_CLAIM_SECRET)
        set(value) = write(KEY_CLAIM_SECRET, value.trim())

    fun clearClaimSecret() {
        prefs.edit().remove(KEY_CLAIM_SECRET).apply()
    }

    /**
     * Reads a sealed value.
     *
     * A value that cannot be opened (the key was invalidated, e.g. after
     * the screen lock of the phone was changed) is dropped: the app then
     * asks the user to pair again instead of sending a broken token.
     */
    private fun read(key: String): String {
        val stored = prefs.getString(key, null) ?: return ""
        if (stored.isBlank()) return ""
        return runCatching { decrypt(stored) }.getOrElse {
            prefs.edit().remove(key).apply()
            ""
        }
    }

    /** Seals a value and writes it down. */
    private fun write(key: String, value: String) {
        if (value.isBlank()) {
            prefs.edit().remove(key).apply()
            return
        }
        val sealed = runCatching { encrypt(value) }.getOrElse {
            // Without a usable key the secret is not stored at all. The
            // app is then "not paired" and says so.
            prefs.edit().remove(key).apply()
            return
        }
        prefs.edit().putString(key, sealed).apply()
    }

    /**
     * The AES key of the app. It is created once and then lives in the
     * Keystore of the phone, where the app cannot read it.
     */
    private fun secretKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        if (keyStore.containsAlias(KEY_ALIAS)) {
            val entry = keyStore.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry
            if (entry != null) return entry.secretKey
        }
        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            ANDROID_KEYSTORE
        )
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(KEY_SIZE_BITS)
                .setRandomizedEncryptionRequired(true)
                .build()
        )
        return generator.generateKey()
    }

    /** Seals a value: the random IV is written in front of the cipher text. */
    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val iv = cipher.iv
        val sealed = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        val raw = ByteArray(iv.size + sealed.size)
        System.arraycopy(iv, 0, raw, 0, iv.size)
        System.arraycopy(sealed, 0, raw, iv.size, sealed.size)
        return Base64.encodeToString(raw, Base64.NO_WRAP)
    }

    /** Opens a sealed value again. */
    private fun decrypt(stored: String): String {
        val raw = Base64.decode(stored, Base64.NO_WRAP)
        require(raw.size > GCM_IV_LENGTH) { "sealed value is too short" }
        val iv = raw.copyOfRange(0, GCM_IV_LENGTH)
        val sealed = raw.copyOfRange(GCM_IV_LENGTH, raw.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(GCM_TAG_BITS, iv))
        return String(cipher.doFinal(sealed), Charsets.UTF_8)
    }

    companion object {
        private const val PREFS_NAME = "sms_relay_secrets"
        private const val KEY_TOKEN = "device_token"
        private const val KEY_CLAIM_SECRET = "claim_secret"

        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "sms_relay_device_key"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val KEY_SIZE_BITS = 256
        private const val GCM_IV_LENGTH = 12
        private const val GCM_TAG_BITS = 128
    }
}
