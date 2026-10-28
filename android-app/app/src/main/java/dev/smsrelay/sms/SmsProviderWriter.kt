package dev.smsrelay.sms

import android.content.ContentValues
import android.content.Context
import android.provider.Telephony

/**
 * Writes a received message into the SMS provider inbox.
 *
 * This is only needed when the app is the DEFAULT SMS app: the platform
 * delivers incoming messages to the default app alone, so if the app does
 * not store them itself they would disappear from the inbox entirely.
 * The provider derives the conversation thread from the address, so no
 * thread id is supplied here.
 */
object SmsProviderWriter {

    /**
     * The value of the `sub_id` column when the card that received the
     * message is not known. The provider uses the same value for a row
     * without a card.
     */
    private const val UNKNOWN_SUBSCRIPTION_ID = -1

    /**
     * Writes a received message into the SMS provider inbox.
     *
     * [subscriptionId] is the card that received the message. It is
     * written into the row as well, so the phone's own inbox knows which
     * card every message came in on.
     */
    fun writeToInbox(
        context: Context,
        sender: String,
        body: String,
        timestampMillis: Long,
        subscriptionId: Int? = null
    ): Boolean {
        return try {
            val values = ContentValues().apply {
                put(Telephony.Sms.ADDRESS, sender)
                put(Telephony.Sms.BODY, body)
                put(Telephony.Sms.DATE, timestampMillis)
                put(Telephony.Sms.DATE_SENT, timestampMillis)
                put(Telephony.Sms.READ, 0)
                put(Telephony.Sms.SEEN, 0)
                put(Telephony.Sms.TYPE, Telephony.Sms.MESSAGE_TYPE_INBOX)
                put(
                    Telephony.Sms.SUBSCRIPTION_ID,
                    subscriptionId ?: UNKNOWN_SUBSCRIPTION_ID
                )
            }
            context.contentResolver.insert(Telephony.Sms.Inbox.CONTENT_URI, values) != null
        } catch (e: SecurityException) {
            // WRITE_SMS was not granted: the relay still works, the inbox
            // copy is simply missing.
            false
        } catch (e: Exception) {
            false
        }
    }
}
