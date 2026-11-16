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
     */
    fun claim(pairingId: String, claimSecret: String): PairClaimResult {
        val payload = JSONObject().apply {
            put("pairing_id", pairingId)
            put("claim_secret", claimSecret)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("pair/claim"))
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                when {
                    response.code == 202 -> PairClaimResult.Pending
                    response.code == 409 -> PairClaimResult.AlreadyClaimed
                    response.code == 410 -> PairClaimResult.Expired
                    response.code == 401 || response.code == 403 || response.code == 404 ->
                        PairClaimResult.Invalid(response.code)

                    response.isSuccessful -> {
                        val json = runCatching { JSONObject(text) }.getOrNull()
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
    }
}
