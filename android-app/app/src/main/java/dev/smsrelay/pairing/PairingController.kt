package dev.smsrelay.pairing

import android.content.Context
import android.os.Handler
import android.os.Looper
import dev.smsrelay.BuildConfig
import dev.smsrelay.SettingsStore
import dev.smsrelay.net.PairClaimResult
import dev.smsrelay.net.PairStartResult
import dev.smsrelay.net.PairingClient
import dev.smsrelay.net.RelayClient
import java.security.SecureRandom
import java.util.Base64
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

/**
 * The pairing of this phone with the relay backend.
 *
 * The phone asks the server for a short code, shows it as a QR code and
 * waits until the admin approves it in the mini app. The long token is
 * handed over exactly once, in the answer of the claim, and is written
 * into the encrypted store right away.
 *
 * The state of a running pairing lives in the settings, so the app can
 * pick it up again after it was killed. The claim secret lives in the
 * encrypted store with the token.
 *
 * Nothing here is logged: the code, the payload and the token are
 * secrets of this phone.
 */
class PairingController(
    private val context: Context,
    private val listener: Listener
) {

    /** What the screen has to show. */
    interface Listener {
        /** The phone waits for the admin: the code is on the screen. */
        fun onPairingStarted(state: State)

        /**
         * The admin approved the pairing, and the server hands the
         * token out only after the user confirmed it on this phone.
         *
         * [respond] is called exactly once with the answer of the user.
         * When the screen cannot show the dialog it has to call
         * `respond(false)`: a pairing is never confirmed silently.
         */
        fun onPairingConfirmationRequired(
            confirmation: Confirmation,
            respond: (Boolean) -> Unit
        )

        /** The device was paired: the token is stored. */
        fun onPaired(deviceName: String)

        /** The pairing did not work out. */
        fun onPairingFailed(reason: Reason)
    }

    /** Why a pairing did not work out. */
    enum class Reason {
        /** The server could not be reached. */
        Network,

        /** The server did not accept the request. */
        Server,

        /** The code was already collected. */
        AlreadyClaimed,

        /** The code ran out. */
        Expired,

        /** The code is unknown to the server. */
        Invalid,

        /** The user did not confirm the pairing on this phone. */
        Cancelled
    }

    /**
     * What the dialog on the phone says. Only names and the server URL:
     * the token and the claim secret are never part of it.
     */
    data class Confirmation(
        val deviceName: String,
        val serverUrl: String,
        val approverName: String,
        val approverUsername: String
    )

    /** The visible part of a running pairing. */
    data class State(
        val deviceName: String,
        val scanCode: String,
        val qrPayload: String,
        val expiresAt: Long
    )

    private val main = Handler(Looper.getMainLooper())
    private val random = SecureRandom()

    /** True while a pairing is running. */
    private val running = AtomicBoolean(false)

    /**
     * Counts the pairings. A thread that belongs to an older pairing
     * does nothing any more: its answer is stale.
     */
    private val generation = AtomicInteger(0)

    /**
     * Asks the server for a code and starts waiting for the admin.
     */
    fun start(deviceName: String) {
        val mine = generation.incrementAndGet()
        running.set(true)

        val settings = SettingsStore(context)
        val name = deviceName.trim()
        settings.deviceName = name
        settings.pairingState = SettingsStore.STATE_PAIRING

        val secret = newClaimSecret()
        settings.claimSecret = secret

        // One HTTP client for the whole pairing: a new one for every
        // poll would open a new pool of connections every two seconds.
        val pairingClient = PairingClient(settings.baseUri, RelayClient.defaultClient())

        Thread {
            val result = pairingClient.start(
                deviceId = settings.deviceId,
                deviceName = name,
                appVersion = BuildConfig.VERSION_NAME,
                claimSecret = secret
            )
            if (mine != generation.get()) return@Thread
            when (result) {
                is PairStartResult.Started -> {
                    settings.pairingId = result.pairingId
                    settings.pairingScanCode = result.scanCode
                    settings.pairingQrPayload = result.qrPayload
                    settings.pairingExpiresAt = expiryOf(result.expiresAt)
                    publish(
                        State(
                            deviceName = name,
                            scanCode = result.scanCode,
                            qrPayload = result.qrPayload,
                            expiresAt = settings.pairingExpiresAt
                        )
                    )
                    poll(settings, mine, pairingClient)
                }

                is PairStartResult.Rejected -> fail(Reason.Server, mine)
                is PairStartResult.Unreachable -> fail(Reason.Network, mine)
            }
        }.start()
    }

    /**
     * Picks a running pairing up again, e.g. after the app was killed.
     */
    fun resume() {
        val settings = SettingsStore(context)
        if (settings.pairingState != SettingsStore.STATE_PAIRING) return
        val pairingId = settings.pairingId
        val secret = settings.claimSecret
        if (pairingId.isBlank() || secret.isBlank()) {
            settings.clearPairing()
            settings.pairingState = SettingsStore.STATE_UNPAIRED
            return
        }
        val expiresAt = settings.pairingExpiresAt
        if (expiresAt > 0 && nowSeconds() > expiresAt) {
            settings.clearPairing()
            settings.pairingState = SettingsStore.STATE_UNPAIRED
            main.post { listener.onPairingFailed(Reason.Expired) }
            return
        }
        val mine = generation.incrementAndGet()
        running.set(true)
        publish(
            State(
                deviceName = settings.deviceName,
                scanCode = settings.pairingScanCode,
                qrPayload = settings.pairingQrPayload,
                expiresAt = expiresAt
            )
        )
        val pairingClient = PairingClient(settings.baseUri, RelayClient.defaultClient())
        Thread { poll(settings, mine, pairingClient) }.start()
    }

    /**
     * Stops the waiting. The pairing itself is not thrown away: the
     * app picks it up again the next time it is opened.
     */
    fun stop() {
        generation.incrementAndGet()
        running.set(false)
    }

    /**
     * Waits for the admin.
     *
     * Every two seconds the server is asked whether the code was
     * approved. A network error makes the wait longer, never shorter.
     */
    private fun poll(settings: SettingsStore, mine: Int, pairingClient: PairingClient) {
        var wait = POLL_INTERVAL_MS
        while (running.get() && mine == generation.get()) {
            val pairingId = settings.pairingId
            val secret = settings.claimSecret
            if (pairingId.isBlank() || secret.isBlank()) {
                fail(Reason.Invalid, mine)
                return
            }
            val expiresAt = settings.pairingExpiresAt
            if (expiresAt > 0 && nowSeconds() > expiresAt) {
                fail(Reason.Expired, mine)
                return
            }
            when (val result = pairingClient.claim(pairingId, secret)) {
                is PairClaimResult.Claimed -> {
                    succeed(settings, result.token, mine)
                    return
                }

                is PairClaimResult.ConfirmationRequired -> {
                    // The admin approved, and the server wants the user
                    // to confirm on this phone. The wait ends here: the
                    // answer of the user decides, and it is asked once.
                    askConfirmation(settings, pairingId, secret, result, mine, pairingClient)
                    return
                }

                PairClaimResult.Pending -> wait = POLL_INTERVAL_MS

                PairClaimResult.Cancelled -> {
                    fail(Reason.Cancelled, mine)
                    return
                }

                PairClaimResult.AlreadyClaimed -> {
                    fail(Reason.AlreadyClaimed, mine)
                    return
                }

                PairClaimResult.Expired -> {
                    fail(Reason.Expired, mine)
                    return
                }

                is PairClaimResult.Invalid -> {
                    fail(Reason.Invalid, mine)
                    return
                }

                is PairClaimResult.Unreachable -> {
                    // The network is down: wait longer before the next try.
                    wait = (wait * 2).coerceAtMost(MAX_WAIT_MS)
                }
            }
            if (!sleep(wait)) return
        }
    }

    /**
     * Asks the user to confirm the pairing on this phone.
     *
     * The dialog names the server and the phone and who approved the
     * pairing. Only the answer of the user collects the token: a screen
     * that cannot show the dialog answers "no", so a pairing is never
     * confirmed without the user.
     */
    private fun askConfirmation(
        settings: SettingsStore,
        pairingId: String,
        secret: String,
        result: PairClaimResult.ConfirmationRequired,
        mine: Int,
        pairingClient: PairingClient
    ) {
        val confirmation = Confirmation(
            deviceName = result.deviceName.ifBlank { settings.deviceName },
            serverUrl = settings.baseUri,
            approverName = result.approverName,
            approverUsername = result.approverUsername
        )
        val answered = AtomicBoolean(false)
        main.post {
            if (mine != generation.get()) {
                // A newer pairing took over: this dialog is not shown and
                // the pairing it belonged to is not cancelled either - it
                // was superseded, not refused.
                return@post
            }
            if (!running.get()) {
                // The screen is gone before the question could be shown.
                // A pairing is never confirmed silently: it is cancelled,
                // and the call to the server runs off the main thread.
                if (answered.compareAndSet(false, true)) {
                    Thread { sendCancellation(pairingId, secret, mine, pairingClient) }.start()
                }
                return@post
            }
            listener.onPairingConfirmationRequired(confirmation) { confirmed ->
                if (!answered.compareAndSet(false, true)) return@onPairingConfirmationRequired
                Thread {
                    if (confirmed) {
                        confirmAndCollect(settings, pairingId, secret, mine, pairingClient)
                    } else {
                        sendCancellation(pairingId, secret, mine, pairingClient)
                    }
                }.start()
            }
        }
    }

    /**
     * The user confirmed: the token is collected with `confirmed:true`.
     * This is the only place a token is written, and it keeps the
     * generation check of [succeed]: a stale answer from a pairing that
     * is not on the screen any more writes nothing.
     */
    private fun confirmAndCollect(
        settings: SettingsStore,
        pairingId: String,
        secret: String,
        mine: Int,
        pairingClient: PairingClient
    ) {
        if (mine != generation.get() || !running.get()) {
            // The pairing this dialog belonged to is over, so no token is
            // collected. The server is not told "no" here: the same
            // pairing may have been picked up again, and a stale answer
            // must never cancel the code that is on the screen now.
            return
        }
        when (val result = pairingClient.claim(pairingId, secret, confirmed = true)) {
            is PairClaimResult.Claimed -> succeed(settings, result.token, mine)
            PairClaimResult.Cancelled -> fail(Reason.Cancelled, mine)
            PairClaimResult.Expired -> fail(Reason.Expired, mine)
            PairClaimResult.AlreadyClaimed -> fail(Reason.AlreadyClaimed, mine)
            is PairClaimResult.Invalid -> fail(Reason.Invalid, mine)
            is PairClaimResult.Unreachable -> fail(Reason.Network, mine)
            // The server still wants a confirmation, or answers something
            // unexpected: this pairing did not happen.
            else -> fail(Reason.Server, mine)
        }
    }

    /**
     * The user turned the pairing down, or the dialog could not be shown.
     *
     * The local pairing is reset only when it is still this very pairing:
     * a code that a newer pairing put on the screen keeps its secret. The
     * screen is told only when this pairing is still the current one. The
     * server is told in every case, so the pairing cannot be collected
     * later; a failure here changes nothing, the phone holds no token.
     */
    private fun sendCancellation(
        pairingId: String,
        secret: String,
        mine: Int,
        pairingClient: PairingClient
    ) {
        val settings = SettingsStore(context)
        // Only the pairing this answer belongs to is cleared: a newer
        // pairing has its own id and keeps its code and secret.
        if (settings.pairingId == pairingId) {
            settings.clearPairing()
            settings.pairingState = SettingsStore.STATE_UNPAIRED
        }
        if (mine == generation.get()) {
            // The user said no: every thread of this pairing is stale now,
            // so a late answer can never write a token.
            generation.incrementAndGet()
            running.set(false)
            main.post { listener.onPairingFailed(Reason.Cancelled) }
        }
        // The server is told as well, so the pairing cannot be collected
        // later. A failure here changes nothing: the phone has no token.
        runCatching { pairingClient.claim(pairingId, secret, confirmed = false) }
    }

    /** The token is here: it is stored and the workers are started again. */
    private fun succeed(settings: SettingsStore, token: String, mine: Int) {
        // A newer pairing took over while the answer was on its way:
        // the answer belongs to a code that is not on the screen any more.
        if (mine != generation.get()) return
        running.set(false)
        settings.token = token
        if (!settings.hasToken()) {
            // The encrypted store would not take the token (the key of
            // the phone is gone): the pairing did not happen.
            fail(Reason.Server, mine)
            return
        }
        settings.pairedDeviceName = settings.deviceName
        settings.pairedBaseUri = settings.baseUri
        settings.clearPairing()
        settings.pairingState = SettingsStore.STATE_PAIRED
        val name = settings.pairedDeviceName
        main.post { listener.onPaired(name) }
    }

    /** The pairing is over without a token. */
    private fun fail(reason: Reason, mine: Int) {
        // A newer pairing took over: this answer is stale and must not
        // clear the code that is on the screen now.
        if (mine != generation.get()) return
        running.set(false)
        val settings = SettingsStore(context)
        settings.clearPairing()
        settings.pairingState = SettingsStore.STATE_UNPAIRED
        main.post { listener.onPairingFailed(reason) }
    }

    private fun publish(state: State) {
        main.post { listener.onPairingStarted(state) }
    }

    /**
     * A fresh claim secret: 32 random bytes, written down in the way
     * that is safe inside a URL and inside JSON.
     */
    private fun newClaimSecret(): String {
        val bytes = ByteArray(SECRET_BYTES)
        random.nextBytes(bytes)
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
    }

    /**
     * When the code runs out. A server that does not say when its code
     * runs out gets the usual five minutes.
     */
    private fun expiryOf(expiresAt: Long): Long =
        if (expiresAt > 0) expiresAt else nowSeconds() + DEFAULT_TTL_SECONDS

    /** Waits. Returns false when the thread was interrupted. */
    private fun sleep(millis: Long): Boolean = try {
        Thread.sleep(millis)
        true
    } catch (e: InterruptedException) {
        Thread.currentThread().interrupt()
        false
    }

    private fun nowSeconds(): Long = System.currentTimeMillis() / 1000L

    companion object {
        /** How often the server is asked whether the code was approved. */
        private const val POLL_INTERVAL_MS = 2000L

        /** The longest wait after a network error. */
        private const val MAX_WAIT_MS = 30000L

        /** The size of the claim secret: 32 random bytes. */
        private const val SECRET_BYTES = 32

        /** How long a code is valid when the server does not say. */
        private const val DEFAULT_TTL_SECONDS = 300L
    }
}
