package dev.smsrelay.sms

import android.content.Context
import dev.smsrelay.SettingsStore
import dev.smsrelay.data.FallbackLogDao
import dev.smsrelay.data.FallbackLogEntity
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.util.PhoneNumbers
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Sends the original SMS to Phone B using the phone's own SIM.
 *
 * The text keeps the original sender, the original timestamp and the
 * complete body, plus the relay message id so a duplicate can be
 * recognised later. Long bodies are split into multipart SMS by
 * SmsManager itself.
 *
 * "Handed to the radio" is not "delivered": the log row is written BEFORE
 * the radio call -- so a status broadcast can always find it -- and only
 * flipped to confirmed when every part of the message answers through
 * SendStatusReceiver. The caps in FallbackPolicy count confirmed sends
 * only, so a radio failure can be retried later instead of burning the
 * one-shot allowance of a message.
 */
class FallbackSender(
    private val context: Context,
    private val settings: SettingsStore,
    private val fallbackLogDao: FallbackLogDao
) {

    /**
     * Hands the message to the radio.
     *
     * The send itself goes through [SmsSender], like every other message
     * of this app: the fallback therefore uses the same SIM card as the
     * composed message and the commands from Telegram, and it splits a
     * long body in the same way.
     */
    fun send(record: MessageRecord): Boolean {
        val raw = settings.phoneB.trim()
        if (raw.isEmpty()) return false
        // The number is written by a human: it is normalised before the
        // radio sees it, like every other number of the app.
        val destination = PhoneNumbers.normalize(raw) ?: raw

        val text = buildText(record)
        // The row exists before the radio is asked: the status broadcast
        // can arrive before a later insert would have run, and it must
        // always find the row it belongs to.
        val logId = fallbackLogDao.insert(
            FallbackLogEntity(
                messageId = record.id,
                sentAt = System.currentTimeMillis(),
                ok = false,
                error = null
            )
        )
        val outcome = SmsSender.send(
            context = context,
            settings = settings,
            destination = destination,
            body = text,
            sentIntents = { partCount ->
                FallbackPartTracker.begin(context, record.id, logId, partCount)
                FallbackPartTracker.pendingIntents(context, record.id, logId, partCount)
            }
        )
        return when (outcome) {
            SmsSender.Outcome.Sent -> {
                // Accepted by the radio; the state is confirmed by
                // SendStatusReceiver when every part answers.
                true
            }

            is SmsSender.Outcome.Failed -> {
                FallbackPartTracker.clear(context, record.id)
                fallbackLogDao.markResultById(logId, false, outcome.reason)
                false
            }
        }
    }

    companion object {
        fun buildText(record: MessageRecord): String {
            val shortId = record.id.take(8)
            val stamp = formatTimestamp(record.receivedAt)
            return "[Relay:$shortId-1/1] ${record.sender} @ $stamp\n${record.body}"
        }

        private fun formatTimestamp(epochSeconds: Long): String {
            val format = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
            format.timeZone = TimeZone.getTimeZone("UTC")
            return format.format(Date(epochSeconds * 1000L))
        }
    }
}
