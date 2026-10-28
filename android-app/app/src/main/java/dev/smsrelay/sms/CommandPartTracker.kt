package dev.smsrelay.sms

import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.os.Build

/**
 * Remembers the radio's and the carrier's answers for every part of a
 * command SMS.
 *
 * A multipart message is only sent when every part was accepted by the
 * radio, and only delivered when every part has a carrier report. The
 * state lives in SharedPreferences, so it survives a process death
 * between the radio call and the status broadcast.
 *
 * The two phases are independent: a delivery report that arrives before
 * the sent report of another part must not lose the earlier answers.
 */
object CommandPartTracker {

    enum class Phase(val wire: String) {
        SENT("sent"),
        DELIVERED("delivered");

        companion object {
            fun fromWire(value: String?): Phase? =
                values().firstOrNull { it.wire == value }
        }
    }

    sealed class Outcome {
        /** Some parts of this phase are still unanswered. */
        object Pending : Outcome()

        /** Every part of this phase was accepted. */
        object AllOk : Outcome()

        /** At least one part of this phase was refused. */
        object Failed : Outcome()

        /** There is no matching state (lost, expired or old report). */
        object Unknown : Outcome()
    }

    private const val PREFS = "command_parts"

    /** A trace older than this belongs to a send that will never answer. */
    private const val MAX_AGE_MS = 24L * 60 * 60 * 1000

    /** Start tracking one send. Must be called before the radio call. */
    @Synchronized
    fun begin(context: Context, commandId: String, partCount: Int) {
        val prefs = prefs(context)
        val now = System.currentTimeMillis()
        prefs.edit()
            .putString(
                commandId,
                encode(Entry(now, partCount, emptyMap(), emptyMap()))
            )
            .apply()
        prune(prefs, now)
    }

    /** Record one answer and report the state of that phase. */
    @Synchronized
    fun record(
        context: Context,
        commandId: String,
        phase: Phase,
        partIndex: Int,
        ok: Boolean
    ): Outcome {
        val prefs = prefs(context)
        val entry = decode(prefs.getString(commandId, null)) ?: return Outcome.Unknown
        if (partIndex < 0 || partIndex >= entry.partCount) return Outcome.Unknown
        val statuses = if (phase == Phase.DELIVERED) {
            entry.delivered.toMutableMap()
        } else {
            entry.sent.toMutableMap()
        }
        statuses[partIndex] = ok
        val updated = if (phase == Phase.DELIVERED) {
            entry.copy(delivered = statuses)
        } else {
            entry.copy(sent = statuses)
        }
        prefs.edit().putString(commandId, encode(updated)).apply()
        return outcome(entry.partCount, statuses)
    }

    @Synchronized
    fun clear(context: Context, commandId: String) {
        prefs(context).edit().remove(commandId).apply()
    }

    /**
     * One PendingIntent per part for one phase. The sent and delivery
     * intents of the same part need different request codes, otherwise
     * FLAG_UPDATE_CURRENT would overwrite one with the other.
     */
    fun pendingIntents(
        context: Context,
        commandId: String,
        kind: String,
        partCount: Int
    ): List<PendingIntent> = List(partCount) { index ->
        val intent = Intent(context, CommandSendStatusReceiver::class.java).apply {
            putExtra(CommandSendStatusReceiver.EXTRA_COMMAND_ID, commandId)
            putExtra(CommandSendStatusReceiver.EXTRA_KIND, kind)
            putExtra(CommandSendStatusReceiver.EXTRA_PART_INDEX, index)
        }
        var flags = PendingIntent.FLAG_UPDATE_CURRENT
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            flags = flags or PendingIntent.FLAG_IMMUTABLE
        }
        PendingIntent.getBroadcast(
            context,
            requestCode(commandId, kind, index),
            intent,
            flags
        )
    }

    private fun requestCode(commandId: String, kind: String, partIndex: Int): Int {
        val phase = if (kind == CommandSendStatusReceiver.KIND_DELIVERED) 1 else 0
        return commandId.hashCode() * 31 + partIndex * 2 + phase
    }

    private fun outcome(partCount: Int, statuses: Map<Int, Boolean>): Outcome = when {
        statuses.any { !it.value } -> Outcome.Failed
        statuses.size >= partCount -> Outcome.AllOk
        else -> Outcome.Pending
    }

    private data class Entry(
        val startedAt: Long,
        val partCount: Int,
        val sent: Map<Int, Boolean>,
        val delivered: Map<Int, Boolean>
    )

    private fun encode(entry: Entry): String = buildString {
        append(entry.startedAt).append(';')
        append(entry.partCount).append(';')
        append(tokens(entry.sent)).append(';')
        append(tokens(entry.delivered))
    }

    private fun tokens(statuses: Map<Int, Boolean>): String =
        statuses.entries.joinToString(",") {
            "${it.key}:${if (it.value) 1 else 0}"
        }

    private fun decode(raw: String?): Entry? {
        val fields = raw?.split(';', limit = 4) ?: return null
        if (fields.size != 4) return null
        val startedAt = fields[0].toLongOrNull() ?: return null
        val partCount = fields[1].toIntOrNull() ?: return null
        val sent = parseTokens(fields[2])
        val delivered = parseTokens(fields[3])
        return Entry(startedAt, partCount, sent, delivered)
    }

    private fun parseTokens(raw: String): Map<Int, Boolean> {
        if (raw.isBlank()) return emptyMap()
        return raw.split(',').mapNotNull { token ->
            val pair = token.split(':', limit = 2)
            val index = pair.getOrNull(0)?.toIntOrNull() ?: return@mapNotNull null
            index to (pair.getOrNull(1) == "1")
        }.toMap()
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
