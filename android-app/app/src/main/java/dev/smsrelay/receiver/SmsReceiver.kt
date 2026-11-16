package dev.smsrelay.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony
import dev.smsrelay.SettingsStore
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.sms.SmsProviderWriter
import dev.smsrelay.sms.SmsSubscriptionLookup
import dev.smsrelay.util.Dedupe
import dev.smsrelay.util.RoleState
import dev.smsrelay.work.Schedule
import java.util.UUID

/**
 * Receives incoming SMS. The message is written to the local Room queue
 * FIRST, then the upload is scheduled. Nothing is ever dropped because
 * the network was unavailable at that moment.
 *
 * EVERY card of the phone is accepted: the platform hands the app the
 * messages of all SIM cards and nothing here filters them, so a dual SIM
 * phone forwards the messages of both cards. The card a message came in on
 * is only noted for the user, it never decides whether the message is
 * forwarded.
 */
class SmsReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_DELIVER_ACTION &&
            intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION
        ) {
            return
        }
        // The platform only delivers SMS_DELIVER to the default SMS app.
        // When that is this app, the message must be stored in the provider
        // inbox as well, otherwise it would vanish from the user's inbox.
        val isDefaultSmsApp = intent.action == Telephony.Sms.Intents.SMS_DELIVER_ACTION

        // SOME OEM BUILDS SEND BOTH BROADCASTS.
        //
        // SMS_RECEIVED is meant for apps that are NOT the default SMS app:
        // the canonical copy of a message is the SMS_DELIVER one. Some
        // vendor builds (MIUI among them) hand SMS_RECEIVED to the default
        // app as well, so both broadcasts reach this receiver for one SMS.
        // Both were stored as source='sms' and the local dedupe only drops
        // copies that came through a DIFFERENT path, on purpose: two
        // genuine repeats of the same text must both be kept. The result
        // was one message uploaded twice and shown twice in Telegram.
        // The whole broadcast is dropped here instead: the SMS_DELIVER
        // copy already carries the message.
        if (!isDefaultSmsApp && RoleState.isDefaultSmsApp(context)) return

        val settings = SettingsStore(context)
        if (!settings.forwardEnabled) return

        val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent) ?: return
        if (messages.isEmpty()) return

        // ONE BROADCAST = ONE MESSAGE.
        //
        // The parts of a concatenated SMS arrive as several PDUs, but the
        // platform does not promise that they carry the same sender
        // spelling or the same timestamp: a message split across two
        // SMSCs comes with the parts formatted differently ("0919 708
        // 1404" vs "+989120000001") and with a second apart. Grouping by
        // sender+timestamp therefore split one long message into a dozen
        // separate ones. The platform hands us the complete set in one
        // intent, so the set IS the message.
        val ordered = messages.sortedBy { it.timestampMillis }
        val sender = ordered.first().displayOriginatingAddress.orEmpty()
        val timestamp = ordered.first().timestampMillis
        val body = ordered.joinToString("") { it.messageBody.orEmpty() }
        val records = listOf(
            MessageRecord(
                id = UUID.randomUUID().toString(),
                sender = sender,
                senderKey = dev.smsrelay.util.SenderNormalizer.normalize(sender),
                body = body,
                parts = ordered.size,
                partIndex = 0,
                receivedAt = timestamp / 1000L,
                groupThreadId = null,
                source = MessageRecord.SOURCE_SMS
            )
        )

        // The database write must not happen on the main thread.
        val pending = goAsync()
        Thread {
            try {
                val dao = AppDatabase.get(context).messageDao()
                val stored = mutableListOf<MessageRecord>()
                for (record in records) {
                    // The same message can also arrive through the
                    // notification listener. Storing it twice would upload
                    // it twice and the admin would see it twice in Telegram.
                    if (Dedupe.isDuplicate(
                            dao, record.sender, record.body, record.receivedAt,
                            MessageRecord.SOURCE_SMS
                        )
                    ) {
                        continue
                    }
                    // Which card received the message? Android does not say
                    // it, so the SMS provider is asked for the stored row
                    // and the broadcast is asked for the subscription. The
                    // answer is only a hint: when it cannot be found the
                    // message is stored anyway, it is just the "via SIM 1"
                    // note that is missing.
                    val subscriptionId = SmsSubscriptionLookup.resolve(
                        context = context,
                        address = record.sender,
                        timestampMillis = timestamp,
                        intent = intent
                    )
                    val withCard = record.copy(subId = subscriptionId ?: UNKNOWN_SUBSCRIPTION_ID)
                    dao.insert(withCard)
                    stored.add(withCard)
                }
                if (isDefaultSmsApp) {
                    for (record in stored) {
                        SmsProviderWriter.writeToInbox(
                            context,
                            record.sender,
                            record.body,
                            record.receivedAt * 1000L,
                            subscriptionId = record.subId.takeIf { it > 0 }
                        )
                    }
                }
                // Only now: the upload worker must not start before the
                // rows are visible in the database.
                Schedule.enqueueUpload(context)
            } finally {
                pending.finish()
            }
        }.start()
    }

    companion object {
        /** The card of a message is not known: 0 is the "unknown" value. */
        private const val UNKNOWN_SUBSCRIPTION_ID = 0
    }
}
