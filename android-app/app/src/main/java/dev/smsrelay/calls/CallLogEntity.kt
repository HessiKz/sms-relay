package dev.smsrelay.calls

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

/**
 * One call of the phone, on its way to the server.
 *
 * The row is written before it is uploaded, so a lost connection or a
 * restart never loses a call. [id] is the id the SERVER computes
 * (`call-<hash of device and provider row>`); until the row was sent it
 * holds a local placeholder, which is fine because the server ignores an
 * id it is given and derives its own from `provider_id` and the content.
 *
 * Rows are never deleted by a migration: this table is a queue, not a
 * cache, so a destructive migration would throw unsent calls away.
 */
@Entity(
    tableName = "call_log",
    indices = [
        Index(value = ["uploadState", "startedAt"]),
        Index(value = ["contentHash"]),
        // The rolling recent window asks for one row per call of the last
        // seven days (up to 200 lookups per run) and the table keeps every
        // call this phone ever had, so the identity lookup needs its own
        // index.
        Index(value = ["providerId"])
    ]
)
data class CallLogEntity(
    @PrimaryKey val id: String,
    /** The `_ID` of the row in `content://call_log/calls`. */
    val providerId: Long,
    /** Empty string for a withheld or private number. */
    val number: String,
    /** The name the phone's own contact store knew at capture time. */
    val cachedName: String? = null,
    /** incoming|outgoing|missed|rejected|blocked|voicemail|unknown */
    val kind: String,
    /** The raw `CallLog.Calls.TYPE` value, kept for the server. */
    val rawType: Int,
    /** Epoch seconds, like the SMS import. */
    val startedAt: Long,
    val durationS: Int = 0,
    /** The subscription (SIM) the call used; 0 means unknown. */
    val subId: Int = 0,
    /** The `PHONE_ACCOUNT_ID` of the call, when the phone reports one. */
    val phoneAccount: String? = null,
    val isRead: Boolean = false,
    /** sha256 over the fields that make a row unique. */
    val contentHash: String,
    val uploadState: String = STATE_QUEUED,
    val attempts: Int = 0,
    val lastError: String? = null
) {
    companion object {
        const val STATE_QUEUED = "queued"
        const val STATE_SENT = "sent"
        const val STATE_FAILED = "failed"

        const val KIND_INCOMING = "incoming"
        const val KIND_OUTGOING = "outgoing"
        const val KIND_MISSED = "missed"
        const val KIND_REJECTED = "rejected"
        const val KIND_BLOCKED = "blocked"
        const val KIND_VOICEMAIL = "voicemail"
        const val KIND_UNKNOWN = "unknown"
    }
}
