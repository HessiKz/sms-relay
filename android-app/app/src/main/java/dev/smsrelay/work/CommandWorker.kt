package dev.smsrelay.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.smsrelay.BuildConfig
import dev.smsrelay.SettingsStore
import dev.smsrelay.contacts.ContactResolver
import dev.smsrelay.data.CommandDatabase
import dev.smsrelay.data.HandledCommandEntity
import dev.smsrelay.net.RelayClient
import dev.smsrelay.sms.CommandPartTracker
import dev.smsrelay.sms.CommandSendStatusReceiver
import dev.smsrelay.sms.SimCards
import dev.smsrelay.sms.SmsSender
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject

/**
 * Executes the SMS commands that arrived from Telegram.
 *
 * The backend keeps a queue of commands ("send this text to that number")
 * and the phone picks them up with its own SIM. The rules:
 *
 *  - while sending is disabled in the app the command is REFUSED, not
 *    silently dropped: the admin learns about it in Telegram;
 *  - the target may be a number or the name of a contact; a target that
 *    is neither is FAILED with the reason "unknown contact", so the
 *    admin learns that no message was sent;
 *  - a command is executed at most once. The local row is written before
 *    the radio is asked to send, so a lost report can cost a repeated
 *    status message but never a repeated SMS;
 *  - a result that the backend did not receive is reported again on the
 *    next run;
 *  - the body of a message is never logged and never shown in the UI.
 */
class CommandWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val settings = SettingsStore(applicationContext)
        if (settings.baseUri.isBlank()) return@withContext Result.success()
        val client = RelayClient(settings.baseUri, settings.token)
        val dao = CommandDatabase.get(applicationContext).handledCommandDao()
        var retryNeeded = false

        // 1) Results the backend never received.
        for (entry in dao.unreported(limit = BATCH)) {
            if (client.reportCommandResult(entry.id, entry.status, entry.detail)) {
                dao.markReported(entry.id, entry.status)
            } else {
                retryNeeded = true
            }
        }

        // 2) Commands whose execution was interrupted (the app was killed
        //    between taking the command and the radio call). They are
        //    failed, never repeated.
        val interruptedBefore = System.currentTimeMillis() - STALE_HANDLING_MS
        for (entry in dao.staleHandling(interruptedBefore, BATCH)) {
            dao.finishAttempt(entry.id, HandledCommandEntity.STATUS_FAILED, DETAIL_INTERRUPTED)
            val stored = dao.findById(entry.id)
            val status = stored?.status
                ?.takeUnless { it == HandledCommandEntity.STATUS_HANDLING }
                ?: HandledCommandEntity.STATUS_FAILED
            val detail = stored?.detail ?: DETAIL_INTERRUPTED
            if (client.reportCommandResult(entry.id, status, detail)) {
                dao.markReported(entry.id, status)
            } else {
                retryNeeded = true
            }
            settings.lastCommandStatus =
                if (detail.isNullOrBlank()) status else status + ": " + detail
            settings.lastCommandAt = System.currentTimeMillis()
        }

        // 3) New commands.
        for (command in client.fetchCommands(deviceInfo(settings))) {
            // The local guard: a command that was handled before is never
            // executed a second time.
            if (dao.findById(command.id) != null) continue

            if (!settings.sendingEnabled) {
                dao.upsert(
                    HandledCommandEntity(
                        id = command.id,
                        status = HandledCommandEntity.STATUS_REJECTED,
                        detail = DETAIL_SENDING_DISABLED
                    )
                )
                if (client.reportCommandResult(command.id, HandledCommandEntity.STATUS_REJECTED, DETAIL_SENDING_DISABLED)) {
                    dao.markReported(command.id, HandledCommandEntity.STATUS_REJECTED)
                } else {
                    retryNeeded = true
                }
                settings.lastCommandStatus = HandledCommandEntity.STATUS_REJECTED
                settings.lastCommandAt = System.currentTimeMillis()
                continue
            }

            // The target is usually a number, but it may also be the
            // name of a contact ("Alice"). The phone has the contact
            // list, so it answers that question itself.
            val destination = ContactResolver.resolve(applicationContext, command.target)
            if (destination == null) {
                dao.upsert(
                    HandledCommandEntity(
                        id = command.id,
                        status = HandledCommandEntity.STATUS_FAILED,
                        detail = DETAIL_UNKNOWN_CONTACT
                    )
                )
                if (client.reportCommandResult(
                        command.id,
                        HandledCommandEntity.STATUS_FAILED,
                        DETAIL_UNKNOWN_CONTACT
                    )
                ) {
                    dao.markReported(command.id, HandledCommandEntity.STATUS_FAILED)
                } else {
                    retryNeeded = true
                }
                settings.lastCommandStatus = HandledCommandEntity.STATUS_FAILED
                settings.lastCommandAt = System.currentTimeMillis()
                continue
            }

            // The guard row is written BEFORE the radio call.
            dao.upsert(
                HandledCommandEntity(
                    id = command.id,
                    status = HandledCommandEntity.STATUS_HANDLING
                )
            )
            val outcome = SmsSender.send(
                context = applicationContext,
                settings = settings,
                destination = destination,
                body = command.body,
                subscriptionId = command.subId.takeIf { it > 0 },
                sentIntents = { partCount ->
                    CommandPartTracker.begin(applicationContext, command.id, partCount)
                    CommandPartTracker.pendingIntents(
                        applicationContext,
                        command.id,
                        CommandSendStatusReceiver.KIND_SENT,
                        partCount
                    )
                },
                deliveryIntents = { partCount ->
                    CommandPartTracker.pendingIntents(
                        applicationContext,
                        command.id,
                        CommandSendStatusReceiver.KIND_DELIVERED,
                        partCount
                    )
                }
            )
            val attemptStatus: String
            val attemptDetail: String?
            when (outcome) {
                SmsSender.Outcome.Sent -> {
                    attemptStatus = HandledCommandEntity.STATUS_SENT
                    attemptDetail = null
                }

                is SmsSender.Outcome.Failed -> {
                    attemptStatus = HandledCommandEntity.STATUS_FAILED
                    attemptDetail = outcome.reason
                    CommandPartTracker.clear(applicationContext, command.id)
                }
            }
            dao.finishAttempt(command.id, attemptStatus, attemptDetail)

            // A radio or carrier answer may have arrived while the send
            // call was returning; that answer is newer than the attempt.
            val stored = dao.findById(command.id)
            val status = stored?.status
                ?.takeUnless { it == HandledCommandEntity.STATUS_HANDLING }
                ?: attemptStatus
            val detail = if (stored == null || status == attemptStatus) {
                attemptDetail
            } else {
                stored.detail
            }
            settings.lastCommandStatus =
                if (detail.isNullOrBlank()) status else status + ": " + detail
            settings.lastCommandAt = System.currentTimeMillis()

            if (client.reportCommandResult(command.id, status, detail)) {
                dao.markReported(command.id, status)
            } else {
                retryNeeded = true
            }
        }

        // Old rows are dropped: the backend forgets them as well.
        dao.prune(System.currentTimeMillis() - RETENTION_MS)

        // The next poll of the chain: WorkManager does not allow a periodic
        // work below 15 minutes, so the short cadence is a chain.
        Schedule.enqueueNextCommandPoll(applicationContext)

        if (retryNeeded) Result.retry() else Result.success()
    }

    /**
     * Compact heartbeat for the server: the installation id, the app
     * version and the active cards. Never the token, a phone number or a
     * contact name.
     */
    private fun deviceInfo(settings: SettingsStore): String {
        val sims = JSONArray()
        for (card in SimCards.active(applicationContext)) {
            sims.put(
                JSONObject().apply {
                    put("sub_id", card.subscriptionId)
                    put("slot", card.slotIndex)
                    card.displayName?.take(MAX_SIM_LABEL_LEN)?.let { put("label", it) }
                }
            )
        }
        return JSONObject().apply {
            put("id", settings.deviceId)
            put("version", BuildConfig.VERSION_NAME)
            put("sending", settings.sendingEnabled)
            put("sims", sims)
        }.toString()
    }

    companion object {
        /** How many commands are handled per run. */
        private const val BATCH = 20

        /** A command that is still "handling" after this long was interrupted. */
        private const val STALE_HANDLING_MS = 15 * 60 * 1000L

        /** Handled commands are kept for a month. */
        private const val RETENTION_MS = 30L * 24 * 60 * 60 * 1000

        /** The server drops a longer SIM label, so it is cut here. */
        private const val MAX_SIM_LABEL_LEN = 32

        const val DETAIL_SENDING_DISABLED = "sending disabled"
        const val DETAIL_INTERRUPTED = "interrupted"

        /** The target was neither a number nor a contact of the phone. */
        const val DETAIL_UNKNOWN_CONTACT = "unknown contact"
    }
}
