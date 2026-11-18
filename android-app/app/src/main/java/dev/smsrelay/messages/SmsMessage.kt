package dev.smsrelay.messages

/**
 * The `TYPE` of a row of `content://sms`, as a small enum.
 *
 * The provider uses the same six numbers on every version of Android;
 * everything unknown is [UNKNOWN] instead of a guess, so a new type can
 * never be drawn as an incoming bubble by accident.
 */
enum class SmsType {
    INBOX,
    SENT,
    DRAFT,
    OUTBOX,
    FAILED,
    QUEUED,
    UNKNOWN;

    /** True when the message was written by this phone. */
    val isOutgoing: Boolean
        get() = this == SENT || this == OUTBOX || this == FAILED || this == QUEUED

    companion object {
        /** Maps the provider number; 1 inbox, 2 sent, 3 draft, 4 outbox, 5 failed, 6 queued. */
        fun fromProvider(type: Int): SmsType = when (type) {
            1 -> INBOX
            2 -> SENT
            3 -> DRAFT
            4 -> OUTBOX
            5 -> FAILED
            6 -> QUEUED
            else -> UNKNOWN
        }
    }
}

/**
 * One message of a thread, read from `content://sms`.
 *
 * [type] is the provider type mapped to [SmsType]; [status] is the raw
 * `STATUS` column (0 none, 32 pending, 64 failed, 128 delivered, -1
 * received). It is kept as the provider reports it, never interpreted as
 * a delivery guarantee: the relay's own queue says whether a message was
 * actually delivered.
 */
data class SmsMessage(
    val id: Long,
    val threadId: Long,
    val address: String,
    val body: String,
    val dateMillis: Long,
    val type: SmsType,
    val read: Boolean,
    val seen: Boolean,
    val status: Int,
    /** The card the message belongs to, or -1 when the provider has none. */
    val subId: Int
) {
    /** True when the outgoing message is still on its way. */
    val isPending: Boolean
        get() = type == SmsType.OUTBOX || type == SmsType.QUEUED || type == SmsType.DRAFT
}
