package dev.smsrelay.sms

import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.smsrelay.data.CommandDatabase
import dev.smsrelay.data.HandledCommandEntity
import dev.smsrelay.work.Schedule

/**
 * Receives the radio's and the carrier's answers for a command SMS.
 *
 * The framework delivers each answer as the broadcast RESULT CODE, so
 * `getResultCode()` is the only reliable source. Every part reports
 * separately:
 *
 *  - a sent answer that is not OK means the message never left the
 *    phone: the command becomes "failed" (`radio_<code>`);
 *  - a delivery answer that is OK is the only proof of delivery: the
 *    command becomes "delivered" when EVERY part answered;
 *  - a delivery answer that is not OK means the carrier could not hand
 *    the message over: the command becomes "failed" (`delivery_<code>`);
 *  - an answer that cannot be matched to a live trace changes nothing.
 *
 * A transition is reported to the backend by the next command poll, which
 * is queued here immediately instead of waiting for the next poll.
 */
class CommandSendStatusReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val commandId = intent.getStringExtra(EXTRA_COMMAND_ID) ?: return
        val phase = CommandPartTracker.Phase.fromWire(intent.getStringExtra(EXTRA_KIND))
            ?: return
        val partIndex = intent.getIntExtra(EXTRA_PART_INDEX, -1)
        if (partIndex < 0) return
        val ok = resultCode == Activity.RESULT_OK
        val code = resultCode

        val pending = goAsync()
        Thread {
            try {
                handle(context, commandId, phase, partIndex, ok, code)
            } catch (e: Exception) {
                // A failed status update must never crash the app.
            } finally {
                pending.finish()
            }
        }.start()
    }

    private fun handle(
        context: Context,
        commandId: String,
        phase: CommandPartTracker.Phase,
        partIndex: Int,
        ok: Boolean,
        code: Int
    ) {
        val dao = CommandDatabase.get(context).handledCommandDao()
        val entry = dao.findById(commandId) ?: return
        if (entry.status == HandledCommandEntity.STATUS_FAILED ||
            entry.status == HandledCommandEntity.STATUS_REJECTED ||
            entry.status == HandledCommandEntity.STATUS_DELIVERED
        ) {
            return
        }

        when (CommandPartTracker.record(context, commandId, phase, partIndex, ok)) {
            CommandPartTracker.Outcome.AllOk -> {
                if (phase == CommandPartTracker.Phase.DELIVERED) {
                    val changed = dao.markDelivered(commandId)
                    CommandPartTracker.clear(context, commandId)
                    if (changed > 0) Schedule.enqueueCommands(context)
                }
                // An accepted sent phase needs no change: the worker has
                // already recorded "sent".
            }

            CommandPartTracker.Outcome.Failed -> {
                val detail = if (phase == CommandPartTracker.Phase.DELIVERED) {
                    "delivery_$code"
                } else {
                    "radio_$code"
                }
                val changed = dao.markFailed(commandId, detail)
                CommandPartTracker.clear(context, commandId)
                if (changed > 0) Schedule.enqueueCommands(context)
            }

            CommandPartTracker.Outcome.Pending,
            CommandPartTracker.Outcome.Unknown -> Unit
        }
    }

    companion object {
        const val EXTRA_COMMAND_ID = "dev.smsrelay.extra.COMMAND_ID"
        const val EXTRA_KIND = "dev.smsrelay.extra.KIND"
        const val EXTRA_PART_INDEX = "dev.smsrelay.extra.PART_INDEX"

        const val KIND_SENT = "sent"
        const val KIND_DELIVERED = "delivered"
    }
}
