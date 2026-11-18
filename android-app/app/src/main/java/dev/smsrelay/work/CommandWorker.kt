package dev.smsrelay.work

import android.content.Context
import android.util.Log
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.smsrelay.BuildConfig
import dev.smsrelay.SettingsStore
import dev.smsrelay.contacts.ContactResolver
import dev.smsrelay.calls.CallActionGuard
import dev.smsrelay.calls.CallActionNames
import dev.smsrelay.calls.CallExecutor
import dev.smsrelay.calls.LiveCallRegistry
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
        val token = settings.token
        if (token.isBlank() || settings.baseUri.isBlank()) {
            // The phone is not paired: it asks the server for nothing.
            // The chain is started again after the pairing.
            return@withContext Result.success()
        }
        val client = RelayClient(settings.baseUri, token) { settings.handleUnauthorized() }
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

        // The device may have been removed on the server while the
        // results were reported: no request is made with a token that
        // the server does not accept.
        if (settings.token.isBlank()) return@withContext Result.success()

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
        val commands = client.fetchCommands(deviceInfo(settings))
        // The device may have been removed on the server while the poll
        // was running: no command is executed with a dead token.
        if (settings.token.isBlank()) return@withContext Result.success()
        for (command in commands) {
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

        // 4) Call actions while no call is up.
        //
        // The live poll belongs to CallService: it long-polls every call
        // and runs every action. Here only a `dial` is meaningful, and
        // only while no call is up - running a second poller next to the
        // service would be two consumers of one queue. The server marks a
        // handed-out row `delivered` atomically, so even a race here can
        // never run one action twice.
        if (!runIdleDialActions(client)) retryNeeded = true

        // The next poll of the chain: WorkManager does not allow a periodic
        // work below 15 minutes, so the short cadence is a chain.
        Schedule.enqueueNextCommandPoll(applicationContext)

        if (retryNeeded) Result.retry() else Result.success()
    }

    /**
     * Runs the `dial` actions that arrived while the phone is idle.
     *
     * Only `dial` is executed here: every other action names a live call
     * and belongs to the running [dev.smsrelay.calls.CallService], whose
     * own long-poll is the only place they are safe to run.
     *
     * But the server claims a batch of up to ten rows and marks them all
     * `delivered` whatever their kind. So a non-`dial` row can land here -
     * a race with a starting call, a row stranded when the service died,
     * a manually created one - and if this worker dropped it silently the
     * admin would wait for a result that never comes. It is therefore
     * reported `rejected` with "no live call": a terminal answer that the
     * server and the Mini App can show, without touching any call.
     *
     * Returns false when a report did not arrive and the run should
     * retry.
     */
    private fun runIdleDialActions(client: RelayClient): Boolean {
        // A live call owns the queue: CallService polls it.
        if (LiveCallRegistry.isBusy) return true
        val actions = client.pollCallActions(0)
        if (actions.isEmpty()) return true
        val guard = CallActionGuard(applicationContext)
        var ok = true
        for (action in actions) {
            if (action.action != CallActionNames.DIAL) {
                // Nothing here can run it: there is no live call. The
                // row is already `delivered`, so saying so is the only
                // way it can ever reach a terminal state.
                Log.i(TAG_CALLS, "idle poll rejects action kind: " + action.action)
                if (!client.reportCallAction(
                        action.id,
                        RESULT_REJECTED,
                        DETAIL_NO_LIVE_CALL
                    )
                ) {
                    // The report did not land. Nothing is re-executed and
                    // no report is queued: the retry re-polls, and this
                    // row is already `delivered`, so it will not come
                    // back. The flag is still set, because a failed
                    // report is a failed run and the admin should see it
                    // rather than a silent success.
                    ok = false
                }
                continue
            }
            // The guard is written before the radio is touched, so a
            // repeated poll or a restart cannot dial twice.
            val claim = guard.claim(action.id)
            if (claim != CallActionGuard.Claim.GRANTED) {
                if (claim == CallActionGuard.Claim.DUPLICATE) {
                    Log.i(TAG_CALLS, "idle dial already executed")
                    client.reportCallAction(action.id, RESULT_DONE, null)
                } else {
                    // No durable id, no call: the guard is the promise.
                    Log.w(TAG_CALLS, "idle dial guard unavailable")
                    if (!client.reportCallAction(
                            action.id,
                            "failed",
                            CallExecutor.DETAIL_REFUSED
                        )
                    ) {
                        ok = false
                    }
                }
                continue
            }
            val result = CallExecutor.placeDial(applicationContext, action.payload)
            Log.i(TAG_CALLS, "idle dial -> " + result.status)
            if (!client.reportCallAction(action.id, result.status, result.detail)) {
                // The dial itself happened: a lost report must never
                // become a second call, so it is not retried here.
                ok = false
            }
        }
        return ok
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

        /** A `dial` action that was already executed by an earlier run. */
        private const val RESULT_DONE = "done"

        /** The terminal statuses of `POST /calls/actions/{id}/result`. */
        private const val RESULT_REJECTED = "rejected"

        /**
         * Why a non-`dial` action cannot run in the idle worker.
         *
         * Short and inside the server's 200-character limit, and it says
         * nothing about the phone beyond the fact that no call is up.
         */
        private const val DETAIL_NO_LIVE_CALL = "no live call"

        /** Log tag of the call-action lines of this worker. */
        private const val TAG_CALLS = "CommandWorker.calls"
    }
}
