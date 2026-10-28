package dev.smsrelay.util

import dev.smsrelay.data.MessageDao

/**
 * Guard against storing the same SMS twice.
 *
 * The phone can deliver one message through two paths: the SMS broadcast
 * (only to the default SMS app) and the notification of the messaging app
 * (only when the notification access is granted). Both paths write into the
 * local queue, so without this guard the same text would be uploaded twice
 * and the admin would see the message twice in Telegram.
 *
 * Two captures of the same text inside the window are one message when
 * they came through DIFFERENT paths. Two genuine repeats of the same text
 * from the same path are separate messages and are both kept.
 */
object Dedupe {

    /** Two captures of the same text within this window may be one message. */
    const val WINDOW_SECONDS = 120L

    /** True when this sender/body pair already arrived through another path. */
    fun isDuplicate(
        dao: MessageDao,
        sender: String,
        body: String,
        receivedAt: Long,
        source: String
    ): Boolean {
        val key = SenderNormalizer.normalize(sender)
        val since = receivedAt - WINDOW_SECONDS
        if (dao.countRecentByKeyFromOtherSource(key, body, since, source) > 0) return true
        return dao.countRecentFromOtherSource(sender, body, since, source) > 0
    }
}
