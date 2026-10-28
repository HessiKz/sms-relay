package dev.smsrelay.sms

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord

/**
 * Receives the radio's answer for a fallback SMS.
 *
 * The framework delivers the outcome as the broadcast RESULT CODE, not as
 * an extra, so `getResultCode()` is the only reliable source. The room
 * write happens off the main thread (Room forbids main-thread queries) and
 * uses targeted updates so a concurrently updated upload state is never
 * clobbered.
 */
class SendStatusReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val messageId = intent.getStringExtra(EXTRA_MESSAGE_ID) ?: return
        val logId = intent.getLongExtra(EXTRA_LOG_ID, -1L)
        val partIndex = intent.getIntExtra(EXTRA_PART_INDEX, -1)
        val code = resultCode
        val ok = code == Activity.RESULT_OK
        val error = if (ok) null else "radio_$code"

        val pending = goAsync()
        Thread {
            try {
                val database = AppDatabase.get(context)
                if (logId < 0 || partIndex < 0) {
                    // An intent from an older build: no per-part state.
                    database.fallbackLogDao().markResult(messageId, ok, error)
                    database.messageDao().markFallbackState(
                        messageId,
                        if (ok) MessageRecord.FALLBACK_SENT else MessageRecord.FALLBACK_FAILED
                    )
                    return@Thread
                }
                when (FallbackPartTracker.record(
                    context, messageId, logId, partIndex, ok
                )) {
                    FallbackPartTracker.Outcome.AllOk -> {
                        FallbackPartTracker.clear(context, messageId)
                        database.fallbackLogDao().markResultById(logId, true, null)
                        database.messageDao().markFallbackState(
                            messageId, MessageRecord.FALLBACK_SENT
                        )
                    }

                    FallbackPartTracker.Outcome.Failed -> {
                        FallbackPartTracker.clear(context, messageId)
                        database.fallbackLogDao().markResultById(
                            logId, false, error ?: "part_failed"
                        )
                        database.messageDao().markFallbackState(
                            messageId, MessageRecord.FALLBACK_FAILED
                        )
                    }

                    // Some parts are still unanswered: the message must
                    // not be confirmed yet.
                    FallbackPartTracker.Outcome.Pending -> Unit

                    // The trace was lost or belongs to another send: no
                    // state can be derived from this broadcast.
                    FallbackPartTracker.Outcome.Unknown -> Unit
                }
            } catch (e: Exception) {
                // A failed status update must never crash the app.
            } finally {
                pending.finish()
            }
        }.start()
    }

    companion object {
        const val EXTRA_MESSAGE_ID = "dev.smsrelay.extra.MESSAGE_ID"
        const val EXTRA_LOG_ID = "dev.smsrelay.extra.LOG_ID"
        const val EXTRA_PART_INDEX = "dev.smsrelay.extra.PART_INDEX"
    }
}
