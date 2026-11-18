package dev.smsrelay.calls

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.telecom.Call
import android.telecom.CallAudioState
import android.telecom.InCallService
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.telecom.VideoProfile
import android.util.Log
import androidx.annotation.RequiresApi
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import dev.smsrelay.R
import dev.smsrelay.SettingsStore
import dev.smsrelay.net.CallAction
import dev.smsrelay.net.RelayClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.UUID
import java.util.concurrent.Executors

/**
 * The phone part of the calls feature: the `InCallService` of a default
 * dialer.
 *
 * Responsibilities, in the order they matter:
 *
 *  1. keep the platform's call alive - every Telecom callback is fast,
 *     exception-safe and never waits for the network;
 *  2. tell the server what is happening (`POST /calls/events`);
 *  3. run the actions the Mini App (and later the bot) asks for
 *     (`GET /calls/actions`), each one behind [CallActionGuard] so it is
 *     never executed twice;
 *  4. show the in-call screen, and a notification as a second path when
 *     the platform refuses to let the screen start.
 *
 * One primary call per device: a second call (call waiting) is kept out
 * of the state machine on purpose. It is neither answered, rejected nor
 * hung up by the relay; the phone's own network behaviour applies. That
 * keeps `call_ref` unambiguous, which is what makes "at most once"
 * possible.
 *
 * Nothing here logs a number, a name, a tone or a message: ids, states
 * and kinds only.
 */
class CallService : InCallService() {

    private val main = Handler(Looper.getMainLooper())

    /** The call the app owns. Touched on the main thread only. */
    private var primary: Call? = null

    /** Calls parked aside as call waiting, tracked only for cleanup. */
    private val ignoredCalls = mutableSetOf<Call>()

    /** The state machine of the primary call; read from worker threads. */
    @Volatile
    private var session: CallSession? = null

    private var callback: Call.Callback? = null

    /** The action long-poll. One per call. */
    private var pollScope: CoroutineScope? = null
    private var pollJob: Job? = null

    /** Event posts are serialised, so `ringing` cannot overtake `active`. */
    private val events = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "call-events").apply { isDaemon = true }
    }

    private val guard by lazy { CallActionGuard(this) }

    /** The runtime receiver of the notification buttons, if registered. */
    private var actionReceiver: BroadcastReceiver? = null

    /** One HTTP client per (base URL, token) pair, reused across events. */
    @Volatile
    private var cachedClient: RelayClient? = null

    @Volatile
    private var cachedClientKey: String = ""

    // ------------------------------------------------------------------
    // Telecom callbacks
    // ------------------------------------------------------------------

    override fun onCreate() {
        super.onCreate()
        createChannel()
        registerActionReceiver()
        Log.i(TAG, "service created")
    }

    /**
     * The notification buttons land here, not in the Activity.
     *
     * A PendingIntent into [CallActivity] would fail exactly when the
     * notification is needed most: a locked phone, a background-start
     * restriction or a vendor build that refuses the activity. A
     * broadcast to a receiver of this service needs no Activity, so
     * Answer and Decline work even when the screen cannot open.
     *
     * The receiver is registered at runtime and therefore only exists
     * while the service is bound by Telecom - which is exactly while a
     * call is up.
     */
    private fun registerActionReceiver() {
        if (actionReceiver != null) return
        val filter = IntentFilter().apply {
            addAction(ACTION_ANSWER)
            addAction(ACTION_DECLINE)
            addAction(ACTION_HANGUP)
        }
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context?, intent: Intent?) {
                val action = intent?.action ?: return
                try {
                    when (action) {
                        ACTION_ANSWER -> {
                            Log.i(TAG, "notification action: answer")
                            telecomHandle().answer()
                        }

                        ACTION_DECLINE -> {
                            Log.i(TAG, "notification action: decline")
                            telecomHandle().reject()
                        }

                        ACTION_HANGUP -> {
                            Log.i(TAG, "notification action: hangup")
                            telecomHandle().hangup()
                        }
                    }
                } catch (e: RuntimeException) {
                    Log.w(TAG, "notification action failed")
                }
            }
        }
        // The intent is always explicit (package + action), so this does
        // not need the exported flag on API 33+.
        val registered = runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                registerReceiver(receiver, filter)
            }
        }.isSuccess
        if (registered) actionReceiver = receiver
    }

    private fun unregisterActionReceiver() {
        val receiver = actionReceiver ?: return
        runCatching { unregisterReceiver(receiver) }
        actionReceiver = null
    }

    override fun onCallAdded(call: Call) {
        try {
            val current = session
            if (current != null && current.isLive) {
                // Call waiting is out of scope: the first call stays the
                // primary one, the second is left to the network.
                ignoredCalls.add(call)
                attachIgnored(call)
                Log.i(TAG, "second call ignored, keeping the primary call")
                return
            }
            becomePrimary(call)
        } catch (e: RuntimeException) {
            // A broken OEM stack must never take the call down with it.
            Log.w(TAG, "onCallAdded failed: " + e.javaClass.simpleName)
        }
    }

    override fun onCallRemoved(call: Call) {
        try {
            if (call === primary) {
                endCall()
            } else {
                ignoredCalls.remove(call)
            }
        } catch (e: RuntimeException) {
            Log.w(TAG, "onCallRemoved failed: " + e.javaClass.simpleName)
        }
    }

    /**
     * The platform's view of the audio route and the mute state.
     *
     * It fires for the hardware buttons and the headset as well as for
     * the in-call screen, so this is the one place that keeps the server
     * right about `muted` and `speaker`.
     */
    override fun onCallAudioStateChanged(audioState: CallAudioState) {
        val current = session ?: return
        val muted = runCatching { audioState.isMuted }.getOrDefault(current.muted)
        val speaker = runCatching {
            audioState.route == CallAudioState.ROUTE_SPEAKER
        }.getOrDefault(current.speaker)
        if (muted == current.muted && speaker == current.speaker) return
        current.muted = muted
        current.speaker = speaker
        // The state itself did not change, the flags did. The server
        // merges the event into the stored session.
        postEvent(current)
        LiveCallRegistry.publish(current)
        updateNotification(NotificationKind.ACTIVE, current)
    }

    override fun onDestroy() {
        Log.i(TAG, "service destroyed")
        stopPolling()
        unregisterActionReceiver()
        runCatching { events.shutdown() }
        callback?.let { cb -> runCatching { primary?.unregisterCallback(cb) } }
        callback = null
        primary = null
        session = null
        LiveCallRegistry.handle = null
        LiveCallRegistry.uiVisible = false
        LiveCallRegistry.publish(null)
        runCatching { NotificationManagerCompat.from(this).cancel(NOTIFICATION_ID) }
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Primary call lifecycle
    // ------------------------------------------------------------------

    private fun becomePrimary(call: Call) {
        val details = call.details
        val number = runCatching { details.handle?.schemeSpecificPart.orEmpty() }
            .getOrDefault("")
            .let { raw ->
                runCatching { android.net.Uri.decode(raw) }.getOrDefault(raw)
            }
        val direction = directionOf(call)
        val startedAt = System.currentTimeMillis() / 1000L
        val state = when (call.state) {
            Call.STATE_RINGING -> CallStates.RINGING
            Call.STATE_ACTIVE -> CallStates.ACTIVE
            Call.STATE_HOLDING -> CallStates.HELD
            else -> CallStates.DIALING
        }
        val built = CallSession(
            callRef = UUID.randomUUID().toString(),
            direction = direction,
            number = number,
            simLabel = simLabelOf(runCatching { details.accountHandle }.getOrNull()),
            startedAt = startedAt
        )
        built.state = state
        if (state == CallStates.ACTIVE) built.answeredAt = startedAt

        primary = call
        session = built
        LiveCallRegistry.handle = telecomHandle()
        LiveCallRegistry.publish(built)
        Log.i(TAG, "primary call added, direction=" + direction + ", state=" + state)

        attach(call)
        postEvent(built)
        openUi(built)
        startPolling()
    }

    private fun attach(call: Call) {
        val cb = object : Call.Callback() {
            override fun onStateChanged(call: Call, state: Int) {
                try {
                    handleState(call, state)
                } catch (e: RuntimeException) {
                    Log.w(TAG, "state handling failed: " + e.javaClass.simpleName)
                }
            }

            override fun onDetailsChanged(call: Call, details: Call.Details) = Unit
        }
        callback = cb
        runCatching { call.registerCallback(cb, main) }
            .onFailure { Log.w(TAG, "callback not registered") }
    }

    /** The second call is watched only so it can be forgotten cleanly. */
    private fun attachIgnored(call: Call) {
        val cb = object : Call.Callback() {
            override fun onStateChanged(call: Call, state: Int) {
                if (state == Call.STATE_DISCONNECTED) {
                    ignoredCalls.remove(call)
                    runCatching { call.unregisterCallback(this) }
                }
            }
        }
        runCatching { call.registerCallback(cb, main) }
    }

    private fun handleState(call: Call, rawState: Int) {
        val current = session ?: return
        if (call !== primary) return
        when (rawState) {
            Call.STATE_RINGING -> {
                current.state = CallStates.RINGING
                postEvent(current)
                LiveCallRegistry.publish(current)
                openUi(current)
            }

            Call.STATE_DIALING -> {
                current.state = CallStates.DIALING
                postEvent(current)
                LiveCallRegistry.publish(current)
                openUi(current)
            }

            Call.STATE_ACTIVE -> {
                current.state = CallStates.ACTIVE
                if (current.answeredAt == null) {
                    current.answeredAt = System.currentTimeMillis() / 1000L
                }
                postEvent(current)
                LiveCallRegistry.publish(current)
                openUi(current)
            }

            Call.STATE_HOLDING -> {
                current.state = CallStates.HELD
                postEvent(current)
                LiveCallRegistry.publish(current)
                updateNotification(NotificationKind.ACTIVE, current)
            }

            Call.STATE_DISCONNECTED -> endCall()

            // Connecting, selecting a phone account, pulling, and any
            // future state: the last known state is close enough, and a
            // guess here would be a state the server refuses.
            else -> Unit
        }
    }

    /**
     * Ends the primary call exactly once.
     *
     * The server stamps `ended_at` itself when it is missing, but the
     * phone sends it so a resumed session keeps the real duration.
     */
    private fun endCall() {
        val current = session ?: return
        if (current.state == CallStates.DISCONNECTED) return
        current.state = CallStates.DISCONNECTED
        current.endedAt = System.currentTimeMillis() / 1000L
        postEvent(current)
        LiveCallRegistry.publish(current)
        Log.i(TAG, "call ended")
        stopPolling()
        runCatching { NotificationManagerCompat.from(this).cancel(NOTIFICATION_ID) }
        val closing = current
        main.postDelayed({
            if (session === closing) {
                val call = primary
                val cb = callback
                if (call != null && cb != null) {
                    runCatching { call.unregisterCallback(cb) }
                }
                callback = null
                primary = null
                session = null
                LiveCallRegistry.handle = null
                LiveCallRegistry.publish(null)
            }
        }, CLEAR_SESSION_MS)
    }

    private fun directionOf(call: Call): String {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            return directionApi29(call)
        }
        // Before Android 10 there is no direction on the details: a
        // ringing call is incoming, everything else was started here.
        return if (call.state == Call.STATE_RINGING) {
            CallDirections.INCOMING
        } else {
            CallDirections.OUTGOING
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun directionApi29(call: Call): String = try {
        if (call.details.callDirection == Call.Details.DIRECTION_INCOMING) {
            CallDirections.INCOMING
        } else {
            CallDirections.OUTGOING
        }
    } catch (e: RuntimeException) {
        if (call.state == Call.STATE_RINGING) CallDirections.INCOMING else CallDirections.OUTGOING
    }

    /**
     * The label of the card a call uses ("SIM 2"), never a number.
     *
     * `getPhoneAccount` needs `READ_PHONE_STATE` or the default-dialer
     * role; a missing answer simply leaves the label empty.
     */
    private fun simLabelOf(handle: PhoneAccountHandle?): String? {
        val account = handle ?: return null
        val telecom = getSystemService(TelecomManager::class.java) ?: return null
        return runCatching { telecom.getPhoneAccount(account)?.label?.toString() }
            .getOrNull()
            ?.trim()
            ?.takeIf { it.isNotEmpty() }
    }

    // ------------------------------------------------------------------
    // Local controls (the in-call screen and the notification)
    // ------------------------------------------------------------------

    private fun telecomHandle(): CallHandle = object : CallHandle {
        override fun answer() {
            val call = primary ?: return
            if (call.state != Call.STATE_RINGING) return
            runCatching { call.answer(VideoProfile.STATE_AUDIO_ONLY) }
                .onFailure { Log.w(TAG, "answer refused") }
        }

        override fun reject() {
            val call = primary ?: return
            if (call.state != Call.STATE_RINGING) return
            runCatching { call.reject(false, null) }
                .onFailure { Log.w(TAG, "reject refused") }
        }

        override fun hangup() {
            runCatching { primary?.disconnect() }
                .onFailure { Log.w(TAG, "hangup refused") }
        }

        // Mute and the route are methods of InCallService, not of Call.
        override fun setMuted(muted: Boolean) {
            runCatching { this@CallService.setMuted(muted) }
                .onFailure { Log.w(TAG, "mute refused") }
        }

        override fun setSpeaker(speaker: Boolean) {
            val route =
                if (speaker) CallAudioState.ROUTE_SPEAKER else CallAudioState.ROUTE_EARPIECE
            runCatching { this@CallService.setAudioRoute(route) }
                .onFailure { Log.w(TAG, "audio route refused") }
        }

        override fun setHold(hold: Boolean) {
            val call = primary ?: return
            runCatching { if (hold) call.hold() else call.unhold() }
                .onFailure { Log.w(TAG, "hold change refused") }
        }

        override fun playDtmf(tone: Char) {
            if (tone !in DTMF_TONES) return
            val call = primary ?: return
            runCatching {
                call.playDtmfTone(tone)
                main.postDelayed({ runCatching { call.stopDtmfTone() } }, DTMF_TONE_MS)
            }.onFailure { Log.w(TAG, "tone refused") }
        }
    }

    /** The same two audio controls, for the remote-action executor. */
    private val audioControl: CallAudioControl = object : CallAudioControl {
        override fun setMuted(muted: Boolean) {
            runCatching { this@CallService.setMuted(muted) }
        }

        override fun setAudioRoute(route: Int) {
            runCatching { this@CallService.setAudioRoute(route) }
        }
    }

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------

    /**
     * Queues one state report.
     *
     * The post happens on a single background thread: the Telecom
     * callback returns at once, ordering is kept, and a server that is
     * down costs a timeout on that thread only.
     */
    private fun postEvent(current: CallSession) {
        val event = current.toEvent()
        val settings = SettingsStore(this)
        val token = settings.token
        val baseUri = settings.baseUri
        if (token.isBlank() || baseUri.isBlank()) return
        runCatching {
            events.execute {
                runCatching {
                    val client = clientFor(baseUri, token, settings)
                    client.postCallEvent(
                        callRef = event.callRef,
                        state = event.state,
                        direction = event.direction,
                        number = event.number.takeIf { it.isNotBlank() },
                        simLabel = event.simLabel,
                        muted = event.muted,
                        speaker = event.speaker,
                        startedAt = event.startedAt,
                        endedAt = event.endedAt
                    )
                }
            }
        }.onFailure { Log.w(TAG, "event not queued") }
    }

    /**
     * A client for a (base URL, token) pair.
     *
     * Reused, because every event would otherwise build a new OkHttp
     * pool. A changed URL or token builds a new one: the old client
     * belongs to a server the phone no longer talks to.
     */
    @Synchronized
    private fun clientFor(baseUri: String, token: String, settings: SettingsStore): RelayClient {
        val key = baseUri + "\u0000" + token
        val cached = cachedClient
        if (cached != null && cachedClientKey == key) return cached
        val created = RelayClient(baseUri, token) { settings.handleUnauthorized() }
        cachedClient = created
        cachedClientKey = key
        return created
    }

    // ------------------------------------------------------------------
    // Remote actions
    // ------------------------------------------------------------------

    private fun startPolling() {
        stopPolling()
        val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
        pollScope = scope
        pollJob = scope.launch {
            val settings = SettingsStore(this@CallService)
            while (isActive) {
                val current = session
                if (current == null || !current.isLive) break
                val token = settings.token
                val base = settings.baseUri
                if (token.isBlank() || base.isBlank()) {
                    // The phone is not paired: nothing to ask for. The
                    // local controls still work, which is the point.
                    delay(UNPAIRED_RETRY_MS)
                    continue
                }
                val client = clientFor(base, token, settings)
                val actions = try {
                    client.pollCallActions(POLL_WAIT_S)
                } catch (e: RuntimeException) {
                    emptyList()
                }
                if (actions.isEmpty()) {
                    delay(IDLE_RETRY_MS)
                    continue
                }
                for (action in actions) {
                    if (!isActive) break
                    handleAction(client, action)
                }
            }
            Log.i(TAG, "action poll stopped")
        }
    }

    private fun stopPolling() {
        runCatching { pollJob?.cancel() }
        runCatching { pollScope?.cancel() }
        pollJob = null
        pollScope = null
    }

    /**
     * Runs one action: policy, guard, Telecom, report.
     *
     * The order is the safety contract:
     *  - a stale, expired or invalid action is refused and never
     *    recorded, so it cannot burn the guard id of a real action;
     *  - the id is written to disk BEFORE the `Call` is touched;
     *  - an id that is already recorded is reported `done` and skipped,
     *    which is what makes a redelivery harmless.
     */
    private suspend fun handleAction(client: RelayClient, action: CallAction) {
        val now = System.currentTimeMillis() / 1000L
        val refusal = CallExecutor.refuse(action, session, now)
        if (refusal != null) {
            Log.i(TAG, "action refused: " + action.action + " (" + refusal.detail + ")")
            report(client, action.id, refusal)
            return
        }
        val claim = guard.claim(action.id)
        if (claim == CallActionGuard.Claim.DUPLICATE) {
            Log.i(TAG, "action already executed: " + action.action)
            report(client, action.id, ActionResult.Done)
            return
        }
        if (claim == CallActionGuard.Claim.UNAVAILABLE) {
            // The id could not be persisted, so "at most once" cannot be
            // promised: the safe answer is to refuse the action.
            Log.w(TAG, "action guard unavailable, refusing: " + action.action)
            report(client, action.id, ActionResult.Failed(DETAIL_GUARD))
            return
        }
        val result = performOnMain(action)
        Log.i(TAG, "action " + action.action + " -> " + result.status)
        report(client, action.id, result)
    }

    /**
     * Runs the Telecom part of an action on the main thread.
     *
     * The poll loop is on a background dispatcher; the `Call` methods are
     * binder calls that this app has always issued from the main thread,
     * which is where an OEM's in-call stack expects them. The answer is
     * carried back with a deferred instead of `Dispatchers.Main`, so the
     * worker does not depend on the Android main dispatcher being
     * installed.
     */
    private suspend fun performOnMain(action: CallAction): ActionResult {
        val answer = CompletableDeferred<ActionResult>()
        val posted = main.post {
            val result = try {
                CallExecutor.perform(applicationContext, primary, audioControl, action, main)
            } catch (e: RuntimeException) {
                ActionResult.Failed(CallExecutor.DETAIL_REFUSED)
            }
            answer.complete(result)
        }
        if (!posted) return ActionResult.Failed(CallExecutor.DETAIL_REFUSED)
        return answer.await()
    }

    /** A report is a network call: it never runs on a Telecom thread. */
    private fun report(client: RelayClient, id: String, result: ActionResult) {
        runCatching {
            events.execute {
                runCatching { client.reportCallAction(id, result.status, result.detail) }
            }
        }
    }

    // ------------------------------------------------------------------
    // UI and notification
    // ------------------------------------------------------------------

    /**
     * Opens the in-call screen and keeps the notification in step.
     *
     * The screen is the primary path. When the platform refuses to start
     * it (background-start restrictions, a locked work profile), the
     * notification still offers Answer and Decline: the call is never
     * only reachable from the server.
     */
    private fun openUi(current: CallSession) {
        // The screen is already up and bound to the registry: publishing
        // the session was enough, and a second startActivity would only
        // flicker it.
        if (LiveCallRegistry.uiVisible) {
            updateNotification(kindFor(current), current)
            return
        }
        val intent = Intent(this, CallActivity::class.java)
            .addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or
                    Intent.FLAG_ACTIVITY_SINGLE_TOP or
                    Intent.FLAG_ACTIVITY_NO_USER_ACTION
            )
        val shown = runCatching { startActivity(intent) }
            .onFailure { Log.w(TAG, "in-call screen not started") }
            .isSuccess
        if (!shown) Log.w(TAG, "falling back to the notification")
        updateNotification(kindFor(current), current)
    }

    private fun kindFor(current: CallSession): NotificationKind =
        if (current.state == CallStates.RINGING) NotificationKind.RINGING
        else NotificationKind.ACTIVE

    private fun createChannel() {
        runCatching {
            val manager = getSystemService(NotificationManager::class.java) ?: return
            val channel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.call_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = getString(R.string.call_channel_description)
                setShowBadge(false)
                enableVibration(false)
                setSound(null, null)
                lockscreenVisibility = Notification.VISIBILITY_PUBLIC
            }
            manager.createNotificationChannel(channel)
        }
    }

    private fun updateNotification(kind: NotificationKind, current: CallSession?) {
        val call = current ?: return
        if (!call.isLive) {
            runCatching { NotificationManagerCompat.from(this).cancel(NOTIFICATION_ID) }
            return
        }
        val incoming = call.direction == CallDirections.INCOMING
        val ringing = kind == NotificationKind.RINGING && incoming
        val shown = call.number.ifBlank { getString(R.string.call_private_number) }

        val title = when {
            kind == NotificationKind.RINGING && incoming -> R.string.call_incoming_title
            kind == NotificationKind.RINGING -> R.string.call_dialing_title
            else -> R.string.call_ongoing_title
        }

        // One PendingIntent per call: a stable request code derived from
        // the ref, so a re-post updates the same intent instead of
        // stacking a second one.
        val open = openCallIntent(call)

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_launcher)
            .setContentTitle(getString(title))
            .setContentText(shown)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setPriority(
                if (ringing) {
                    NotificationCompat.PRIORITY_MAX
                } else {
                    NotificationCompat.PRIORITY_HIGH
                }
            )
            .setOngoing(true)
            .setAutoCancel(false)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(open)
            .setOnlyAlertOnce(true)

        // A ringing call should own the screen even when the activity
        // could not be started from the service: that is what the
        // full-screen intent is for. Android 14 made it a user grant;
        // a default dialer is allowed, but the answer is checked rather
        // than assumed, and the actions below remain the fallback.
        if (ringing && canUseFullScreenIntent()) {
            builder.setFullScreenIntent(open, true)
        }

        if (ringing) {
            builder.addAction(
                0,
                getString(R.string.call_answer),
                actionIntent(ACTION_ANSWER, REQUEST_ANSWER)
            )
            builder.addAction(
                0,
                getString(R.string.call_decline),
                actionIntent(ACTION_DECLINE, REQUEST_DECLINE)
            )
        } else {
            builder.addAction(
                0,
                getString(R.string.call_hang_up),
                actionIntent(ACTION_HANGUP, REQUEST_HANGUP)
            )
        }

        try {
            NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, builder.build())
        } catch (e: SecurityException) {
            // Android 13+ without POST_NOTIFICATIONS: the screen is the
            // remaining path, and it is already open.
            Log.w(TAG, "notification not shown")
        }
    }

    private fun openCallIntent(current: CallSession): PendingIntent {
        val intent = Intent(this, CallActivity::class.java)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        // One stable request code per call, so a re-post replaces the
        // intent of that call and two ringing calls cannot share one.
        // The base sits above the action codes so the numbers cannot
        // collide even if a future action is added.
        val code = REQUEST_OPEN_BASE + (current.callRef.hashCode() and 0xFFF)
        return PendingIntent.getActivity(
            this,
            code,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    /**
     * True when a full-screen intent would actually be shown.
     *
     * Android 14 added the user grant. A default dialer passes it by
     * default; when the user revoked it, or on a vendor build that
     * refuses, the notification keeps its Answer/Decline buttons and the
     * call stays answerable - it just does not take over the screen.
     */
    private fun canUseFullScreenIntent(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return true
        return canUseFullScreenIntentApi34()
    }

    @RequiresApi(Build.VERSION_CODES.UPSIDE_DOWN_CAKE)
    private fun canUseFullScreenIntentApi34(): Boolean = runCatching {
        getSystemService(NotificationManager::class.java)?.canUseFullScreenIntent() ?: false
    }.getOrDefault(false)

    /**
     * A broadcast that the service's own receiver handles.
     *
     * See [registerActionReceiver]: the whole point is that the buttons
     * keep working when the platform refuses to start the screen. The
     * intent is explicit (package + action), so another app cannot forge
     * it, and no manifest component is needed.
     */
    private fun actionIntent(action: String, code: Int): PendingIntent {
        val intent = Intent(action).setPackage(packageName)
        return PendingIntent.getBroadcast(
            this,
            code,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    private enum class NotificationKind { RINGING, ACTIVE }

    companion object {
        private const val TAG = "CallService"

        /** The notification buttons of the in-call service. */
        private const val ACTION_ANSWER = "dev.smsrelay.calls.NOTIFY_ANSWER"
        private const val ACTION_DECLINE = "dev.smsrelay.calls.NOTIFY_DECLINE"
        private const val ACTION_HANGUP = "dev.smsrelay.calls.NOTIFY_HANGUP"

        private const val CHANNEL_ID = "calls"
        private const val NOTIFICATION_ID = 4101
        private const val REQUEST_OPEN_BASE = 4200
        private const val REQUEST_ANSWER = 4103
        private const val REQUEST_DECLINE = 4104
        private const val REQUEST_HANGUP = 4105

        /**
         * Long-poll window. Deliberately below the client's 15 s read
         * timeout: a poll that hit the timeout would look like an empty
         * answer and hide actions.
         */
        private const val POLL_WAIT_S = 10

        /** Pause between polls (and after an empty or failed one). */
        private const val IDLE_RETRY_MS = 1000L

        /** A phone that is not paired asks less often. */
        private const val UNPAIRED_RETRY_MS = 5000L

        /** How long a finished session stays published for the screen. */
        private const val CLEAR_SESSION_MS = 2000L

        private const val DTMF_TONE_MS = 150L

        /** Reported when the "at most once" guard could not be written. */
        private const val DETAIL_GUARD = "call action guard unavailable"

        private val DTMF_TONES = "0123456789*#".toSet()
    }
}
