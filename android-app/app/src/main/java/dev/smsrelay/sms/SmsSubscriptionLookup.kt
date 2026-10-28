package dev.smsrelay.sms

import android.content.Context
import android.content.Intent
import android.database.Cursor
import android.provider.Telephony
import kotlin.math.abs

/**
 * Finds out which SIM card received a message.
 *
 * Android hands the message to the app without saying which card it came
 * in on: `SmsMessage` has no field for it. Two sources answer the
 * question:
 *
 *  1. THE SMS PROVIDER: every stored message carries the id of the
 *     subscription it arrived on, in the `sub_id` column. The row is
 *     found by the address of the sender and the time of the message.
 *     The messaging app of the phone writes that row, so this is the
 *     source that works when the app reads the messages from the
 *     notifications;
 *  2. THE BROADCAST: the platform puts the id of the receiving
 *     subscription into the intent of the message
 *     (`"subscription"`, documented in
 *     `Telephony.Sms.Intents.SMS_DELIVER_ACTION`).
 *
 * The answer is a hint and nothing more: when it cannot be found the
 * message is still stored and still forwarded to Telegram. Only the
 * "via SIM 1" note in the history is then missing.
 */
object SmsSubscriptionLookup {

    /**
     * How far from the time of the message the row is still searched.
     *
     * The message time and the time of the stored row are the same value
     * on a phone that stores the message itself; a small window covers
     * the phones that round it a little differently.
     */
    const val WINDOW_MILLIS = 5_000L

    /**
     * The window of the notification path: there the time is the time the
     * notification was posted, not the time the message arrived.
     */
    const val NOTIFICATION_WINDOW_MILLIS = 60_000L

    /** The column of the SMS table that names the SIM card. */
    private const val COLUMN_SUB_ID = "sub_id"

    /** The extra of the broadcast that names the receiving subscription. */
    private const val EXTRA_SUBSCRIPTION = "subscription"

    /**
     * The subscription that received the message, or null when it cannot
     * be found.
     *
     * The provider is asked first: its row is the record the phone itself
     * wrote. The broadcast is the second source, used when the provider
     * has no row yet. The last attempt looks at the time alone, for the
     * path that reads the messages from the notifications: there the
     * "sender" is the name of the contact and not the number, so the row
     * cannot be found by its address.
     */
    fun resolve(
        context: Context,
        address: String,
        timestampMillis: Long,
        windowMillis: Long = WINDOW_MILLIS,
        intent: Intent? = null
    ): Int? {
        val fromProvider = lookupProvider(context, address, timestampMillis, windowMillis)
        if (fromProvider != null) return fromProvider
        val fromIntent = intent?.let { subscriptionFromIntent(context, it) }
        if (fromIntent != null) return fromIntent
        return lookupProvider(context, null, timestampMillis, windowMillis)
    }

    /**
     * The subscription id of the stored row that belongs to the message.
     *
     * The row is searched by the address of the sender and by the time of
     * the message; the row closest to that time wins. When [address] is
     * null only the time is used. Only a subscription that is active right
     * now is accepted, so a row that belongs to a card which was taken out
     * never names a card that cannot receive.
     */
    private fun lookupProvider(
        context: Context,
        address: String?,
        timestampMillis: Long,
        windowMillis: Long
    ): Int? {
        if (address != null && address.isBlank()) return null
        val projection = arrayOf(Telephony.Sms.ADDRESS, Telephony.Sms.DATE, COLUMN_SUB_ID)
        val selection = if (address == null) {
            "${Telephony.Sms.DATE} BETWEEN ? AND ?"
        } else {
            "${Telephony.Sms.ADDRESS} = ? AND ${Telephony.Sms.DATE} BETWEEN ? AND ?"
        }
        val arguments = if (address == null) {
            arrayOf(
                (timestampMillis - windowMillis).toString(),
                (timestampMillis + windowMillis).toString()
            )
        } else {
            arrayOf(
                address,
                (timestampMillis - windowMillis).toString(),
                (timestampMillis + windowMillis).toString()
            )
        }
        var cursor: Cursor? = null
        try {
            cursor = context.contentResolver.query(
                Telephony.Sms.Inbox.CONTENT_URI,
                projection,
                selection,
                arguments,
                "${Telephony.Sms.DATE} DESC"
            ) ?: return null
            val subIndex = cursor.getColumnIndex(COLUMN_SUB_ID)
            val dateIndex = cursor.getColumnIndex(Telephony.Sms.DATE)
            if (subIndex < 0 || dateIndex < 0) return null
            var bestDistance = Long.MAX_VALUE
            var bestSubId: Int? = null
            while (cursor.moveToNext()) {
                val distance = abs(cursor.getLong(dateIndex) - timestampMillis)
                if (distance >= bestDistance) continue
                bestDistance = distance
                bestSubId = cursor.getInt(subIndex)
            }
            val subId = bestSubId ?: return null
            if (subId < 0) return null
            return if (SimCards.find(context, subId) != null) subId else null
        } catch (e: SecurityException) {
            // No READ_SMS and not the default SMS app: the provider is
            // closed to this app. The message is still forwarded.
            return null
        } catch (e: Exception) {
            return null
        } finally {
            runCatching { cursor?.close() }
        }
    }

    /**
     * The subscription the broadcast names, or null.
     *
     * The platform documents the extra as a long value; some phones put
     * an int there instead, so both are read.
     */
    private fun subscriptionFromIntent(context: Context, intent: Intent): Int? {
        val fromLong = runCatching {
            intent.getLongExtra(EXTRA_SUBSCRIPTION, -1L)
        }.getOrDefault(-1L)
        if (fromLong >= 0L) return validated(context, fromLong.toInt())
        val fromInt = runCatching {
            intent.getIntExtra(EXTRA_SUBSCRIPTION, -1)
        }.getOrDefault(-1)
        if (fromInt >= 0) return validated(context, fromInt)
        return null
    }

    /** The subscription id, but only when the card is active right now. */
    private fun validated(context: Context, subscriptionId: Int): Int? =
        if (SimCards.find(context, subscriptionId) != null) subscriptionId else null
}
