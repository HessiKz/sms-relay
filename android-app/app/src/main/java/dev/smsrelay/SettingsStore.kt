package dev.smsrelay

import android.content.Context
import android.content.SharedPreferences

/**
 * Small SharedPreferences-backed settings store. Deliberately not a
 * DataStore dependency: the app is tiny and the write volume is nil.
 */
class SettingsStore(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences("sms_relay", Context.MODE_PRIVATE)

    var baseUri: String
        get() = prefs.getString(KEY_BASE_URI, DEFAULT_BASE_URI) ?: DEFAULT_BASE_URI
        set(value) = prefs.edit().putString(KEY_BASE_URI, value.trim()).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, DEFAULT_TOKEN)
            ?.takeIf { it.isNotBlank() }
            ?: DEFAULT_TOKEN
        set(value) = prefs.edit().putString(KEY_TOKEN, value.trim()).apply()

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
     * the heartbeat of the server. It is not a credential: the device
     * token is still the only thing that authorises a request.
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

        const val DEFAULT_BASE_URI = "https://ai-barrow.ir/relay"
        const val DEFAULT_FALLBACK_GAP_SECONDS = 30
        const val DEFAULT_FALLBACK_MAX_PER_HOUR = 10

        /**
         * The number that is texted when the delivery to Telegram
         * fails. Intentionally empty: enter it in the settings, or set it
         * in a private local build.
         */
        const val DEFAULT_PHONE_B = ""

        /**
         * Device token of this phone. Intentionally empty: enter it in the
         * settings (or override this constant in a private local build that
         * is never published).
         */
        const val DEFAULT_TOKEN = ""
    }
}
