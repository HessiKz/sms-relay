package dev.smsrelay.calls

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.telecom.Call
import android.telecom.CallAudioState
import android.telecom.TelecomManager
import android.telecom.VideoProfile
import androidx.core.content.ContextCompat
import dev.smsrelay.net.CallAction
import dev.smsrelay.util.PhoneNumbers

/**
 * The emergency numbers of the regions this relay serves.
 *
 * One list for the whole app: the dialpad, the remote-action executor and
 * the idle dial poll all have to agree. A number that is not in the list
 * takes the normal path, which is what the platform does as well.
 *
 * The check is on the DIGITS: `0112` reaches the same service as `112`,
 * so a leading zero must not turn an emergency number into a normal one.
 */
object EmergencyNumbers {

    val NUMBERS: Set<String> = setOf("110", "112", "115", "125", "911", "999", "000")

    fun matches(text: String?): Boolean {
        if (text.isNullOrBlank()) return false
        val digits = PhoneNumbers.toAsciiDigits(text).filter { it.isDigit() }
        if (digits.isEmpty()) return false
        if (digits in NUMBERS) return true
        val trimmed = digits.trimStart('0')
        return trimmed.isNotEmpty() && trimmed in NUMBERS
    }
}

/** The action names of the `GET /calls/actions` contract. */
object CallActionNames {
    const val ANSWER = "answer"
    const val REJECT = "reject"
    const val HANGUP = "hangup"
    const val MUTE = "mute"
    const val UNMUTE = "unmute"
    const val SPEAKER_ON = "speaker_on"
    const val SPEAKER_OFF = "speaker_off"
    const val HOLD = "hold"
    const val UNHOLD = "unhold"
    const val DTMF = "dtmf"
    const val DIAL = "dial"

    /** True for the actions that only name a live call. */
    fun needsLiveCall(action: String): Boolean = action != DIAL
}

/**
 * How one remote action ended, in the vocabulary of
 * `POST /calls/actions/{id}/result`.
 *
 * [detail] is short, contains no number, name or body, and is what the
 * Mini App shows under a failed tap.
 */
sealed class ActionResult(val status: String, val detail: String?) {
    object Done : ActionResult("done", null)
    class Failed(detail: String) : ActionResult("failed", detail)
    class Rejected(detail: String) : ActionResult("rejected", detail)
}

/**
 * Runs one call action against the Telecom `Call`.
 *
 * Two steps, deliberately separate:
 *
 *  1. [refuse] is pure policy. It needs no `Call` and no permission, so
 *     it runs BEFORE the action id is written into the guard: a stale or
 *     expired action is refused without being marked as executed.
 *  2. [perform] touches the `Call` (or the radio, for `dial`).
 *
 * Every `Call` method is a binder call; run [perform] on the main thread
 * so an OEM that expects it there never sees a wrong-thread call. None
 * of the operations here blocks on I/O, so the main thread is fine.
 *
 * Nothing in this file logs a number, a name or a tone.
 */
object CallExecutor {

    /**
     * The policy check. Returns the refusal, or null when the action may
     * be executed.
     *
     * [nowSeconds] is the wall clock in epoch seconds; [CallAction.expiresAt]
     * comes from the server in the same unit.
     */
    fun refuse(action: CallAction, session: CallSession?, nowSeconds: Long): ActionResult.Rejected? {
        val name = action.action
        if (name !in KNOWN_ACTIONS) return ActionResult.Rejected(DETAIL_UNKNOWN_ACTION)

        val expiry = expirySeconds(action.expiresAt)
        if (expiry != null && expiry <= nowSeconds) return ActionResult.Rejected(DETAIL_EXPIRED)

        if (name == CallActionNames.DIAL) {
            if (action.callRef.isNotBlank()) return ActionResult.Rejected(DETAIL_UNEXPECTED_REF)
            val target = dialableTarget(action.payload) ?: return ActionResult.Rejected(DETAIL_INVALID_TARGET)
            if (EmergencyNumbers.matches(target)) return ActionResult.Rejected(DETAIL_EMERGENCY)
            return null
        }

        // Every other action names the live call. A ref that is not the
        // current session ref is stale: it must never land on this call.
        val current = session ?: return ActionResult.Rejected(DETAIL_NO_CALL)
        if (!current.isLive) return ActionResult.Rejected(DETAIL_NO_CALL)
        if (action.callRef != current.callRef) return ActionResult.Rejected(DETAIL_STALE_CALL)
        if (name == CallActionNames.DTMF) {
            val tone = action.payload?.trim().orEmpty()
            if (tone.length != 1 || tone[0] !in DTMF_TONES) {
                return ActionResult.Rejected(DETAIL_INVALID_TONE)
            }
        }
        return null
    }

    /**
     * Runs the action that [refuse] accepted.
     *
     * Main thread. A `Call` that refuses an operation (holding a ringing
     * call, for instance) is a normal answer: the action FAILS, the call
     * stays up and nothing is thrown at Telecom.
     *
     * [audio] is the service, because mute and the audio route live on
     * `InCallService` and not on `Call`.
     */
    fun perform(
        context: Context,
        call: Call?,
        audio: CallAudioControl?,
        action: CallAction,
        main: Handler
    ): ActionResult {
        val name = action.action
        if (name == CallActionNames.DIAL) return placeDial(context, action.payload)
        if (name == CallActionNames.MUTE) {
            if (audio == null) return ActionResult.Failed(DETAIL_NO_CALL)
            audio.setMuted(true)
            return ActionResult.Done
        }
        if (name == CallActionNames.UNMUTE) {
            if (audio == null) return ActionResult.Failed(DETAIL_NO_CALL)
            audio.setMuted(false)
            return ActionResult.Done
        }
        if (name == CallActionNames.SPEAKER_ON) {
            if (audio == null) return ActionResult.Failed(DETAIL_NO_CALL)
            audio.setAudioRoute(CallAudioState.ROUTE_SPEAKER)
            return ActionResult.Done
        }
        if (name == CallActionNames.SPEAKER_OFF) {
            if (audio == null) return ActionResult.Failed(DETAIL_NO_CALL)
            audio.setAudioRoute(CallAudioState.ROUTE_EARPIECE)
            return ActionResult.Done
        }
        if (call == null) return ActionResult.Failed(DETAIL_NO_CALL)

        return try {
            when (name) {
                CallActionNames.ANSWER -> {
                    // The state is read from the Call itself: Call.getState()
                    // exists on every supported release. (Call.Details also
                    // has getState(), but only since API 31, and minSdk is
                    // 26. Call.getState() is the deprecated-but-present
                    // form; it still returns the live state.)
                    if (call.state != Call.STATE_RINGING) {
                        ActionResult.Rejected(DETAIL_NOT_RINGING)
                    } else {
                        call.answer(VideoProfile.STATE_AUDIO_ONLY)
                        ActionResult.Done
                    }
                }

                CallActionNames.REJECT -> {
                    // Same rule as ANSWER: the live state comes from Call.
                    if (call.state != Call.STATE_RINGING) {
                        ActionResult.Rejected(DETAIL_NOT_RINGING)
                    } else {
                        // The second argument is the "reject with a text
                        // message" body; this app never sends one.
                        call.reject(false, null)
                        ActionResult.Done
                    }
                }

                CallActionNames.HANGUP -> {
                    call.disconnect()
                    ActionResult.Done
                }

                CallActionNames.HOLD -> {
                    call.hold()
                    ActionResult.Done
                }

                CallActionNames.UNHOLD -> {
                    call.unhold()
                    ActionResult.Done
                }

                CallActionNames.DTMF -> {
                    val tone = action.payload?.trim().orEmpty()
                    val character = tone.firstOrNull()
                        ?: return ActionResult.Rejected(DETAIL_INVALID_TONE)
                    if (tone.length != 1 || character !in DTMF_TONES) {
                        return ActionResult.Rejected(DETAIL_INVALID_TONE)
                    }
                    call.playDtmfTone(character)
                    // The tone must be stopped, or the far end hears one
                    // long beep. A fixed short tone is what a keypad does.
                    main.postDelayed(
                        { runCatching { call.stopDtmfTone() } },
                        DTMF_TONE_MS
                    )
                    ActionResult.Done
                }

                else -> ActionResult.Rejected(DETAIL_UNKNOWN_ACTION)
            }
        } catch (e: SecurityException) {
            ActionResult.Failed(DETAIL_NOT_ALLOWED)
        } catch (e: RuntimeException) {
            // IllegalStateException (hold on a ringing call), a dead
            // binder, a vendor stack saying no: the call must survive.
            ActionResult.Failed(DETAIL_REFUSED)
        }
    }

    /**
     * Places a `dial` action locally.
     *
     * No SIM handle is put into the bundle: the platform picks the card,
     * exactly like an emergency call. An emergency number is refused
     * here even though the server refuses it too - a safety rule that
     * lives only on the server is one network bug away from being gone.
     */
    fun placeDial(context: Context, payload: String?): ActionResult {
        val target = dialableTarget(payload) ?: return ActionResult.Rejected(DETAIL_INVALID_TARGET)
        if (EmergencyNumbers.matches(target)) return ActionResult.Rejected(DETAIL_EMERGENCY)
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CALL_PHONE) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return ActionResult.Failed(DETAIL_NO_PERMISSION)
        }
        val telecom = context.getSystemService(TelecomManager::class.java)
            ?: return ActionResult.Failed(DETAIL_NO_TELECOM)
        return try {
            telecom.placeCall(Uri.fromParts("tel", target, null), Bundle())
            ActionResult.Done
        } catch (e: SecurityException) {
            ActionResult.Failed(DETAIL_NOT_ALLOWED)
        } catch (e: RuntimeException) {
            ActionResult.Failed(DETAIL_REFUSED)
        }
    }

    /**
     * The payload of a `dial` action as something the radio can call.
     *
     * The server normalises the number before it queues the action, so
     * the payload is `+98912…` or `short:1234` (the thread-key shapes of
     * the backend). Both are turned into a dial string here; anything
     * without at least two digits is not a destination.
     */
    fun dialableTarget(payload: String?): String? {
        val raw = payload?.trim().orEmpty()
        if (raw.isEmpty()) return null
        val withoutPrefix = when {
            raw.startsWith(SHORT_PREFIX, ignoreCase = true) -> raw.substring(SHORT_PREFIX.length)
            else -> raw
        }
        val ascii = PhoneNumbers.toAsciiDigits(withoutPrefix)
        val cleaned = ascii.filter { it.isDigit() || it == '+' || it == '*' || it == '#' }.trim()
        if (cleaned.count { it.isDigit() } < MIN_DIAL_DIGITS) return null
        return cleaned
    }

    /**
     * `expires_at` in epoch seconds.
     *
     * The contract says seconds. A phone that is talking to a server
     * which sent milliseconds would see every action as valid for
     * decades; the shape check makes that harmless.
     */
    private fun expirySeconds(value: Long): Long? {
        if (value <= 0L) return null
        return if (value > MILLIS_THRESHOLD) value / 1000L else value
    }

    /** The tones a keypad may send; a single character each. */
    private val DTMF_TONES = "0123456789*#".toSet()

    private val KNOWN_ACTIONS = setOf(
        CallActionNames.ANSWER, CallActionNames.REJECT, CallActionNames.HANGUP,
        CallActionNames.MUTE, CallActionNames.UNMUTE,
        CallActionNames.SPEAKER_ON, CallActionNames.SPEAKER_OFF,
        CallActionNames.HOLD, CallActionNames.UNHOLD,
        CallActionNames.DTMF, CallActionNames.DIAL
    )

    private const val SHORT_PREFIX = "short:"
    private const val MIN_DIAL_DIGITS = 2
    private const val DTMF_TONE_MS = 150L

    /** Seconds above this are milliseconds, not a date before 1970. */
    private const val MILLIS_THRESHOLD = 10_000_000_000L

    const val DETAIL_UNKNOWN_ACTION = "unknown action"
    const val DETAIL_EXPIRED = "expired"
    const val DETAIL_UNEXPECTED_REF = "unexpected call ref"
    const val DETAIL_INVALID_TARGET = "invalid destination"
    const val DETAIL_EMERGENCY = "emergency number"
    const val DETAIL_NO_CALL = "no live call"
    const val DETAIL_STALE_CALL = "stale call"
    const val DETAIL_INVALID_TONE = "invalid tone"
    const val DETAIL_NOT_RINGING = "not ringing"
    const val DETAIL_NOT_ALLOWED = "not allowed"
    const val DETAIL_REFUSED = "the phone refused"
    const val DETAIL_NO_PERMISSION = "no call permission"
    const val DETAIL_NO_TELECOM = "no telephony service"
}
