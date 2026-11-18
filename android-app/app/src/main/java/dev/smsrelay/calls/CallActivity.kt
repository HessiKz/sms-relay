package dev.smsrelay.calls

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.database.Cursor
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.ContactsContract
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import dev.smsrelay.R
import dev.smsrelay.util.PhoneNumbers
import java.util.concurrent.Executors

/**
 * The in-call screen of the default dialer.
 *
 * It shows the state of the one primary call and offers the controls the
 * user expects: Answer, Decline, Hang up, Mute, Speaker, Hold and a
 * keypad for DTMF. It is plain views on purpose - an incoming call is
 * the worst moment to inflate a Compose tree - and it must work with no
 * pairing, no network and no backend: every control calls [CallService]
 * in this process, never the server.
 *
 * The screen never answers or rejects a call by itself: it only asks the
 * service, which owns the Telecom `Call`. If the service is gone, the
 * buttons are inert and the platform UI keeps the call.
 */
class CallActivity : AppCompatActivity() {

    private lateinit var textState: TextView
    private lateinit var textName: TextView
    private lateinit var textNumber: TextView
    private lateinit var textSim: TextView
    private lateinit var textTimer: TextView
    private lateinit var buttonAnswer: Button
    private lateinit var buttonDecline: Button
    private lateinit var buttonHangup: Button
    private lateinit var buttonMute: Button
    private lateinit var buttonSpeaker: Button
    private lateinit var buttonHold: Button
    private lateinit var buttonKeypad: Button
    private lateinit var layoutKeypad: LinearLayout
    private lateinit var textKeypad: TextView

    private val main = Handler(Looper.getMainLooper())

    /** Digits sent from the keypad, shown in a line and cleared on close. */
    private val keypadDigits = StringBuilder()

    /** A contact name resolved once per call; null while unknown. */
    @Volatile
    private var resolvedName: String? = null

    /** The ref the name was looked up for, so a new call re-looks. */
    private var namedRef: String? = null

    private val contactLookup = Executors.newSingleThreadExecutor { runnable ->
        Thread(runnable, "call-contact").apply { isDaemon = true }
    }

    /** Ticks the elapsed timer while the call is up. */
    private val ticker = object : Runnable {
        override fun run() {
            renderTimer()
            if (LiveCallRegistry.session?.isLive == true) {
                main.postDelayed(this, TICK_MS)
            }
        }
    }

    /** Re-read every label whenever the call changes. */
    private val listener = LiveCallRegistry.Listener { current ->
        if (isFinishing || isDestroyed) return@Listener
        render(current)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // A ringing call must be usable on a locked phone, and the screen
        // has to light up: this is the whole point of an in-call UI.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
            )
        }
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        setContentView(R.layout.activity_call)

        textState = findViewById(R.id.textCallState)
        textName = findViewById(R.id.textCallName)
        textNumber = findViewById(R.id.textCallNumber)
        textSim = findViewById(R.id.textCallSim)
        textTimer = findViewById(R.id.textCallTimer)
        buttonAnswer = findViewById(R.id.buttonCallAnswer)
        buttonDecline = findViewById(R.id.buttonCallDecline)
        buttonHangup = findViewById(R.id.buttonCallHangup)
        buttonMute = findViewById(R.id.buttonCallMute)
        buttonSpeaker = findViewById(R.id.buttonCallSpeaker)
        buttonHold = findViewById(R.id.buttonCallHold)
        buttonKeypad = findViewById(R.id.buttonCallKeypad)
        layoutKeypad = findViewById(R.id.layoutCallKeypad)
        textKeypad = findViewById(R.id.textCallKeypadDigits)

        bindButtons()

        LiveCallRegistry.addListener(listener)
        LiveCallRegistry.uiVisible = true

        // A notification action (Answer/Decline/Hang up) arrives as an
        // extra: the screen applies it and the service acts.
        handleIntent(intent)
        render(LiveCallRegistry.session)
        main.post(ticker)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        LiveCallRegistry.uiVisible = true
        // A null or finished session is a normal case: the notification
        // was tapped after the call ended. render() shows "Call ended"
        // and closes the screen shortly after; nothing here dereferences
        // the session.
        render(LiveCallRegistry.session)
    }

    override fun onDestroy() {
        LiveCallRegistry.removeListener(listener)
        LiveCallRegistry.uiVisible = false
        main.removeCallbacks(ticker)
        contactLookup.shutdownNow()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // Controls
    // ------------------------------------------------------------------

    private fun bindButtons() {
        buttonAnswer.setOnClickListener { LiveCallRegistry.handle?.answer() }
        buttonDecline.setOnClickListener {
            // A decline ends the screen at once: a ringing call that is
            // being rejected must not keep a full-screen window up.
            LiveCallRegistry.handle?.reject()
            finishSoon()
        }
        buttonHangup.setOnClickListener {
            LiveCallRegistry.handle?.hangup()
            finishSoon()
        }
        buttonMute.setOnClickListener {
            val current = LiveCallRegistry.session ?: return@setOnClickListener
            LiveCallRegistry.handle?.setMuted(!current.muted)
        }
        buttonSpeaker.setOnClickListener {
            val current = LiveCallRegistry.session ?: return@setOnClickListener
            LiveCallRegistry.handle?.setSpeaker(!current.speaker)
        }
        buttonHold.setOnClickListener {
            val current = LiveCallRegistry.session ?: return@setOnClickListener
            LiveCallRegistry.handle?.setHold(current.state != CallStates.HELD)
        }
        buttonKeypad.setOnClickListener {
            val show = layoutKeypad.visibility != View.VISIBLE
            layoutKeypad.visibility = if (show) View.VISIBLE else View.GONE
            if (!show) {
                keypadDigits.setLength(0)
                textKeypad.text = ""
            }
        }
        val tones = mapOf(
            R.id.buttonDtmf0 to '0', R.id.buttonDtmf1 to '1', R.id.buttonDtmf2 to '2',
            R.id.buttonDtmf3 to '3', R.id.buttonDtmf4 to '4', R.id.buttonDtmf5 to '5',
            R.id.buttonDtmf6 to '6', R.id.buttonDtmf7 to '7', R.id.buttonDtmf8 to '8',
            R.id.buttonDtmf9 to '9', R.id.buttonDtmfStar to '*', R.id.buttonDtmfHash to '#'
        )
        for ((id, tone) in tones) {
            findViewById<Button>(id).setOnClickListener {
                // A tone is only meaningful on an up call; the service
                // ignores it otherwise as well.
                LiveCallRegistry.handle?.playDtmf(tone)
                if (keypadDigits.length < MAX_KEYPAD_DIGITS) {
                    keypadDigits.append(tone)
                    textKeypad.text = keypadDigits.toString()
                }
            }
        }
    }

    private fun handleIntent(intent: Intent?) {
        if (intent == null) return
        // The notification buttons do not come through here: they are
        // broadcasts to the service, so they still work when this screen
        // cannot start. The extras are kept for a future deep link.
        when {
            intent.getBooleanExtra(EXTRA_ANSWER, false) -> {
                intent.removeExtra(EXTRA_ANSWER)
                LiveCallRegistry.handle?.answer()
            }

            intent.getBooleanExtra(EXTRA_DECLINE, false) -> {
                intent.removeExtra(EXTRA_DECLINE)
                LiveCallRegistry.handle?.reject()
                finishSoon()
            }

            intent.getBooleanExtra(EXTRA_HANGUP, false) -> {
                intent.removeExtra(EXTRA_HANGUP)
                LiveCallRegistry.handle?.hangup()
                finishSoon()
            }
        }
    }

    /**
     * Closes the screen shortly after a local end.
     *
     * The delay is short and bounded: the user must not be stuck looking
     * at a dead screen if the network never confirms the disconnect.
     */
    private fun finishSoon() {
        main.postDelayed({ if (!isFinishing) finish() }, FINISH_DELAY_MS)
    }

    // ------------------------------------------------------------------
    // Rendering
    // ------------------------------------------------------------------

    private fun render(current: CallSession?) {
        if (current == null || !current.isLive) {
            textState.text = getString(R.string.call_ended)
            textTimer.text = ""
            showRingingControls(false)
            showActiveControls(false)
            finishSoon()
            return
        }

        val incoming = current.direction == CallDirections.INCOMING
        val ringing = current.state == CallStates.RINGING
        val held = current.state == CallStates.HELD

        textState.text = when {
            ringing && incoming -> getString(R.string.call_incoming_title)
            ringing -> getString(R.string.call_dialing_title)
            held -> getString(R.string.call_held_title)
            current.state == CallStates.ACTIVE -> getString(R.string.call_ongoing_title)
            else -> getString(R.string.call_dialing_title)
        }

        // The number is never hidden from its owner; it is simply blank
        // for a withheld caller.
        val number = current.number
        textNumber.text = if (number.isBlank()) {
            getString(R.string.call_private_number)
        } else if (isShortCode(number)) {
            number
        } else {
            format(number)
        }

        // The name, when the phone's own contact list knows it.
        val name = resolvedName
        if (name.isNullOrBlank()) {
            textName.visibility = View.GONE
            if (namedRef != current.callRef) {
                // Marked before the query, so a lookup that finds nothing
                // is not repeated on every tick.
                namedRef = current.callRef
                resolvedName = null
                if (number.isNotBlank()) lookupContact(current.callRef, number)
            }
        } else {
            textName.text = name
            textName.visibility = View.VISIBLE
        }

        textSim.text = current.simLabel
            ?.takeIf { it.isNotBlank() }
            ?.let { getString(R.string.call_on_sim, it) }
            .orEmpty()
        textSim.visibility = if (textSim.text.isNullOrBlank()) View.GONE else View.VISIBLE

        showRingingControls(ringing && incoming)
        showActiveControls(!ringing)
        // An outgoing call that is still dialling also needs a way out:
        // the platform reports `disconnected` only after the network
        // gives up, which can take half a minute.
        if (ringing && !incoming) {
            buttonHangup.visibility = View.VISIBLE
        }

        buttonMute.text = getString(
            if (current.muted) R.string.call_mute_on else R.string.call_mute
        )
        buttonSpeaker.text = getString(
            if (current.speaker) R.string.call_speaker_on else R.string.call_speaker
        )
        buttonHold.text = getString(
            if (held) R.string.call_unhold else R.string.call_hold
        )
        // Hold and the keypad are meaningless while the call is not up.
        buttonHold.isEnabled = !ringing
        buttonKeypad.isEnabled = !ringing
        if (ringing) {
            layoutKeypad.visibility = View.GONE
            keypadDigits.setLength(0)
            textKeypad.text = ""
        }

        renderTimer()
    }

    private fun showRingingControls(visible: Boolean) {
        buttonAnswer.visibility = if (visible) View.VISIBLE else View.GONE
        buttonDecline.visibility = if (visible) View.VISIBLE else View.GONE
    }

    private fun showActiveControls(visible: Boolean) {
        val flag = if (visible) View.VISIBLE else View.GONE
        buttonMute.visibility = flag
        buttonSpeaker.visibility = flag
        buttonHold.visibility = flag
        buttonKeypad.visibility = flag
        if (visible) {
            // A call that is up always needs a way to end it.
            buttonHangup.visibility = View.VISIBLE
        } else {
            buttonHangup.visibility = View.GONE
            layoutKeypad.visibility = View.GONE
        }
    }

    /** The elapsed time since the call was answered. */
    private fun renderTimer() {
        val current = LiveCallRegistry.session
        if (current == null) {
            textTimer.text = ""
            return
        }
        if (!current.isLive || current.state == CallStates.RINGING) {
            textTimer.text = ""
            return
        }
        val elapsed = System.currentTimeMillis() - current.countingSince * 1000L
        textTimer.text = formatElapsed(elapsed)
    }

    private fun formatElapsed(millis: Long): String {
        val total = (millis / 1000L).coerceAtLeast(0L)
        val hours = total / 3600L
        val minutes = (total % 3600L) / 60L
        val seconds = total % 60L
        return if (hours > 0) {
            String.format(LocaleHolder.LOCALE, "%d:%02d:%02d", hours, minutes, seconds)
        } else {
            String.format(LocaleHolder.LOCALE, "%d:%02d", minutes, seconds)
        }
    }

    /** Grouping for display only; the call target is unchanged. */
    private fun format(text: String): String {
        if (text.length < DISPLAY_GROUP_MIN) return text
        val builder = StringBuilder()
        var index = 0
        for (character in text) {
            if (index == 3 || (index > 3 && (index - 3) % 3 == 0 && text.length > 4)) {
                builder.append(' ')
            }
            builder.append(character)
            index += 1
        }
        return builder.toString()
    }

    private fun isShortCode(text: String): Boolean {
        val digits = text.filter { it.isDigit() }
        return digits.isNotEmpty() && digits.length <= SHORT_CODE_MAX
    }

    /**
     * Looks the number up in the phone's own contact list.
     *
     * `READ_CONTACTS` is optional: without it the screen simply shows the
     * number. The lookup runs on a short-lived thread (a provider query
     * must not block the frame that has to light up the screen) and its
     * answer is applied only if the same call is still on screen. Names
     * are never logged.
     */
    private fun lookupContact(callRef: String, number: String) {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CONTACTS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val query = PhoneNumbers.normalize(number) ?: number
        runCatching {
            contactLookup.execute {
                val name = runCatching { queryName(query) }.getOrNull()
                if (name.isNullOrBlank()) return@execute
                main.post {
                    if (isFinishing || isDestroyed) return@post
                    if (LiveCallRegistry.session?.callRef != callRef) return@post
                    resolvedName = name
                    textName.text = name
                    textName.visibility = View.VISIBLE
                }
            }
        }
    }

    private fun queryName(number: String): String? {
        val uri = android.net.Uri.withAppendedPath(
            ContactsContract.PhoneLookup.CONTENT_FILTER_URI,
            android.net.Uri.encode(number)
        )
        val projection = arrayOf(ContactsContract.PhoneLookup.DISPLAY_NAME)
        var cursor: Cursor? = null
        return try {
            cursor = contentResolver.query(uri, projection, null, null, null)
            if (cursor != null && cursor.moveToFirst()) {
                cursor.getString(0)?.trim()?.takeIf { it.isNotEmpty() }
            } else {
                null
            }
        } finally {
            runCatching { cursor?.close() }
        }
    }

    /** Locale for the timer; kept in one place so it never changes shape. */
    private object LocaleHolder {
        val LOCALE: java.util.Locale = java.util.Locale.getDefault()
    }

    companion object {
        /** Opened from a notification: answer the ringing call. */
        const val EXTRA_ANSWER = "dev.smsrelay.calls.extra.ANSWER"

        /** Opened from a notification: decline the ringing call. */
        const val EXTRA_DECLINE = "dev.smsrelay.calls.extra.DECLINE"

        /** Opened from a notification: end an up call. */
        const val EXTRA_HANGUP = "dev.smsrelay.calls.extra.HANGUP"

        private const val TICK_MS = 1000L
        private const val FINISH_DELAY_MS = 1200L
        private const val MAX_KEYPAD_DIGITS = 24
        private const val DISPLAY_GROUP_MIN = 7
        private const val SHORT_CODE_MAX = 6
    }
}
