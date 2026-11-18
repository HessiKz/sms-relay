package dev.smsrelay.calls

import android.os.Handler
import android.os.Looper
import java.util.concurrent.CopyOnWriteArrayList

/**
 * The call states of the `POST /calls/events` contract.
 *
 * The server keeps one primary call per device and merges events of the
 * same `call_ref`; an unknown state is refused, so the phone must only
 * ever send these five strings.
 */
object CallStates {
    const val RINGING = "ringing"
    const val DIALING = "dialing"
    const val ACTIVE = "active"
    const val HELD = "held"
    const val DISCONNECTED = "disconnected"
}

/** The two directions of the contract. */
object CallDirections {
    const val INCOMING = "incoming"
    const val OUTGOING = "outgoing"
}

/**
 * One immutable state report for `POST /calls/events`.
 *
 * It is immutable on purpose: the report is handed to a background
 * worker, and a mutable object could change between being queued and
 * being written to the socket.
 *
 * [startedAt] and [endedAt] are epoch SECONDS (the server refuses a
 * value in the future and stores seconds, not milliseconds).
 */
data class CallEvent(
    val callRef: String,
    val state: String,
    val direction: String,
    val number: String,
    val simLabel: String?,
    val muted: Boolean,
    val speaker: Boolean,
    val startedAt: Long,
    val endedAt: Long?
)

/**
 * The primary call of this phone, as the in-call UI and the action
 * executor see it.
 *
 * A device has one primary call (the design keeps call waiting and
 * conference out of scope); the fields are read from several threads,
 * which is why the mutable ones are `@Volatile`. Nothing here is ever
 * logged.
 *
 * `callRef` is a phone-generated UUID that is valid until the call
 * disconnects; it is the only thing a remote action may target.
 */
class CallSession(
    val callRef: String,
    val direction: String,
    val number: String,
    val simLabel: String?,
    val startedAt: Long
) {
    @Volatile
    var state: String = CallStates.RINGING

    @Volatile
    var muted: Boolean = false

    @Volatile
    var speaker: Boolean = false

    /** When the call was answered; null while it still rings. */
    @Volatile
    var answeredAt: Long? = null

    /** When the call ended; null while it is still up. */
    @Volatile
    var endedAt: Long? = null

    /** Ringing, dialing, active or held: anything but disconnected. */
    val isLive: Boolean get() = state != CallStates.DISCONNECTED

    /** The moment the on-screen timer counts from. */
    val countingSince: Long get() = answeredAt ?: startedAt

    fun toEvent(): CallEvent = CallEvent(
        callRef = callRef,
        state = state,
        direction = direction,
        number = number,
        simLabel = simLabel,
        muted = muted,
        speaker = speaker,
        startedAt = startedAt,
        endedAt = endedAt
    )

    override fun toString(): String = "CallSession(state=$state, direction=$direction)"
}

/**
 * The local controls the in-call screen needs.
 *
 * They are implemented by [CallService], which owns the Telecom `Call`.
 * A button that finds no handle (the service is gone) simply does
 * nothing: the screen never crashes and never keeps a dead call alive.
 */
interface CallHandle {
    fun answer()
    fun reject()
    fun hangup()

    /**
     * Mute and the audio route. They are implemented by the
     * `InCallService`, not by the `Call`: see [CallAudioControl].
     */
    fun setMuted(muted: Boolean)
    fun setSpeaker(speaker: Boolean)

    fun setHold(hold: Boolean)
    fun playDtmf(tone: Char)
}

/**
 * The audio controls of the in-call service.
 *
 * Mute and the audio route are NOT methods of `Call`: they belong to
 * `InCallService` (the service owns the audio session of every call it
 * shows). This interface is how [CallExecutor] reaches them without
 * taking a dependency on the service class.
 */
interface CallAudioControl {
    fun setMuted(muted: Boolean)
    fun setAudioRoute(route: Int)
}

/**
 * The one live call of this process, published by [CallService].
 *
 * The service and the in-call screen live in the same process, so this
 * registry replaces a broadcast: it is synchronous, it cannot be spoofed
 * by another app and it costs no permission. A listener is always
 * called on the main thread.
 */
object LiveCallRegistry {

    @Volatile
    var session: CallSession? = null
        private set

    /** Set by the running service; null when no service is bound. */
    @Volatile
    var handle: CallHandle? = null

    /** True while the in-call screen is on screen (see [CallActivity]). */
    @Volatile
    var uiVisible: Boolean = false

    private val main = Handler(Looper.getMainLooper())
    private val listeners = CopyOnWriteArrayList<Listener>()

    fun interface Listener {
        fun onCallChanged(session: CallSession?)
    }

    /** True while a call is up; the idle dial poll must then stay quiet. */
    val isBusy: Boolean get() = session?.isLive == true

    fun addListener(listener: Listener) {
        if (!listeners.contains(listener)) listeners.add(listener)
    }

    fun removeListener(listener: Listener) {
        listeners.remove(listener)
    }

    /**
     * Publishes a session, or null when the call is over.
     *
     * Listeners are told on the main thread, so a listener may touch
     * views directly. A throwing listener is contained: the UI must
     * never be able to break the Telecom callback that published.
     */
    fun publish(session: CallSession?) {
        this.session = session
        main.post {
            for (listener in listeners) {
                runCatching { listener.onCallChanged(session) }
            }
        }
    }
}
