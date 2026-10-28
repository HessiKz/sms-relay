package dev.smsrelay.sms

import dev.smsrelay.SettingsStore
import dev.smsrelay.data.FallbackLogDao
import dev.smsrelay.data.MessageDao
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.net.RelayClient

/**
 * Decides when a direct-SIM fallback SMS to Phone B is justified.
 *
 * Deliberately conservative. A fallback is sent only when the backend
 * CONFIRMED that Telegram delivery failed for that message id
 * (`state == "failed"`).
 *
 * A timeout is NOT a confirmation: the backend may have delivered the
 * message anyway, so nothing is sent over the air in that case. A
 * rejected-by-the-API response (429, 5xx) is a retryable condition, not
 * a confirmed delivery failure, so it never triggers a fallback either.
 *
 * The caps (per message, per hour, minimum gap) count CONFIRMED sends
 * only. An attempt that the radio refused does not consume the allowance,
 * so the message can be retried after the minimum gap.
 */
class FallbackPolicy(
    private val settings: SettingsStore,
    private val messageDao: MessageDao,
    private val fallbackLogDao: FallbackLogDao
) {

    /**
     * Called after every upload cycle: asks the backend for the delivery
     * state of the messages the app still owns and sends a fallback only
     * for the ones the backend marked as failed.
     */
    fun onPeriodicSweep(client: RelayClient, sender: FallbackSender) {
        if (!settings.fallbackEnabled) return
        // A message uploaded normally is "sent" locally, so the sweep has
        // to look at every state the backend may still turn into a
        // confirmed failure -- not only at what is still queued.
        val since = System.currentTimeMillis() / 1000L - SWEEP_WINDOW_SECONDS
        val candidates = messageDao.fallbackCandidates(
            states = listOf(
                MessageRecord.STATE_QUEUED,
                MessageRecord.STATE_SENT,
                MessageRecord.STATE_DUPLICATE,
                MessageRecord.STATE_FAILED
            ),
            fallbackSent = MessageRecord.FALLBACK_SENT,
            since = since,
            limit = SWEEP_LIMIT
        )
        if (candidates.isEmpty()) return

        val states = client.fetchStates(candidates.map { it.id })
        for (record in candidates) {
            val backendState = states[record.id]
            if (backendState?.state == MessageRecord.BACKEND_STATE_DELIVERED) {
                messageDao.markBackendOutcome(
                    record.id,
                    MessageRecord.STATE_DELIVERED,
                    MessageRecord.BACKEND_STATE_DELIVERED
                )
                continue
            }
            if (!authorisesFallback(record, backendState?.state)) continue
            if (!maySend(record)) continue
            // The state is updated by SendStatusReceiver once the radio
            // answers; until then the record keeps its previous value.
            sender.send(record)
        }
    }

    /**
     * Only a CONFIRMED failure justifies the cellular fallback.
     *
     *  - the backend answered "failed": confirmed;
     *  - the upload was rejected with a 4xx: confirmed by the API itself,
     *    and the backend has no row for the message at all;
     *  - still queued locally: the upload has not settled yet, so only a
     *    backend "failed" counts.
     */
    private fun authorisesFallback(record: MessageRecord, backendState: String?): Boolean =
        when (record.uploadState) {
            MessageRecord.STATE_FAILED ->
                backendState == null || backendState == MessageRecord.BACKEND_STATE_FAILED
            else -> backendState == MessageRecord.BACKEND_STATE_FAILED
        }

    private fun maySend(record: MessageRecord): Boolean {
        if (!settings.forwardEnabled) return false
        if (!settings.fallbackEnabled) return false
        // The app is read-only by default: no SMS leaves the phone while
        // the user did not allow sending.
        if (!settings.sendingEnabled) return false
        if (settings.phoneB.isBlank()) return false
        if (record.fallbackState == MessageRecord.FALLBACK_SENT) return false
        if (fallbackLogDao.countConfirmedForMessage(record.id) > 0) return false

        val now = System.currentTimeMillis()
        val last = fallbackLogDao.lastAttemptAt()
        if (last != null && now - last < settings.fallbackMinGapSeconds * 1000L) return false
        if (fallbackLogDao.countConfirmedSince(now - 3600_000L) >=
            settings.fallbackMaxPerHour
        ) {
            return false
        }
        return true
    }

    companion object {
        /** How many messages are inspected per sweep. */
        const val SWEEP_LIMIT = 50

        /** Only messages received this recently are considered. */
        const val SWEEP_WINDOW_SECONDS = 48L * 60 * 60
    }
}
