package dev.smsrelay

import android.content.Context
import android.content.SharedPreferences
import android.os.Handler
import android.os.Looper

/**
 * Small SharedPreferences-backed settings store. Deliberately not a
 * DataStore dependency: the app is tiny and the write volume is nil.
 *
 * The two secrets of the app (the device token and the claim secret of a
 * running pairing) are NOT here: they live in [TokenStore], which keeps
 * them sealed under a Keystore key. This class is the only place the rest
 * of the app reads the token from.
 */
class SettingsStore(context: Context) {

    private val appContext = context.applicationContext

    private val prefs: SharedPreferences =
        appContext.getSharedPreferences("sms_relay", Context.MODE_PRIVATE)

    /** The secrets of the app, sealed under a Keystore key. */
    private val secrets = TokenStore(appContext)

    init {
        // The token used to live in the plain preferences. It is not
        // copied over into the sealed store: the server issues a new
        // token during the pairing, and the old one is worthless.
        if (prefs.contains(KEY_TOKEN)) {
            prefs.edit().remove(KEY_TOKEN).apply()
        }
    }

    /**
     * The URL of the relay backend.
     *
     * The device token belongs to one server only. A different URL means
     * a different server, so the token is dropped and the device has to
     * pair again.
     */
    var baseUri: String
        get() = prefs.getString(KEY_BASE_URI, DEFAULT_BASE_URI) ?: DEFAULT_BASE_URI
        set(value) {
            val trimmed = value.trim()
            val changed = trimmed.isNotEmpty() && trimmed != baseUri
            prefs.edit().putString(KEY_BASE_URI, trimmed).apply()
            if (changed) invalidateForNewServer()
        }

    /**
     * The device token of this phone. It is a bearer credential and lives
     * in the encrypted store, never in the plain preferences.
     */
    var token: String
        get() = secrets.token
        set(value) {
            secrets.token = value.trim()
        }

    /** True when the phone holds a token the server may accept. */
    fun hasToken(): Boolean = token.isNotBlank()

    /** Drops the token: the device has to pair again. */
    fun clearToken() {
        secrets.clearToken()
    }

    /**
     * The secret of a running pairing. It is short lived and never part
     * of the QR code: only this phone can collect the token.
     */
    var claimSecret: String
        get() = secrets.claimSecret
        set(value) {
            secrets.claimSecret = value.trim()
        }

    /**
     * The name of this phone, as the admin sees it. Not a secret.
     */
    var deviceName: String
        get() = prefs.getString(KEY_DEVICE_NAME, "") ?: ""
        set(value) = prefs.edit().putString(KEY_DEVICE_NAME, value.trim()).apply()

    /** The name the device was paired with. */
    var pairedDeviceName: String
        get() = prefs.getString(KEY_PAIRED_NAME, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PAIRED_NAME, value.trim()).apply()

    /**
     * The server URL the current token was issued for. A different URL
     * invalidates the token.
     */
    var pairedBaseUri: String
        get() = prefs.getString(KEY_PAIRED_BASE_URI, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PAIRED_BASE_URI, value.trim()).apply()

    /**
     * Where the pairing stands: unpaired, pairing, paired, removed or
     * server_changed. The screen shows what it means.
     */
    var pairingState: String
        get() = prefs.getString(KEY_PAIRING_STATE, STATE_UNPAIRED) ?: STATE_UNPAIRED
        set(value) = prefs.edit().putString(KEY_PAIRING_STATE, value).apply()

    /** The id of the running pairing, empty when there is none. */
    var pairingId: String
        get() = prefs.getString(KEY_PAIRING_ID, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PAIRING_ID, value.trim()).apply()

    /** The short code the admin reads out loud, empty when there is none. */
    var pairingScanCode: String
        get() = prefs.getString(KEY_PAIRING_SCAN_CODE, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PAIRING_SCAN_CODE, value.trim()).apply()

    /** The payload of the QR code, empty when there is none. */
    var pairingQrPayload: String
        get() = prefs.getString(KEY_PAIRING_QR, "") ?: ""
        set(value) = prefs.edit().putString(KEY_PAIRING_QR, value.trim()).apply()

    /** When the running pairing runs out (epoch seconds). */
    var pairingExpiresAt: Long
        get() = prefs.getLong(KEY_PAIRING_EXPIRES, 0L)
        set(value) = prefs.edit().putLong(KEY_PAIRING_EXPIRES, value).apply()

    /**
     * Forgets a running pairing: the code, the payload and the claim
     * secret. The token is not touched.
     */
    fun clearPairing() {
        prefs.edit()
            .remove(KEY_PAIRING_ID)
            .remove(KEY_PAIRING_SCAN_CODE)
            .remove(KEY_PAIRING_QR)
            .remove(KEY_PAIRING_EXPIRES)
            .apply()
        secrets.clearClaimSecret()
    }

    /**
     * The server refused the token of this device: it was removed in the
     * admin app, or the server was reset. The token is dropped, the
     * queue of the phone stays untouched and the user has to pair again.
     *
     * The device id is NOT changed, so the server can wake the very same
     * row up again.
     */
    fun handleUnauthorized() {
        // Without a token there is nothing to drop: the device was
        // already unpaired, and the screen knows the reason already.
        if (!hasToken()) return
        clearToken()
        pairingState = STATE_REMOVED
        clearPairing()
        // The screen shows the state at once, without waiting for the
        // next time the app is opened.
        val callback = onDeviceRemoved
        if (callback != null) mainHandler.post { callback() }
    }

    /** The token belongs to one server only. */
    private fun invalidateForNewServer() {
        if (!hasToken()) return
        clearToken()
        pairingState = STATE_SERVER_CHANGED
        clearPairing()
    }

    /**
     * Master switch for every outgoing SMS of this app.
     *
     * The relay is read-only by default: it receives messages, uploads them
     * and reports them to Telegram. While this flag is off the app never
     * sends an SMS over the air - not the cellular fallback, not a message
     * the user composes and not a command that arrives from Telegram.
     */
    var sendingEnabled: Boolean
        get() = prefs.getBoolean(KEY_SENDING_ENABLED, false)
        set(value) = prefs.edit().putBoolean(KEY_SENDING_ENABLED, value).apply()

    var phoneB: String
        get() = prefs.getString(KEY_PHONE_B, DEFAULT_PHONE_B) ?: DEFAULT_PHONE_B
        set(value) = prefs.edit().putString(KEY_PHONE_B, value.trim()).apply()

    var forwardEnabled: Boolean
        get() = prefs.getBoolean(KEY_FORWARD, true)
        set(value) = prefs.edit().putBoolean(KEY_FORWARD, value).apply()

    var fallbackEnabled: Boolean
        get() = prefs.getBoolean(KEY_FALLBACK, false)
        set(value) = prefs.edit().putBoolean(KEY_FALLBACK, value).apply()

    /**
     * The SIM card the outgoing messages go out with.
     *
     * It is the id of the subscription (the card the user picked in the
     * "Send an SMS" section). Null means: the user did not pick a card
     * and the first active card is used. See
     * [dev.smsrelay.sms.SmsSender.resolveSubscriptionId].
     */
    var simSubscriptionId: Int?
        get() {
            val raw = prefs.getString(KEY_SIM, null) ?: return null
            return raw.toIntOrNull()
        }
        set(value) {
            prefs.edit().putString(KEY_SIM, value?.toString()).apply()
        }

    var fallbackMinGapSeconds: Int
        get() = prefs.getInt(KEY_FALLBACK_GAP, DEFAULT_FALLBACK_GAP_SECONDS)
        set(value) = prefs.edit().putInt(KEY_FALLBACK_GAP, value).apply()

    var fallbackMaxPerHour: Int
        get() = prefs.getInt(KEY_FALLBACK_MAX_HOUR, DEFAULT_FALLBACK_MAX_PER_HOUR)
        set(value) = prefs.edit().putInt(KEY_FALLBACK_MAX_HOUR, value).apply()

    var lastUploadAt: Long
        get() = prefs.getLong(KEY_LAST_UPLOAD, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_UPLOAD, value).apply()

    var lastProbeOk: Boolean
        get() = prefs.getBoolean(KEY_LAST_PROBE_OK, false)
        set(value) = prefs.edit().putBoolean(KEY_LAST_PROBE_OK, value).apply()

    /** Short status of the last command that arrived from Telegram. */
    var lastCommandStatus: String
        get() = prefs.getString(KEY_LAST_COMMAND_STATUS, "") ?: ""
        set(value) = prefs.edit().putString(KEY_LAST_COMMAND_STATUS, value).apply()

    /** Epoch millis of the last command that arrived from Telegram. */
    var lastCommandAt: Long
        get() = prefs.getLong(KEY_LAST_COMMAND_AT, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_COMMAND_AT, value).apply()

    /**
     * The highest SMS inbox row id that was imported. The worker resumes
     * from here, so an interrupted import never starts over.
     */
    var importCursor: Long
        get() = prefs.getLong(KEY_IMPORT_CURSOR, 0L)
        set(value) = prefs.edit().putLong(KEY_IMPORT_CURSOR, value).apply()

    /** Epoch millis of the last successful upload of the contact list. */
    var lastContactsSyncAt: Long
        get() = prefs.getLong(KEY_LAST_CONTACTS_SYNC, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_CONTACTS_SYNC, value).apply()

    /** Epoch millis of the last attempt to upload the contact list. */
    var lastContactsAttemptAt: Long
        get() = prefs.getLong(KEY_LAST_CONTACTS_ATTEMPT, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST_CONTACTS_ATTEMPT, value).apply()

    /** How many contacts the server has. */
    var contactsCount: Int
        get() = prefs.getInt(KEY_CONTACTS_COUNT, 0)
        set(value) = prefs.edit().putInt(KEY_CONTACTS_COUNT, value).apply()

    /**
     * Random id of this installation, used only to recognise the phone in
     * the heartbeat of the server and to pair it again. It is not a
     * credential: the device token is still the only thing that
     * authorises a request.
     */
    val deviceId: String
        get() {
            prefs.getString(KEY_DEVICE_ID, null)
                ?.takeIf { it.isNotBlank() }
                ?.let { return it }
            val generated = java.util.UUID.randomUUID().toString()
            prefs.edit().putString(KEY_DEVICE_ID, generated).apply()
            return generated
        }

    companion object {
        /**
         * Called when the device was removed on the server. The screen
         * shows "pair again" at once instead of waiting for the next
         * time the app is opened.
         *
         * The screen sets it while it is alive and clears it again when
         * it goes away, so no screen is kept alive by it.
         */
        @Volatile
        var onDeviceRemoved: (() -> Unit)? = null

        private val mainHandler = Handler(Looper.getMainLooper())

        const val KEY_BASE_URI = "base_uri"
        const val KEY_TOKEN = "token"
        const val KEY_PHONE_B = "phone_b"
        const val KEY_FORWARD = "forward_enabled"
        const val KEY_FALLBACK = "fallback_enabled"
        const val KEY_SENDING_ENABLED = "sending_enabled"
        const val KEY_SIM = "sim_id"
        const val KEY_FALLBACK_GAP = "fallback_gap_s"
        const val KEY_FALLBACK_MAX_HOUR = "fallback_max_per_hour"
        const val KEY_LAST_UPLOAD = "last_upload_at"
        const val KEY_LAST_PROBE_OK = "last_probe_ok"
        const val KEY_LAST_COMMAND_STATUS = "last_command_status"
        const val KEY_LAST_COMMAND_AT = "last_command_at"
        const val KEY_LAST_CONTACTS_SYNC = "last_contacts_sync_at"
        const val KEY_LAST_CONTACTS_ATTEMPT = "last_contacts_attempt_at"
        const val KEY_CONTACTS_COUNT = "contacts_count"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_IMPORT_CURSOR = "import_cursor"
        const val KEY_DEVICE_NAME = "device_name"
        const val KEY_PAIRED_NAME = "paired_device_name"
        const val KEY_PAIRED_BASE_URI = "paired_base_uri"
        const val KEY_PAIRING_STATE = "pairing_state"
        const val KEY_PAIRING_ID = "pairing_id"
        const val KEY_PAIRING_SCAN_CODE = "pairing_scan_code"
        const val KEY_PAIRING_QR = "pairing_qr_payload"
        const val KEY_PAIRING_EXPIRES = "pairing_expires_at"

        const val DEFAULT_BASE_URI = "https://ai-barrow.ir/relay"
        const val DEFAULT_FALLBACK_GAP_SECONDS = 30
        const val DEFAULT_FALLBACK_MAX_PER_HOUR = 10

        /**
         * The number that is texted when the delivery to Telegram
         * fails. Intentionally empty: enter it in the settings, or set it
         * in a private local build.
         */
        const val DEFAULT_PHONE_B = ""

        /** The device was never paired. */
        const val STATE_UNPAIRED = "unpaired"

        /** A pairing is running: the app waits for the admin. */
        const val STATE_PAIRING = "pairing"

        /** The device holds a token. */
        const val STATE_PAIRED = "paired"

        /** The device was removed on the server: it has to pair again. */
        const val STATE_REMOVED = "removed"

        /** The server URL changed: the token is worthless now. */
        const val STATE_SERVER_CHANGED = "server_changed"
    }
}
