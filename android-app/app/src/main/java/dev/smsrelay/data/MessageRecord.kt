package dev.smsrelay.data

import androidx.room.ColumnInfo
import androidx.room.Entity
import androidx.room.PrimaryKey

@Entity(tableName = "messages")
data class MessageRecord(
    @PrimaryKey val id: String,
    val sender: String,
    /** Normalised sender (see SenderNormalizer): the identity used for
     *  duplicate detection, independent of how the address is spelled. */
    @ColumnInfo(defaultValue = "''")
    val senderKey: String = "",
    val body: String,
    val parts: Int,
    val partIndex: Int,
    val receivedAt: Long,
    val uploadState: String = STATE_QUEUED,
    val uploadAttempts: Int = 0,
    val lastAttemptAt: Long = 0L,
    val lastError: String? = null,
    val backendState: String? = null,
    val fallbackState: String = FALLBACK_NONE,
    val groupThreadId: String? = null,
    /** The SIM card the message came in on, as the id of the
     *  subscription. 0 means: the card could not be found. */
    @ColumnInfo(defaultValue = "0")
    val subId: Int = 0,
    /** How the message reached the app: the SMS broadcast or the
     *  notification of the default messaging app. */
    @ColumnInfo(defaultValue = "'sms'")
    val source: String = SOURCE_SMS,
    val createdAt: Long = System.currentTimeMillis()
) {
    companion object {
        const val STATE_QUEUED = "queued"
        const val STATE_SENT = "sent"
        const val STATE_DUPLICATE = "duplicate"
        const val STATE_FAILED = "failed"
        const val STATE_DELIVERED = "delivered"

        const val FALLBACK_NONE = "none"
        const val FALLBACK_SENT = "sent"
        const val FALLBACK_FAILED = "failed"

        const val SOURCE_SMS = "sms"
        const val SOURCE_NOTIFICATION = "notification"

        const val BACKEND_STATE_DELIVERED = "delivered"
        const val BACKEND_STATE_FAILED = "failed"
    }
}
