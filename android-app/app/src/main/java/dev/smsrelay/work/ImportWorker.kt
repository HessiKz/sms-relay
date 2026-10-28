package dev.smsrelay.work

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.provider.Telephony
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dev.smsrelay.SettingsStore
import dev.smsrelay.net.ImportMessage
import dev.smsrelay.net.ImportResult
import dev.smsrelay.net.RelayClient
import dev.smsrelay.util.RoleState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Copies the phone's existing SMS inbox to the server, in batches.
 *
 * The worker resumes from the last imported row id, so stopping the app or
 * losing the connection does not start the import over. Rows are sent
 * directly; they are never written to the local Room database, and the
 * server stores them as history that is not forwarded to Telegram.
 *
 * Message bodies are never logged.
 */
class ImportWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val settings = SettingsStore(applicationContext)
        if (settings.baseUri.isBlank()) return@withContext failure(ERROR_SERVER)
        if (!RoleState.isDefaultSmsApp(applicationContext)) {
            return@withContext failure(ERROR_NO_ROLE)
        }
        val readSms = ContextCompat.checkSelfPermission(
            applicationContext, Manifest.permission.READ_SMS
        )
        if (readSms != PackageManager.PERMISSION_GRANTED) {
            return@withContext failure(ERROR_NO_PERMISSION)
        }

        val client = RelayClient(settings.baseUri, settings.token)
        var cursor = settings.importCursor
        var processed = 0
        var imported = 0
        var duplicates = 0
        var skipped = 0

        while (true) {
            val page = try {
                readPage(cursor)
            } catch (e: SecurityException) {
                return@withContext failure(ERROR_NO_PERMISSION)
            } catch (e: Exception) {
                return@withContext failure(ERROR_READ)
            }
            if (page.rows == 0) break
            if (page.lastId <= cursor) break

            skipped += page.rows - page.messages.size
            if (page.messages.isNotEmpty()) {
                when (val result = client.importBatch(page.messages)) {
                    is ImportResult.Accepted -> {
                        imported += result.imported
                        duplicates += result.duplicates
                        skipped += result.skipped
                    }
                    is ImportResult.Unauthorized ->
                        return@withContext failure(ERROR_AUTH)
                    is ImportResult.Rejected ->
                        return@withContext failure(ERROR_REJECTED)
                    is ImportResult.Retryable ->
                        return@withContext Result.retry()
                    is ImportResult.Unreachable ->
                        return@withContext Result.retry()
                }
            }

            // The cursor moves only after the server accepted the batch:
            // a failed request is replayed from the previous position.
            cursor = page.lastId
            settings.importCursor = cursor
            processed += page.rows
            setProgressAsync(progress(processed, imported, duplicates, skipped))
            if (page.rows < BATCH) break
        }

        setProgressAsync(progress(processed, imported, duplicates, skipped))
        Result.success(
            workDataOf(
                KEY_PROCESSED to processed,
                KEY_IMPORTED to imported,
                KEY_DUPLICATES to duplicates,
                KEY_SKIPPED to skipped
            )
        )
    }

    private data class Page(
        val rows: Int,
        val lastId: Long,
        val messages: List<ImportMessage>
    )

    /**
     * One page of the inbox, oldest first.
     *
     * The limit is also in the sort order, because not every SMS provider
     * honours it. Reading is capped at [BATCH] rows regardless, and the
     * next page continues after the last row actually read.
     */
    private fun readPage(cursor: Long): Page {
        val projection = arrayOf(
            Telephony.Sms._ID,
            Telephony.Sms.ADDRESS,
            Telephony.Sms.BODY,
            Telephony.Sms.DATE,
            Telephony.Sms.SUBSCRIPTION_ID
        )
        val result = applicationContext.contentResolver.query(
            Telephony.Sms.Inbox.CONTENT_URI,
            projection,
            "${Telephony.Sms._ID} > ?",
            arrayOf(cursor.toString()),
            "${Telephony.Sms._ID} ASC LIMIT $BATCH"
        ) ?: throw IllegalStateException("SMS inbox unavailable")

        var rows = 0
        var lastId = cursor
        val messages = mutableListOf<ImportMessage>()
        result.use { query ->
            val idIndex = query.getColumnIndex(Telephony.Sms._ID)
            val addressIndex = query.getColumnIndex(Telephony.Sms.ADDRESS)
            val bodyIndex = query.getColumnIndex(Telephony.Sms.BODY)
            val dateIndex = query.getColumnIndex(Telephony.Sms.DATE)
            if (idIndex < 0 || addressIndex < 0 || bodyIndex < 0 || dateIndex < 0) {
                return Page(0, cursor, emptyList())
            }
            val subIndex = query.getColumnIndex(Telephony.Sms.SUBSCRIPTION_ID)
            while (rows < BATCH && query.moveToNext()) {
                rows += 1
                val id = query.getLong(idIndex)
                if (id <= lastId) continue
                lastId = id
                val sender = query.getString(addressIndex)?.trim().orEmpty()
                val body = query.getString(bodyIndex).orEmpty()
                val seconds = epochSeconds(query.getLong(dateIndex))
                if (!isUsable(sender, body, seconds)) continue
                val subId = if (subIndex >= 0) query.getInt(subIndex) else 0
                messages.add(
                    ImportMessage(
                        providerId = id,
                        sender = sender,
                        body = body,
                        receivedAt = seconds,
                        subId = subId.coerceIn(0, 99)
                    )
                )
            }
        }
        return Page(rows, lastId, messages)
    }

    private fun isUsable(sender: String, body: String, seconds: Long): Boolean {
        if (sender.isEmpty() || body.isEmpty()) return false
        if (sender.length > MAX_SENDER || body.length > MAX_BODY) return false
        val now = System.currentTimeMillis() / 1000L
        return seconds >= MIN_RECEIVED_AT && seconds <= now + FUTURE_GRACE_S
    }

    private fun epochSeconds(value: Long): Long =
        if (value > 100_000_000_000L) value / 1000L else value

    private fun progress(processed: Int, imported: Int,
                         duplicates: Int, skipped: Int) = workDataOf(
        KEY_PROCESSED to processed,
        KEY_IMPORTED to imported,
        KEY_DUPLICATES to duplicates,
        KEY_SKIPPED to skipped
    )

    private fun failure(reason: String): Result =
        Result.failure(workDataOf(KEY_ERROR to reason))

    companion object {
        const val BATCH = 100
        const val MAX_SENDER = 64
        const val MAX_BODY = 16000
        const val MIN_RECEIVED_AT = 946684800L
        const val FUTURE_GRACE_S = 86400L

        const val KEY_PROCESSED = "processed"
        const val KEY_IMPORTED = "imported"
        const val KEY_DUPLICATES = "duplicates"
        const val KEY_SKIPPED = "skipped"
        const val KEY_ERROR = "error"

        const val ERROR_SERVER = "server"
        const val ERROR_NO_ROLE = "no_role"
        const val ERROR_NO_PERMISSION = "no_permission"
        const val ERROR_AUTH = "auth"
        const val ERROR_REJECTED = "rejected"
        const val ERROR_READ = "read"
    }
}
