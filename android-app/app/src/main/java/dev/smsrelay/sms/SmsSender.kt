package dev.smsrelay.sms

import android.app.PendingIntent
import android.content.Context
import android.os.Build
import android.telephony.SmsManager
import dev.smsrelay.SettingsStore

/**
 * Sends an SMS through the phone's own SIM.
 *
 * Shared by the cellular fallback (FallbackSender), the message the user
 * composes in the app (MainActivity) and the commands that arrive from
 * Telegram (CommandWorker), so all three pick the same SIM and split long
 * bodies the same way.
 *
 * The body of a message is never written to a log or to a status text.
 */
object SmsSender {

    /** Outcome of one send attempt. */
    sealed class Outcome {
        /** The radio accepted the message. */
        object Sent : Outcome()

        /** The message never left the phone; [reason] is a short code. */
        data class Failed(val reason: String) : Outcome()
    }

    const val REASON_NO_MANAGER = "no_sms_manager"
    const val REASON_PERMISSION = "permission_denied"
    const val REASON_INVALID_TARGET = "invalid_target"
    const val REASON_EMPTY_BODY = "empty_body"
    const val REASON_SEND_ERROR = "send_error"

    /** The card the user picked cannot be used right now. */
    const val REASON_SIM_UNAVAILABLE = "sim_unavailable"

    /** Destination numbers are E.164-ish: an optional + and 5..20 digits. */
    private val TARGET_PATTERN = Regex("^\\+?[0-9]{5,20}$")

    fun isValidTarget(destination: String): Boolean =
        TARGET_PATTERN.matches(destination.trim())

    /**
     * Hand the message to the radio.
     *
     * [sentIntents] is optional: it is called with the number of parts the
     * radio will send and must return one PendingIntent per part. The
     * radio answers each part separately, so a multipart message is only
     * confirmed when every part reports success. "Sent" means the radio
     * accepted the message, not that the recipient read it.
     *
     * [deliveryIntents] follows the same contract: the carrier answers
     * each part separately, and only its answers can confirm delivery.
     *
     * [subscriptionId] is the card chosen by the sender of a command. An
     * explicit card is used exactly: when it is no longer active the
     * message FAILS instead of silently leaving through another card.
     * Null or 0 keeps the normal rule of [resolveSubscriptionId].
     */
    fun send(
        context: Context,
        settings: SettingsStore,
        destination: String,
        body: String,
        subscriptionId: Int? = null,
        sentIntents: ((partCount: Int) -> List<PendingIntent>)? = null,
        deliveryIntents: ((partCount: Int) -> List<PendingIntent>)? = null
    ): Outcome {
        val target = destination.trim()
        if (!isValidTarget(target)) return Outcome.Failed(REASON_INVALID_TARGET)
        if (body.isBlank()) return Outcome.Failed(REASON_EMPTY_BODY)
        val manager = if (subscriptionId != null && subscriptionId > 0) {
            if (SimCards.find(context, subscriptionId) == null) {
                return Outcome.Failed(REASON_SIM_UNAVAILABLE)
            }
            managerForSubscription(context, subscriptionId)
                ?: return Outcome.Failed(REASON_SIM_UNAVAILABLE)
        } else {
            managerForSim(context, settings)
        }
        if (manager == null) {
            // No card could be used: either the phone has no SMS service
            // at all, or the card the user picked refuses the message.
            val reason = if (context.getSystemService(SmsManager::class.java) == null) {
                REASON_NO_MANAGER
            } else {
                REASON_SIM_UNAVAILABLE
            }
            return Outcome.Failed(reason)
        }
        return try {
            val parts = manager.divideMessage(body)
            val perPart = sentIntents?.let { ArrayList(it.invoke(parts.size)) }
            val perPartDelivery = deliveryIntents?.let { ArrayList(it.invoke(parts.size)) }
            manager.sendMultipartTextMessage(
                target,
                null,
                parts,
                perPart,
                perPartDelivery
            )
            Outcome.Sent
        } catch (e: SecurityException) {
            Outcome.Failed(REASON_PERMISSION)
        } catch (e: Exception) {
            Outcome.Failed(REASON_SEND_ERROR)
        }
    }

    /**
     * The subscription id the messages of this app go out with.
     *
     * The rule is the one the user expects:
     *
     *  - the card the user picked in the app wins;
     *  - a card that is no longer active (taken out, deactivated) is
     *    forgotten, and the first active card takes over;
     *  - without a choice of its own the FIRST active card is used, the
     *    one in slot 1 (SIM 1).
     *
     * The default is deliberately NOT the default SMS card of the system:
     * on a dual SIM phone that is often the second card, and the message
     * would leave through a card the user never chose.
     *
     * Null means: the phone has no active card at all.
     */
    fun resolveSubscriptionId(context: Context, settings: SettingsStore): Int? {
        val cards = SimCards.active(context)
        if (cards.isEmpty()) return null
        val chosen = settings.simSubscriptionId
        if (chosen != null) {
            val match = cards.firstOrNull { it.subscriptionId == chosen }
            if (match != null) return match.subscriptionId
        }
        return cards.first().subscriptionId
    }

    /**
     * Resolve the SmsManager for the SIM the messages go out with.
     *
     * This is the ONE place that decides which card is used: the composed
     * message, the commands from Telegram and the cellular fallback all
     * come through here, so they can never use different cards.
     *
     * A stale subscription id would silently fall back to another card, so
     * the id is always checked against the active cards first.
     */
    fun managerForSim(context: Context, settings: SettingsStore): SmsManager? {
        val system = context.getSystemService(SmsManager::class.java) ?: return null
        val subscriptionId = resolveSubscriptionId(context, settings) ?: return system
        return managerForSubscription(context, subscriptionId)
    }

    /**
     * The SmsManager bound to one subscription. `createForSubscriptionId`
     * replaced the static factory in Android 12; older phones still need
     * the deprecated call.
     */
    @Suppress("DEPRECATION")
    private fun managerForSubscription(context: Context, subscriptionId: Int): SmsManager? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            context.getSystemService(SmsManager::class.java)
                ?.let { runCatching { it.createForSubscriptionId(subscriptionId) }.getOrNull() }
        } else {
            runCatching {
                SmsManager.getSmsManagerForSubscriptionId(subscriptionId)
            }.getOrNull()
        }
}
