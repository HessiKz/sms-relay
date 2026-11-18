package dev.smsrelay.work

import android.content.Context
import androidx.work.Constraints
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.OutOfQuotaPolicy
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import dev.smsrelay.calls.CallLogWorker
import java.util.concurrent.TimeUnit

object Schedule {

    const val UNIQUE_UPLOAD = "sms-relay-upload"
    const val UNIQUE_PERIODIC = "sms-relay-periodic"
    const val UNIQUE_COMMANDS = "sms-relay-commands"
    const val UNIQUE_COMMANDS_ONCE = "sms-relay-commands-once"
    const val UNIQUE_COMMANDS_NEXT = "sms-relay-commands-next"
    const val UNIQUE_IMPORT = "sms-relay-import"
    const val UNIQUE_CALL_LOG = "sms-relay-call-log"
    const val UNIQUE_CALL_LOG_PERIODIC = "sms-relay-call-log-periodic"

    /**
     * How often the command queue is polled.
     *
     * The relay phone is a dedicated device, so a five-second cadence is
     * worth the radio wakeups: a Mini App message reaches the radio in
     * seconds instead of up to a minute.
     */
    const val COMMAND_POLL_SECONDS = 5L

    /**
     * One user-initiated run of the historical import.
     *
     * KEEP, not REPLACE: a second tap while the import is running must not
     * restart it from a newer cursor. The worker itself is resumable, so
     * the button can be pressed again after it finished.
     */
    fun enqueueImport(context: Context) {
        val request = OneTimeWorkRequestBuilder<ImportWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork(UNIQUE_IMPORT, ExistingWorkPolicy.KEEP, request)
    }

    /**
     * One user-initiated run of the call-log sync.
     *
     * KEEP, not REPLACE: a second tap while a run is going must not start
     * a second one, and the worker is resumable, so the button can be
     * pressed again after it finished.
     */
    fun enqueueCallLog(context: Context) {
        val request = OneTimeWorkRequestBuilder<CallLogWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork(UNIQUE_CALL_LOG, ExistingWorkPolicy.KEEP, request)
    }

    /**
     * Periodic call-log sync (15 min), the safety net for the forward
     * cursor and the rolling recent window.
     *
     * WorkManager refuses a shorter interval, and the call log does not
     * need a tighter cadence: a call row is written when the call ends,
     * and the Mini App shows the live call through the call session, not
     * through the log.
     */
    fun scheduleCallLog(context: Context) {
        val request = PeriodicWorkRequestBuilder<CallLogWorker>(15, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_CALL_LOG_PERIODIC,
            ExistingPeriodicWorkPolicy.KEEP,
            request
        )
    }

    /** Immediate upload attempt, network constrained. */
    fun enqueueUpload(context: Context) {
        val request = OneTimeWorkRequestBuilder<UploadWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
            .build()
        WorkManager.getInstance(context)
            .enqueueUniqueWork(UNIQUE_UPLOAD, ExistingWorkPolicy.REPLACE, request)
    }

    /** Periodic safety net (15 min) so nothing stays stuck if a push fails. */
    fun schedulePeriodic(context: Context) {
        val request = PeriodicWorkRequestBuilder<UploadWorker>(15, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_PERIODIC,
            ExistingPeriodicWorkPolicy.KEEP,
            request
        )
    }

    /**
     * Periodic poll for the commands that arrived from Telegram.
     *
     * WorkManager refuses a periodic interval below 15 minutes, so this is
     * the safety net. The short cadence is the chain of
     * [enqueueNextCommandPoll].
     */
    fun scheduleCommands(context: Context) {
        val request = PeriodicWorkRequestBuilder<CommandWorker>(15, TimeUnit.MINUTES)
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
            UNIQUE_COMMANDS,
            ExistingPeriodicWorkPolicy.KEEP,
            request
        )
    }

    /** Immediate poll for the commands that arrived from Telegram. */
    fun enqueueCommands(context: Context) {
        val request = OneTimeWorkRequestBuilder<CommandWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        // KEEP, not REPLACE: a poll that is already waiting must not be
        // pushed back every time the app is opened.
        WorkManager.getInstance(context)
            .enqueueUniqueWork(UNIQUE_COMMANDS_ONCE, ExistingWorkPolicy.KEEP, request)
    }

    /**
     * Starts the command poll chain from the beginning.
     *
     * The chain is stopped while the phone is not paired (the worker
     * does not talk to the server without a token), so it has to be
     * armed again after the pairing.
     */
    fun restartCommandChain(context: Context) {
        val request = OneTimeWorkRequestBuilder<CommandWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_COMMANDS_NEXT,
            ExistingWorkPolicy.REPLACE,
            request
        )
    }

    /**
     * The next poll of the chain, five seconds from now.
     *
     * WorkManager does not allow a periodic work below 15 minutes, so the
     * short cadence is a chain: every run arms the next one. One stable
     * name with REPLACE keeps exactly one link waiting, so a run that is
     * started twice cannot multiply the chain.
     */
    fun enqueueNextCommandPoll(context: Context) {
        val request = OneTimeWorkRequestBuilder<CommandWorker>()
            .setConstraints(
                Constraints.Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            )
            .setInitialDelay(COMMAND_POLL_SECONDS, TimeUnit.SECONDS)
            .build()
        WorkManager.getInstance(context).enqueueUniqueWork(
            UNIQUE_COMMANDS_NEXT,
            ExistingWorkPolicy.REPLACE,
            request
        )
    }
}
