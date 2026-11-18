package dev.smsrelay.net

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException

/** What the server answered to a pairing request. */
sealed class PairStartResult {
    /** The server handed out a code that waits for the admin. */
    data class Started(
        val pairingId: String,
        val scanCode: String,
        val qrPayload: String,
        val expiresAt: Long
    ) : PairStartResult()

    /** The server did not accept the request. */
    data class Rejected(val code: Int) : PairStartResult()

    /** The server could not be reached. */
    data class Unreachable(val reason: String) : PairStartResult()
}

/** What the server answered when the phone asks for the token. */
sealed class PairClaimResult {
    /** The admin has not approved the code yet. */
    object Pending : PairClaimResult()

    /** The admin approved the code: this is the only answer with a token. */
    data class Claimed(val token: String) : PairClaimResult()

    /**
     * The admin approved the pairing, but the server hands the token out
     * only after the user confirmed the pairing on this phone. The names
     * are the ones the server reported; nothing here is a secret.
     */
    data class ConfirmationRequired(
        val deviceName: String,
        val approverName: String,
        val approverUsername: String
    ) : PairClaimResult()

    /** The pairing was cancelled: there is no token to collect. */
    object Cancelled : PairClaimResult()

    /** The code was already collected by somebody else. */
    object AlreadyClaimed : PairClaimResult()

    /** The code ran out. */
    object Expired : PairClaimResult()

    /** The code is unknown to the server. */
    data class Invalid(val code: Int) : PairClaimResult()

    /** The server could not be reached. */
    data class Unreachable(val reason: String) : PairClaimResult()
}

/**
 * The two unauthenticated endpoints of the pairing.
 *
 * The phone asks for a code, shows it, and then waits until the admin
 * approves it in the mini app. The long token is handed over exactly
 * once, in the answer of the claim.
 *
 * The claim secret only ever travels in the body of these two requests.
 * It is never part of the QR code and never logged.
 */
class PairingClient(
    private val baseUri: String,
    private val client: OkHttpClient = RelayClient.defaultClient()
) {

    /**
     * Asks the server for a code.
     */
    fun start(
        deviceId: String,
        deviceName: String,
        appVersion: String,
        claimSecret: String
    ): PairStartResult {
        val payload = JSONObject().apply {
            put("device_id", deviceId)
            put("name", deviceName)
            put("platform", PLATFORM)
            put("app_version", appVersion)
            put("claim_secret", claimSecret)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("pair/start"))
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                if (!response.isSuccessful) {
                    return@use PairStartResult.Rejected(response.code)
                }
                val json = runCatching { JSONObject(text) }.getOrNull()
                    ?: return@use PairStartResult.Rejected(response.code)
                val pairingId = json.optString("pairing_id").trim()
                val scanCode = json.optString("scan_code").trim()
                val qrPayload = json.optString("qr_payload").trim()
                val expiresAt = json.optLong("expires_at", 0L)
                if (pairingId.isEmpty() || scanCode.isEmpty() || qrPayload.isEmpty()) {
                    PairStartResult.Rejected(response.code)
                } else {
                    PairStartResult.Started(pairingId, scanCode, qrPayload, expiresAt)
                }
            }
        } catch (e: IOException) {
            PairStartResult.Unreachable(e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            PairStartResult.Unreachable(e.javaClass.simpleName)
        }
    }

    /**
     * Asks the server whether the admin approved the code yet.
     *
     * [confirmed] is null while the phone is still waiting and when it
     * merely polls. It is true only when the user confirmed the pairing
     * on this phone, and false when the user turned it down.
     *
     * The field is always sent, so a poll carries an explicit
     * `"confirmed": null`. The server answers that with a pending or an
     * awaiting-confirmation status and never with a token; it only hands
     * the token out for `confirmed: true`. Older servers ignore the
     * unknown field and keep their immediate-token behaviour.
     */
    fun claim(
        pairingId: String,
        claimSecret: String,
        confirmed: Boolean? = null
    ): PairClaimResult {
        val payload = JSONObject().apply {
            put("pairing_id", pairingId)
            put("claim_secret", claimSecret)
            put("confirmed", confirmed ?: JSONObject.NULL)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("pair/claim"))
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(text) }.getOrNull()
                when {
                    response.code == 202 -> {
                        // Pending, or approved and waiting for the user to
                        // confirm on this phone. The names in that answer
                        // are shown in the dialog; the token is not there.
                        if (json?.optString("status").orEmpty().trim() ==
                            STATUS_AWAITING_CONFIRMATION
                        ) {
                            val approver = json?.optJSONObject("approved_by")
                            PairClaimResult.ConfirmationRequired(
                                deviceName = json?.optJSONObject("device")
                                    ?.optString("name").orEmpty().trim(),
                                approverName = approver?.optString("name")
                                    .orEmpty().trim(),
                                approverUsername = approver?.optString("username")
                                    .orEmpty().trim()
                            )
                        } else {
                            PairClaimResult.Pending
                        }
                    }

                    response.code == 409 -> PairClaimResult.AlreadyClaimed
                    response.code == 410 -> PairClaimResult.Expired
                    response.code == 401 || response.code == 403 || response.code == 404 ->
                        PairClaimResult.Invalid(response.code)

                    response.isSuccessful -> {
                        if (json?.optString("status").orEmpty().trim() ==
                            STATUS_CANCELLED
                        ) {
                            return@use PairClaimResult.Cancelled
                        }
                        val token = json?.optString("token").orEmpty().trim()
                        if (token.isEmpty()) {
                            PairClaimResult.Invalid(response.code)
                        } else {
                            PairClaimResult.Claimed(token)
                        }
                    }

                    else -> PairClaimResult.Unreachable("http_${response.code}")
                }
            }
        } catch (e: IOException) {
            PairClaimResult.Unreachable(e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            PairClaimResult.Unreachable(e.javaClass.simpleName)
        }
    }

    private fun endpoint(path: String): String =
        baseUri.trimEnd('/') + "/" + path.trimStart('/')

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()

        private const val PLATFORM = "android"

        /** The server approved the pairing and wants the phone to confirm. */
        private const val STATUS_AWAITING_CONFIRMATION = "awaiting_phone_confirmation"

        /** The pairing was cancelled on the server. */
        private const val STATUS_CANCELLED = "cancelled"
    }
}
