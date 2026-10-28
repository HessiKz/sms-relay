package dev.smsrelay.work

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import dev.smsrelay.SettingsStore
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.net.ApiResult
import dev.smsrelay.net.RelayClient
import dev.smsrelay.sms.FallbackPolicy
import dev.smsrelay.sms.FallbackSender
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * Uploads queued messages to the relay backend.
 *
 * Rules:
 *  - upload sequentially with a small gap, so a burst does not hammer the API;
 *  - a 200 response (also for a duplicate id) means the backend owns the
 *    message now: stop retrying;
 *  - a 401 is a configuration error: keep the message, retry slowly;
 *  - a rejected payload (4xx) is a permanent failure of THAT message: the
 *    message is marked failed, and only the backend's own per-id state may
 *    later authorise a cellular fallback;
 *  - network failures and 5xx are retryable, never a confirmed failure;
 *  - the fallback decision is taken from the backend's per-id state, so a
 *    timeout can never cause a duplicate SMS over the air.
 */
class UploadWorker(
    context: Context,
    params: WorkerParameters
) : CoroutineWorker(context, params) {

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        val settings = SettingsStore(applicationContext)
        if (settings.token.isBlank() || settings.baseUri.isBlank()) {
            return@withContext Result.success()
        }
        val database = AppDatabase.get(applicationContext)
        val dao = database.messageDao()
        val fallbackDao = database.fallbackLogDao()
        val client = RelayClient(settings.baseUri, settings.token)
        val policy = FallbackPolicy(settings, dao, fallbackDao)
        val sender = FallbackSender(applicationContext, settings, fallbackDao)

        val pending = dao.pendingUploads(limit = BATCH)
        if (pending.isEmpty()) {
            policy.onPeriodicSweep(client, sender)
            return@withContext Result.success()
        }

        var retryNeeded = false
        for (record in pending) {
            when (val result = client.upload(record)) {
                is ApiResult.Accepted -> {
                    dao.markUploadState(
                        id = record.id,
                        uploadState = if (result.duplicate) {
                            MessageRecord.STATE_DUPLICATE
                        } else {
                            MessageRecord.STATE_SENT
                        },
                        attempts = record.uploadAttempts,
                        attemptAt = System.currentTimeMillis(),
                        error = null,
                        backendState = result.state
                    )
                    settings.lastUploadAt = System.currentTimeMillis()
                }

                is ApiResult.Unauthorized -> {
                    dao.markUploadState(
                        id = record.id,
                        uploadState = MessageRecord.STATE_QUEUED,
                        attempts = record.uploadAttempts + 1,
                        attemptAt = System.currentTimeMillis(),
                        error = "unauthorized",
                        backendState = record.backendState
                    )
                    retryNeeded = true
                }

                is ApiResult.Rejected -> {
                    // The backend refused this message (bad payload or a
                    // rule violation). That is a confirmed failure of the
                    // message, not of the network. The backend stored
                    // nothing for it, so the fallback policy may send it
                    // over the air.
                    dao.markUploadState(
                        id = record.id,
                        uploadState = MessageRecord.STATE_FAILED,
                        attempts = record.uploadAttempts + 1,
                        attemptAt = System.currentTimeMillis(),
                        error = "http_${result.code}",
                        backendState = MessageRecord.BACKEND_STATE_FAILED
                    )
                }

                is ApiResult.Retryable -> {
                    // 429 / 5xx: the backend or Telegram is temporarily
                    // unavailable. Keep the message queued and retry --
                    // this is NOT a confirmed delivery failure.
                    dao.markUploadState(
                        id = record.id,
                        uploadState = MessageRecord.STATE_QUEUED,
                        attempts = record.uploadAttempts + 1,
                        attemptAt = System.currentTimeMillis(),
                        error = "http_${result.code}",
                        backendState = record.backendState
                    )
                    retryNeeded = true
                }

                is ApiResult.Unreachable -> {
                    dao.markUploadState(
                        id = record.id,
                        uploadState = MessageRecord.STATE_QUEUED,
                        attempts = record.uploadAttempts + 1,
                        attemptAt = System.currentTimeMillis(),
                        error = "unreachable",
                        backendState = record.backendState
                    )
                    retryNeeded = true
                }
            }
            delay(REQUEST_GAP_MS)
        }

        policy.onPeriodicSweep(client, sender)
        if (retryNeeded) Result.retry() else Result.success()
    }

    companion object {
        private const val BATCH = 20
        private const val REQUEST_GAP_MS = 500L
    }
}
