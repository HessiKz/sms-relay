package dev.smsrelay.sms

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build

/**
 * Remembers the radio's answer for every part of a fallback SMS.
 *
 * The radio reports each part of a long SMS through its own PendingIntent.
 * A multipart message is only confirmed when ALL parts report success: the
 * first successful part must not mark the whole message as sent. The state
 * lives in SharedPreferences, so it survives a process death between the
 * radio call and the status broadcast.
 */
object FallbackPartTracker {

    sealed class Outcome {
        /** Some parts are still unanswered. */
        object Pending : Outcome()

        /** Every part was accepted by the radio. */
        object AllOk : Outcome()

        /** At least one part failed. */
        object Failed : Outcome()

        /** There is no state for this send (lost/expired/foreign log id). */
        object Unknown : Outcome()
    }

    private const val PREFS = "fallback_parts"

    /** A trace older than this belongs to a send that will never answer. */
    private const val MAX_AGE_MS = 24L * 60 * 60 * 1000

    /** Start tracking one send. Must be called before the radio call. */
    @Synchronized
    fun begin(context: Context, messageId: String, logId: Long, partCount: Int) {
        val prefs = prefs(context)
        val now = System.currentTimeMillis()
        prefs.edit()
            .putString(messageId, encode(Entry(logId, now, partCount, emptyMap())))
            .apply()
        prune(prefs, now)
    }

    /** Record the answer of one part and report the state of the whole send. */
    @Synchronized
    fun record(
        context: Context,
        messageId: String,
        logId: Long,
        partIndex: Int,
        ok: Boolean
    ): Outcome {
        val prefs = prefs(context)
        val entry = decode(prefs.getString(messageId, null)) ?: return Outcome.Unknown
        if (entry.logId != logId) return Outcome.Unknown
        if (partIndex < 0 || partIndex >= entry.partCount) return Outcome.Unknown
        val statuses = entry.statuses.toMutableMap()
        statuses[partIndex] = ok
        prefs.edit()
            .putString(messageId, encode(entry.copy(statuses = statuses)))
            .apply()
        return outcome(entry.partCount, statuses)
    }

    @Synchronized
    fun clear(context: Context, messageId: String) {
        prefs(context).edit().remove(messageId).apply()
    }

    /** One PendingIntent per part, each with its own request code. */
    fun pendingIntents(
        context: Context,
        messageId: String,
        logId: Long,
        partCount: Int
    ): List<PendingIntent> = List(partCount) { index ->
        val intent = Intent(context, SendStatusReceiver::class.java).apply {
            putExtra(SendStatusReceiver.EXTRA_MESSAGE_ID, messageId)
            putExtra(SendStatusReceiver.EXTRA_LOG_ID, logId)
            putExtra(SendStatusReceiver.EXTRA_PART_INDEX, index)
        }
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags = flags or PendingIntent.FLAG_IMMUTABLE
        }
        PendingIntent.getBroadcast(context, requestCode(logId, index), intent, flags)
    }

    private fun requestCode(logId: Long, partIndex: Int): Int =
        (logId * 31L + partIndex).toInt()

    private fun outcome(partCount: Int, statuses: Map<Int, Boolean>): Outcome = when {
        statuses.any { !it.value } -> Outcome.Failed
        statuses.size >= partCount -> Outcome.AllOk
        else -> Outcome.Pending
    }

    private data class Entry(
        val logId: Long,
        val startedAt: Long,
        val partCount: Int,
        val statuses: Map<Int, Boolean>
    )

    private fun encode(entry: Entry): String = buildString {
        append(entry.logId).append(';')
        append(entry.startedAt).append(';')
        append(entry.partCount).append(';')
        append(entry.statuses.entries.joinToString(",") {
            "${it.key}:${if (it.value) 1 else 0}"
        })
    }

    private fun decode(raw: String?): Entry? {
        val fields = raw?.split(';', limit = 4) ?: return null
        if (fields.size != 4) return null
        val logId = fields[0].toLongOrNull() ?: return null
        val startedAt = fields[1].toLongOrNull() ?: return null
        val partCount = fields[2].toIntOrNull() ?: return null
        val statuses = if (fields[3].isBlank()) {
            emptyMap()
        } else {
            fields[3].split(',').mapNotNull { token ->
                val pair = token.split(':', limit = 2)
                val index = pair.getOrNull(0)?.toIntOrNull() ?: return@mapNotNull null
                index to (pair.getOrNull(1) == "1")
            }.toMap()
        }
        return Entry(logId, startedAt, partCount, statuses)
    }

    private fun prune(prefs: SharedPreferences, now: Long) {
        val stale = prefs.all.entries
            .filter { (_, value) ->
                val entry = decode(value as? String)
                entry == null || now - entry.startedAt > MAX_AGE_MS
            }
            .map { it.key }
        if (stale.isEmpty()) return
        val editor = prefs.edit()
        for (key in stale) editor.remove(key)
        editor.apply()
    }

    private fun prefs(context: Context): SharedPreferences =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
}
