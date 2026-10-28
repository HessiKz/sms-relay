package dev.smsrelay.net

import dev.smsrelay.data.Contact
import dev.smsrelay.data.MessageRecord
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

sealed class ApiResult {
    data class Accepted(val duplicate: Boolean, val state: String?) : ApiResult()
    object Unauthorized : ApiResult()
    /** Permanent rejection of THIS message (4xx other than 401/403/429). */
    data class Rejected(val code: Int, val reason: String?) : ApiResult()
    /** Transient server-side condition (429, 5xx): retry later. */
    data class Retryable(val code: Int, val reason: String?) : ApiResult()
    data class Unreachable(val reason: String) : ApiResult()
}

/** Backend view of one message, as returned by GET /sms-state. */
data class BackendState(
    val state: String,
    val attempts: Int,
    val lastError: String? = null
)

/**
 * One SMS command that the phone should send with its own SIM, as returned
 * by GET /relay/commands.
 */
data class RelayCommand(
    val id: String,
    val target: String,
    val body: String,
    val source: String,
    val createdAt: Long,
    /** The card the sender chose; 0 means: use the app's own rule. */
    val subId: Int = 0
)

/** One row of the phone's SMS inbox on its way to the server. */
data class ImportMessage(
    val providerId: Long,
    val sender: String,
    val body: String,
    val receivedAt: Long,
    val subId: Int
)

/** Outcome of one import batch. */
sealed class ImportResult {
    data class Accepted(
        val imported: Int,
        val duplicates: Int,
        val skipped: Int
    ) : ImportResult()

    object Unauthorized : ImportResult()
    data class Rejected(val code: Int) : ImportResult()
    data class Retryable(val code: Int) : ImportResult()
    data class Unreachable(val reason: String) : ImportResult()
}

/**
 * Thin HTTP client for the relay backend. It never logs message bodies;
 * failures are reported as categories only.
 *
 * Every request is built inside a try block: a server URL that the user
 * emptied in the settings must not crash the app.
 */
class RelayClient(
    private val baseUri: String,
    private val token: String,
    private val client: OkHttpClient = defaultClient()
) {

    fun upload(record: MessageRecord): ApiResult {
        val payload = JSONObject().apply {
            put("id", record.id)
            put("sender", record.sender)
            put("body", record.body)
            put("received_at", record.receivedAt)
            put("parts", record.parts)
            put("part_index", record.partIndex)
            put("source", record.source)
            record.groupThreadId?.let { put("thread_id", it) }
            if (record.subId in 0..99) put("sub_id", record.subId)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("sms"))
                .addHeader("Authorization", "Bearer $token")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            execute(request)
        } catch (e: RuntimeException) {
            ApiResult.Unreachable(e.javaClass.simpleName)
        }
    }

    /**
     * Delivery state of the given message ids as the backend sees them.
     * Used to learn that the backend CONFIRMED a delivery failure, which
     * is the only condition that authorises a cellular fallback.
     */
    fun fetchStates(ids: List<String>): Map<String, BackendState> {
        if (ids.isEmpty()) return emptyMap()
        return try {
            val request = Request.Builder()
                .url(endpoint("sms-state") + "?ids=" +
                    java.net.URLEncoder.encode(ids.joinToString(","), "UTF-8"))
                .addHeader("Authorization", "Bearer $token")
                .get()
                .build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return emptyMap()
                val text = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(text) }.getOrNull()
                val states = json?.optJSONObject("states") ?: return emptyMap()
                val result = mutableMapOf<String, BackendState>()
                for (key in states.keys()) {
                    val entry = states.optJSONObject(key) ?: continue
                    result[key] = BackendState(
                        state = entry.optString("state"),
                        attempts = entry.optInt("attempts"),
                        lastError = (entry.optString("last_error")
                            .takeUnless { it.isBlank() }
                            ?: entry.optString("lastError").takeUnless { it.isBlank() })
                    )
                }
                result
            }
        } catch (e: IOException) {
            emptyMap()
        } catch (e: RuntimeException) {
            emptyMap()
        }
    }

    /**
     * Sends one batch of historical SMS to the server.
     *
     * The server stores them as read-only history: nothing is forwarded to
     * Telegram. Provider ids make a replayed batch a no-op, so a lost
     * response only costs one duplicate request.
     */
    fun importBatch(messages: List<ImportMessage>): ImportResult {
        val items = JSONArray()
        for (message in messages) {
            items.put(
                JSONObject().apply {
                    if (message.providerId > 0) put("provider_id", message.providerId)
                    put("sender", message.sender)
                    put("body", message.body)
                    put("received_at", message.receivedAt)
                    put("sub_id", message.subId)
                }
            )
        }
        val payload = JSONObject().apply { put("items", items) }
        return try {
            val request = Request.Builder()
                .url(endpoint("import"))
                .addHeader("Authorization", "Bearer $token")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response ->
                val text = response.body?.string().orEmpty()
                when {
                    response.code == 401 || response.code == 403 ->
                        ImportResult.Unauthorized
                    response.isSuccessful -> {
                        val json = runCatching { JSONObject(text) }.getOrNull()
                        ImportResult.Accepted(
                            imported = json?.optInt("imported", 0) ?: 0,
                            duplicates = json?.optInt("duplicates", 0) ?: 0,
                            skipped = json?.optInt("skipped", 0) ?: 0
                        )
                    }
                    response.code == 429 || response.code >= 500 ->
                        ImportResult.Retryable(response.code)
                    else -> ImportResult.Rejected(response.code)
                }
            }
        } catch (e: IOException) {
            ImportResult.Unreachable(e.javaClass.simpleName)
        } catch (e: RuntimeException) {
            ImportResult.Unreachable(e.javaClass.simpleName)
        }
    }

    /** Health probe. Returns the round trip time in milliseconds. */
    fun probe(): Result<Long> {
        val started = System.currentTimeMillis()
        return try {
            val request = Request.Builder()
                .url(endpoint("health"))
                .get()
                .build()
            client.newCall(request).execute().use { response ->
                if (response.isSuccessful) {
                    Result.success(System.currentTimeMillis() - started)
                } else {
                    Result.failure(IOException("HTTP ${response.code}"))
                }
            }
        } catch (e: IOException) {
            Result.failure(e)
        } catch (e: RuntimeException) {
            Result.failure(e)
        }
    }

    /**
     * The SMS commands the backend waits for, newest last.
     *
     * Device token only. An empty list means "nothing to do" as well as
     * "the server could not be reached": the caller must not send anything
     * in either case.
     *
     * [deviceInfo] is an optional JSON heartbeat. The server treats it as
     * advisory, so a malformed value can never break the poll.
     */
    fun fetchCommands(deviceInfo: String? = null): List<RelayCommand> {
        return try {
            val builder = Request.Builder()
                .url(endpoint("commands"))
                .addHeader("Authorization", "Bearer $token")
            if (!deviceInfo.isNullOrBlank()) {
                builder.addHeader("X-Device-Info", deviceInfo)
            }
            val request = builder.get().build()
            client.newCall(request).execute().use { response ->
                if (!response.isSuccessful) return emptyList()
                val text = response.body?.string().orEmpty()
                val json = runCatching { JSONObject(text) }.getOrNull() ?: return emptyList()
                val array = json.optJSONArray("commands") ?: return emptyList()
                val commands = mutableListOf<RelayCommand>()
                for (index in 0 until array.length()) {
                    val item = array.optJSONObject(index) ?: continue
                    val id = item.optString("id").takeIf { it.isNotBlank() } ?: continue
                    commands.add(
                        RelayCommand(
                            id = id,
                            target = item.optString("target"),
                            body = item.optString("body"),
                            source = item.optString("source"),
                            createdAt = item.optLong("created_at"),
                            subId = item.optInt("sub_id", 0)
                        )
                    )
                }
                commands
            }
        } catch (e: IOException) {
            emptyList()
        } catch (e: RuntimeException) {
            emptyList()
        }
    }

    /**
     * Sends the contact list of the phone to the server.
     *
     * The server replaces the whole list with what arrives here, so the
     * phone sends everything it has. Names and numbers are never logged
     * and never shown anywhere but the bot's contact list.
     *
     * Returns false when the upload did not arrive.
     */
    fun uploadContacts(contacts: List<Contact>): Boolean {
        val array = JSONArray()
        for (contact in contacts) {
            val number = contact.number.trim()
            val name = contact.name.trim()
            // The server drops empty entries anyway; they are not sent.
            if (number.isEmpty() || name.isEmpty()) continue
            array.put(
                JSONObject().apply {
                    put("number", number)
                    put("name", name)
                }
            )
        }
        val payload = JSONObject().apply {
            put("contacts", array)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("contacts"))
                .addHeader("Authorization", "Bearer $token")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response -> response.isSuccessful }
        } catch (e: IOException) {
            false
        } catch (e: RuntimeException) {
            false
        }
    }

    /**
     * Report how a command ended. The backend forwards the outcome to the
     * admin in Telegram.
     *
     * Returns false when the report did not arrive, so the caller can try
     * again later instead of losing the outcome.
     */
    fun reportCommandResult(id: String, status: String, detail: String?): Boolean {
        val payload = JSONObject().apply {
            put("status", status)
            if (!detail.isNullOrBlank()) put("detail", detail)
        }
        return try {
            val request = Request.Builder()
                .url(endpoint("commands/$id/result"))
                .addHeader("Authorization", "Bearer $token")
                .post(payload.toString().toRequestBody(JSON))
                .build()
            client.newCall(request).execute().use { response -> response.isSuccessful }
        } catch (e: IOException) {
            false
        } catch (e: RuntimeException) {
            // A malformed id would break the URL: the command is reported
            // as "not delivered" instead of crashing the worker.
            false
        }
    }

    private fun execute(request: Request): ApiResult = try {
        client.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            when {
                response.code == 401 || response.code == 403 -> ApiResult.Unauthorized
                response.isSuccessful -> {
                    val json = runCatching { JSONObject(text) }.getOrNull()
                    ApiResult.Accepted(
                        duplicate = json?.optBoolean("duplicate", false) ?: false,
                        state = json?.optString("state").takeUnless { it.isNullOrBlank() }
                    )
                }
                response.code == 429 || response.code >= 500 ->
                    ApiResult.Retryable(response.code, jsonReason(text))
                else -> ApiResult.Rejected(response.code, jsonReason(text))
            }
        }
    } catch (e: IOException) {
        ApiResult.Unreachable(e.javaClass.simpleName)
    } catch (e: RuntimeException) {
        ApiResult.Unreachable(e.javaClass.simpleName)
    }

    private fun jsonReason(text: String): String? =
        runCatching {
            val json = JSONObject(text)
            json.optString("reason").takeUnless { it.isBlank() }
                ?: json.optJSONObject("detail")?.let { detail ->
                    when {
                        detail.optString("error").isNotBlank() -> detail.optString("error")
                        else -> detail.optString("detail").takeUnless { it.isBlank() }
                    }
                }
        }.getOrNull()

    private fun endpoint(path: String): String =
        baseUri.trimEnd('/') + "/" + path.trimStart('/')

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()

        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .writeTimeout(15, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build()
    }
}
