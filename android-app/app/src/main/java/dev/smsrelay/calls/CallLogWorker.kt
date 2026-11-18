package dev.smsrelay.calls

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import dev.smsrelay.SettingsStore
import dev.smsrelay.net.CallSyncItem
import dev.smsrelay.net.CallSyncResult
import dev.smsrelay.net.RelayClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Copies the phone's call log to the server, then keeps it fresh.
 *
 * Two passes, because a call log row is not final when it is written:
 *
 *  1. a forward `_ID` cursor reads every row the phone has not sent yet,
 *     oldest first, in pages of [BATCH];
 *  2. a rolling seven-day `DATE` window re-reads the calls that may have
 *     changed since - a row is written when the call ENDS, and the user
 *     opening the log sets `IS_READ`.
 *
 * The rules follow the SMS import, with one deliberate difference: a
 * call row that the server permanently rejects must not block the whole
 * log, so the cursor moves past it and the row is marked failed. A
 * transient failure (429, 5xx, no network, 401) stops the run WITHOUT
 * moving the cursor, so the next run repeats it.
 *
 * Numbers, names and durations are never logged.
 */
class CallLogWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val settings = SettingsStore(applicationContext)
        if (settings.baseUri.isBlank()) return@withContext failure(ERROR_SERVER)
        val token = settings.token
        if (token.isBlank()) {
            // The phone is not paired: the call log stays where it is.
            return@withContext failure(ERROR_NOT_PAIRED)
        }
        if (ContextCompat.checkSelfPermission(
                applicationContext, Manifest.permission.READ_CALL_LOG
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return@withContext failure(ERROR_NO_PERMISSION)
        }

        // A cursor of an older layout cannot describe which rows were
        // already sent, so the sync restarts from the first row once per
        // install. The server deduplicates by content hash.
        if (settings.callLogCursorFormat != CURSOR_FORMAT) {
            settings.callLogCursor = 0L
            settings.callLogCursorFormat = CURSOR_FORMAT
        }

        val client = RelayClient(settings.baseUri, token) { settings.handleUnauthorized() }
        val dao = CallLogDatabase.get(applicationContext).callLogDao()
        var cursor = settings.callLogCursor
        var processed = 0
        var inserted = 0
        var updated = 0
        var unchanged = 0
        var failed = 0

        // 1. Forward pass over the rows the phone has not sent yet.
        while (true) {
            val page = try {
                CallLogReader.page(applicationContext, cursor, BATCH)
            } catch (e: SecurityException) {
                return@withContext failure(ERROR_NO_PERMISSION)
            } catch (e: Exception) {
                return@withContext failure(ERROR_READ)
            }
            if (page.rows == 0) break
            if (page.lastId <= cursor) break

            // Room first, upload second: a crash between the two only
            // leaves queued rows behind, never a gap in the log.
            //
            // This insert is FATAL, not best effort. Advancing the cursor
            // past a page that was not stored would skip those calls for
            // ever, because nothing reads them again: the run is retried
            // with the same cursor instead.
            try {
                dao.insertAll(page.calls)
            } catch (e: Exception) {
                return@withContext retryFor(Stop.STORAGE)
            }

            val upload = uploadPending(client, dao)
            inserted += upload.inserted
            updated += upload.updated
            unchanged += upload.unchanged
            failed += upload.failed
            processed += page.rows
            setProgressAsync(progress(processed, inserted, updated, unchanged, failed))

            // A transient stop keeps the cursor before this page, so the
            // page is read again on the next run.
            if (upload.stop != null) {
                return@withContext retryFor(upload.stop)
            }

            cursor = page.lastId
            settings.callLogCursor = cursor
            if (page.rows < BATCH) break
        }

        // 2. Rolling recent window: duration and read state of the last
        // days change after the row was first seen.
        val recent = try {
            CallLogReader.recent(applicationContext)
        } catch (e: SecurityException) {
            return@withContext failure(ERROR_NO_PERMISSION)
        } catch (e: Exception) {
            return@withContext failure(ERROR_READ)
        }
        var newest = 0L
        val changed = mutableListOf<CallLogEntity>()
        for (entity in recent) {
            if (entity.startedAt > newest) newest = entity.startedAt
            val stored = dao.findByProvider(entity.providerId)
            if (stored == null) {
                // A call the phone had before its cursor (or one that
                // fell out of the window): it is uploaded once.
                changed.add(entity)
                continue
            }
            if (stored.contentHash == entity.contentHash) continue
            // The same call, rewritten by the provider: the duration is
            // final, or the user read it. It goes back into the queue so
            // the server learns the new values.
            //
            // A row the server permanently refused stays failed: re-queuing
            // it would spend one request on the same rejection on every run.
            // The newer values are still stored, so the phone's copy is
            // right either way.
            val state = if (stored.uploadState == CallLogEntity.STATE_FAILED) {
                CallLogEntity.STATE_FAILED
            } else {
                CallLogEntity.STATE_QUEUED
            }
            changed.add(
                entity.copy(
                    id = stored.id,
                    uploadState = state,
                    attempts = stored.attempts,
                    lastError = stored.lastError
                )
            )
        }
        if (changed.isNotEmpty()) {
            // Same rule as the forward pass: the re-queued rows must reach
            // Room before anything is uploaded, or the run would report
            // success for values that were never stored.
            try {
                dao.upsertAll(changed)
            } catch (e: Exception) {
                return@withContext retryFor(Stop.STORAGE)
            }
        }
        val recentUpload = uploadPending(client, dao)
        inserted += recentUpload.inserted
        updated += recentUpload.updated
        unchanged += recentUpload.unchanged
        failed += recentUpload.failed
        if (recentUpload.stop != null) {
            setProgressAsync(progress(processed, inserted, updated, unchanged, failed))
            return@withContext retryFor(recentUpload.stop)
        }
        if (newest > 0L && newest > settings.callLogRecentCursor) {
            settings.callLogRecentCursor = newest
        }

        settings.lastCallSyncAt = System.currentTimeMillis()
        setProgressAsync(progress(processed, inserted, updated, unchanged, failed))
        Result.success(
            workDataOf(
                KEY_PROCESSED to processed,
                KEY_INSERTED to inserted,
                KEY_UPDATED to updated,
                KEY_UNCHANGED to unchanged,
                KEY_FAILED to failed
            )
        )
    }

    /** How one upload round ended. */
    private class Upload(
        val inserted: Int,
        val updated: Int,
        val unchanged: Int,
        val failed: Int,
        /** Non-null when the run has to stop and be retried later. */
        val stop: Stop?
    )

    /** Why a run stopped early. */
    private enum class Stop { UNAUTHORIZED, RETRYABLE, UNREACHABLE, STORAGE }

    /**
     * Uploads every queued row, [BATCH] at a time.
     *
     * A permanent rejection of one row marks only that row failed and the
     * loop goes on: one bad row must not hold the whole log hostage, and it
     * must not take its healthy neighbours with it. A transient failure
     * stops the loop and leaves the rows queued.
     */
    private fun uploadPending(client: RelayClient, dao: CallLogDao): Upload {
        var inserted = 0
        var updated = 0
        var unchanged = 0
        var failed = 0
        while (true) {
            val batch = dao.pending(BATCH)
            if (batch.isEmpty()) break
            when (val result = client.syncCalls(batch.map { it.toItem() })) {
                is CallSyncResult.Accepted -> {
                    inserted += result.inserted
                    updated += result.updated
                    unchanged += result.unchanged
                    failed += result.skipped
                    for (row in batch) dao.markSent(row.id)
                }

                // The token was dropped by the client: every row stays
                // queued for the next pairing, and the run stops without
                // marking anything failed.
                is CallSyncResult.Unauthorized ->
                    return Upload(inserted, updated, unchanged, failed, Stop.UNAUTHORIZED)

                is CallSyncResult.Rejected -> {
                    // A confirmed refusal of this batch. The API validates
                    // a request as one document, so ONE malformed item
                    // returns 422 for all of them; a multi-row batch is
                    // therefore re-tried row by row. Only the row that is
                    // really refused is marked failed, the healthy rest
                    // are sent, and nothing is lost with the batch.
                    if (batch.size == 1) {
                        // No second request: the single row IS the request
                        // that was just refused.
                        failed += 1
                        dao.markFailed(batch[0].id, "http_${result.code}")
                    } else {
                        val rejected = rejectBatch(client, dao, batch)
                        inserted += rejected.inserted
                        updated += rejected.updated
                        unchanged += rejected.unchanged
                        failed += rejected.failed
                        if (rejected.stop != null) {
                            return Upload(inserted, updated, unchanged, failed, rejected.stop)
                        }
                    }
                }

                is CallSyncResult.Retryable ->
                    return Upload(inserted, updated, unchanged, failed, Stop.RETRYABLE)

                is CallSyncResult.Unreachable ->
                    return Upload(inserted, updated, unchanged, failed, Stop.UNREACHABLE)
            }
        }
        return Upload(inserted, updated, unchanged, failed, null)
    }

    private fun CallLogEntity.toItem(): CallSyncItem = CallSyncItem(
        providerId = providerId,
        // Every outbound string is capped at the server's own column
        // width. The server truncates as well, but a client that sends a
        // value the API refuses would turn one long field into a 422 for
        // the whole batch, so the cut happens here first.
        number = number.take(MAX_NUMBER_LENGTH),
        cachedName = cachedName?.take(MAX_NAME_LENGTH),
        kind = kind,
        rawType = rawType,
        startedAt = startedAt,
        durationS = durationS,
        subId = subId,
        phoneAccount = phoneAccount?.take(MAX_ACCOUNT_LENGTH),
        isRead = isRead,
        contentHash = contentHash.take(MAX_HASH_LENGTH)
    )

    /**
     * A batch the server refused as a whole.
     *
     * The API validates a request as one document, so a single malformed
     * item returns 422 for all of them. Re-sending one row at a time
     * isolates the offender: that row is marked failed (the cursor may
     * pass it) and every other row stays queued. A transient stop during
     * the walk stops the run with the cursor where it was.
     */
    private fun rejectBatch(
        client: RelayClient,
        dao: CallLogDao,
        batch: List<CallLogEntity>
    ): Upload {
        var inserted = 0
        var updated = 0
        var unchanged = 0
        var failed = 0
        for (row in batch) {
            when (val result = client.syncCalls(listOf(row.toItem()))) {
                is CallSyncResult.Accepted -> {
                    inserted += result.inserted
                    updated += result.updated
                    unchanged += result.unchanged
                    failed += result.skipped
                    dao.markSent(row.id)
                }

                is CallSyncResult.Unauthorized ->
                    return Upload(inserted, updated, unchanged, failed, Stop.UNAUTHORIZED)

                is CallSyncResult.Rejected -> {
                    failed += 1
                    dao.markFailed(row.id, "http_${result.code}")
                }

                is CallSyncResult.Retryable ->
                    return Upload(inserted, updated, unchanged, failed, Stop.RETRYABLE)

                is CallSyncResult.Unreachable ->
                    return Upload(inserted, updated, unchanged, failed, Stop.UNREACHABLE)
            }
        }
        // Every row recorded its own outcome; the run simply goes on. A row
        // that is still queued here was left alone on purpose.
        return Upload(inserted, updated, unchanged, failed, null)
    }

    private fun retryFor(stop: Stop): Result = when (stop) {
        // The rest of the queue waits for the next pairing or the next
        // run; nothing is deleted and nothing is marked failed.
        Stop.UNAUTHORIZED, Stop.RETRYABLE, Stop.UNREACHABLE -> Result.retry()
        // Room could not store the page: the run is repeated with the
        // same cursor, so the calls are read again instead of skipped.
        Stop.STORAGE -> Result.retry()
    }

    private fun progress(
        processed: Int,
        inserted: Int,
        updated: Int,
        unchanged: Int,
        failed: Int
    ) = workDataOf(
        KEY_PROCESSED to processed,
        KEY_INSERTED to inserted,
        KEY_UPDATED to updated,
        KEY_UNCHANGED to unchanged,
        KEY_FAILED to failed
    )

    private fun failure(reason: String): Result =
        Result.failure(workDataOf(KEY_ERROR to reason))

    companion object {
        /** Page size of the forward cursor and of the upload batch. */
        const val BATCH = 100

        /**
         * The widest value the server accepts for each string field. They
         * mirror `backend/app/calls.py`; a client that sends more would get
         * a 422 for the whole batch.
         */
        const val MAX_NUMBER_LENGTH = 64
        const val MAX_NAME_LENGTH = 96
        const val MAX_ACCOUNT_LENGTH = 64
        const val MAX_HASH_LENGTH = 128

        /**
         * Cursor layout of this build: 1 = `_ID` of
         * `content://call_log/calls`, oldest first.
         */
        const val CURSOR_FORMAT = 1

        const val KEY_PROCESSED = "processed"
        const val KEY_INSERTED = "inserted"
        const val KEY_UPDATED = "updated"
        const val KEY_UNCHANGED = "unchanged"
        const val KEY_FAILED = "failed"
        const val KEY_ERROR = "error"

        const val ERROR_SERVER = "server"
        const val ERROR_NO_PERMISSION = "no_permission"
        const val ERROR_NOT_PAIRED = "not_paired"
        const val ERROR_READ = "read"
    }
}
