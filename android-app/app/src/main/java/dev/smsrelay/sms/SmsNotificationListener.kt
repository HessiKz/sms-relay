package dev.smsrelay.sms

import android.app.Notification
import android.os.Bundle
import android.provider.Telephony
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import dev.smsrelay.SettingsStore
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.util.Dedupe
import dev.smsrelay.util.RoleState
import dev.smsrelay.work.Schedule
import java.util.UUID
import java.util.concurrent.Executors

/**
 * Second capture path.
 *
 * When the app is NOT the default SMS app it receives no SMS broadcast at
 * all. The messaging app however posts a notification for every incoming
 * message, and a notification listener may read it once the user grants
 * "notification access" in the system settings.
 *
 * The path is only used when the default-SMS-app role is not held, and
 * every capture is deduplicated against the local database, so the two
 * paths can never produce the same message twice.
 */
class SmsNotificationListener : NotificationListenerService() {

    // NotificationListenerService is a Service, not a BroadcastReceiver:
    // it has no goAsync(). The database work therefore runs on a small
    // background executor.
    private val executor = Executors.newSingleThreadExecutor()

    override fun onNotificationPosted(sbn: StatusBarNotification?) {
        val notification = sbn?.notification ?: return
        val packageName = sbn.packageName ?: return
        // Only the messaging app the user chose has the message. Any other
        // app may post a "message" notification too, and those are not SMS.
        if (!isDefaultMessagingApp(packageName)) return
        // When the app is the default SMS app the message already arrived
        // through the broadcast path; capturing it again would duplicate it.
        if (RoleState.canReceiveSmsBroadcast(this)) return
        if (!SettingsStore(this).forwardEnabled) return

        val extras = notification.extras ?: return
        val sender = senderOf(extras) ?: return
        val body = bodyOf(extras) ?: return
        if (body.isBlank()) return

        val postedAt = sbn.postTime
        val receivedAt = postedAt / 1000L

        executor.execute {
            try {
                val dao = AppDatabase.get(this).messageDao()
                if (Dedupe.isDuplicate(
                        dao, sender, body, receivedAt,
                        MessageRecord.SOURCE_NOTIFICATION
                    )
                ) {
                    return@execute
                }
                // Which card received the message? Here the row of the
                // messaging app in the SMS provider is the source: it knows
                // the card of every message it shows. When the provider
                // cannot be read the message is still stored, only the
                // "via SIM 1" note is missing.
                val subscriptionId = SmsSubscriptionLookup.resolve(
                    context = this,
                    address = sender,
                    timestampMillis = postedAt,
                    windowMillis = SmsSubscriptionLookup.NOTIFICATION_WINDOW_MILLIS
                )
                dao.insert(
                    MessageRecord(
                        id = UUID.randomUUID().toString(),
                        sender = sender,
                        senderKey = dev.smsrelay.util.SenderNormalizer.normalize(sender),
                        body = body,
                        parts = 1,
                        partIndex = 0,
                        receivedAt = receivedAt,
                        groupThreadId = null,
                        subId = subscriptionId ?: UNKNOWN_SUBSCRIPTION_ID,
                        source = MessageRecord.SOURCE_NOTIFICATION
                    )
                )
                // The broadcast path schedules its own upload; this path
                // must do the same, otherwise the message waits for the
                // periodic run.
                Schedule.enqueueUpload(this)
            } catch (e: Exception) {
                // Never crash on a malformed notification.
            }
        }
    }

    override fun onDestroy() {
        executor.shutdown()
        super.onDestroy()
    }

    private fun isDefaultMessagingApp(packageName: String): Boolean =
        Telephony.Sms.getDefaultSmsPackage(this) == packageName

    private fun senderOf(extras: Bundle): String? {
        val title = extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()
        if (!title.isNullOrBlank()) return title
        val conversation = extras.getCharSequence(Notification.EXTRA_CONVERSATION_TITLE)?.toString()
        return conversation?.takeIf { it.isNotBlank() }
    }

    private fun bodyOf(extras: Bundle): String? {
        @Suppress("DEPRECATION")
        val messages = extras.getParcelableArray(Notification.EXTRA_MESSAGES)
        if (messages != null) {
            for (index in messages.indices.reversed()) {
                val bundle = messages[index] as? Bundle ?: continue
                val text = bundle.getCharSequence(Notification.EXTRA_TEXT)?.toString()
                if (!text.isNullOrBlank()) return text
            }
        }
        val text = extras.getCharSequence(Notification.EXTRA_TEXT)?.toString()
        if (!text.isNullOrBlank()) return text
        val big = extras.getCharSequence(Notification.EXTRA_BIG_TEXT)?.toString()
        return big?.takeIf { it.isNotBlank() }
    }

    companion object {
        /** Two captures of the same text within this window are the same message. */
        const val DEDUPE_WINDOW_SECONDS = Dedupe.WINDOW_SECONDS

        /** The card of a message is not known: 0 is the "unknown" value. */
        private const val UNKNOWN_SUBSCRIPTION_ID = 0
    }
}
