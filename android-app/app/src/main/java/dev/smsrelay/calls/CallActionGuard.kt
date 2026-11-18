package dev.smsrelay.calls

import android.content.Context
import org.json.JSONArray

/**
 * The local "at most once" guard for remote call actions.
 *
 * The server marks an action `delivered` in the same transaction that
 * hands it out, so two pollers never run the same one. That is not
 * enough on the phone: a report can be lost, a poller can be killed
 * between the HTTP answer and the Telecom call, and the process can be
 * restarted mid-action. The id is therefore written to disk BEFORE the
 * `Call` is touched. A redelivered id is refused, not executed again -
 * a repeated hangup is harmless, a repeated `dial` is not.
 *
 * The store is a plain ordered list of the last [MAX_IDS] ids in a
 * private preferences file. It deliberately holds nothing else: no
 * numbers, no names, no results.
 */
class CallActionGuard(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /**
     * Records [id] and answers whether the caller may execute it.
     *
     * The write is synchronous (`commit`), because the whole point is
     * that the id is durable BEFORE the next line touches the radio.
     * A write that fails means the "at most once" promise cannot be kept,
     * so the caller must not execute either.
     */
    @Synchronized
    fun claim(id: String): Claim {
        if (id.isBlank()) return Claim.UNAVAILABLE
        val ids = read() ?: return Claim.UNAVAILABLE
        if (ids.contains(id)) return Claim.DUPLICATE
        ids.add(id)
        while (ids.size > MAX_IDS) ids.removeAt(0)
        return if (write(ids)) Claim.GRANTED else Claim.UNAVAILABLE
    }

    /** True when [id] was recorded by an earlier run of the app. */
    @Synchronized
    fun contains(id: String): Boolean =
        id.isNotBlank() && read()?.contains(id) == true

    /**
     * Forgets everything.
     *
     * Only used when the device is unpaired: the ids belong to actions of
     * a server the phone no longer talks to.
     */
    @Synchronized
    fun clear() {
        runCatching { prefs.edit().remove(KEY_IDS).commit() }
    }

    /** Null when the stored list cannot be read at all. */
    private fun read(): MutableList<String>? {
        val raw = runCatching { prefs.getString(KEY_IDS, null) }.getOrNull()
        if (raw.isNullOrBlank()) return mutableListOf()
        val array = runCatching { JSONArray(raw) }.getOrNull() ?: return null
        val ids = mutableListOf<String>()
        for (index in 0 until array.length()) {
            val value = array.optString(index)
            if (value.isNotBlank()) ids.add(value)
        }
        return ids
    }

    private fun write(ids: List<String>): Boolean {
        val array = JSONArray()
        for (id in ids) array.put(id)
        return runCatching {
            prefs.edit().putString(KEY_IDS, array.toString()).commit()
        }.getOrDefault(false)
    }

    /** The answer of [claim]. */
    enum class Claim {
        /** The id is durable; the action may run. */
        GRANTED,

        /** The id was recorded before; the action already ran. */
        DUPLICATE,

        /** The id could not be persisted; nothing may run. */
        UNAVAILABLE
    }

    companion object {
        private const val PREFS_NAME = "call_action_guard"
        private const val KEY_IDS = "executed_ids"

        /** Enough to cover a long call with a keypad and a restart. */
        const val MAX_IDS = 100
    }
}
