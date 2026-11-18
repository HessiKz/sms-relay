package dev.smsrelay.calls

import android.content.Context
import android.provider.CallLog
import java.security.MessageDigest

/**
 * Reads the phone's call log.
 *
 * Two different reads share this class, because both must agree on the
 * mapping and the hash:
 *
 *  - [page] walks the log forwards with an `_ID` cursor, oldest first;
 *  - [recent] re-reads the last days by `DATE`, because the provider
 *    rewrites `DURATION`, `TYPE` and `IS_READ` when a call ends or the
 *    user opens the log.
 *
 * The reader never logs a number or a name. Everything it returns is a
 * plain value object.
 */
object CallLogReader {

    /** More than the call log ever returns in one page. */
    private const val MAX_ROWS = 100

    /**
     * One page of the log, oldest first.
     *
     * [rows] is how many provider rows were seen, [lastId] the highest
     * `_ID` of them. When a row cannot be used (a null id) it is skipped
     * and counted in [rows] but not in [calls], exactly like the SMS
     * import does, so the cursor never gets stuck on it.
     */
    data class Page(
        val rows: Int,
        val lastId: Long,
        val calls: List<CallLogEntity>
    )

    /**
     * Rows after [cursor], oldest first, at most [limit].
     *
     * The limit is part of the sort order because not every vendor
     * provider honours a limit clause; [MAX_ROWS] caps the read anyway.
     */
    fun page(context: Context, cursor: Long, limit: Int = MAX_ROWS): Page {
        val size = limit.coerceIn(1, MAX_ROWS)
        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
            CallLog.Calls.TYPE,
            "subscription_id",
            CallLog.Calls.PHONE_ACCOUNT_ID,
            CallLog.Calls.IS_READ
        )
        val result = context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            "${CallLog.Calls._ID} > ?",
            arrayOf(cursor.toString()),
            "${CallLog.Calls._ID} ASC LIMIT $size"
        ) ?: throw IllegalStateException("call log unavailable")

        var rows = 0
        var lastId = cursor
        val calls = mutableListOf<CallLogEntity>()
        result.use { query ->
            val idIndex = query.getColumnIndex(CallLog.Calls._ID)
            if (idIndex < 0) return Page(0, cursor, emptyList())
            val numberIndex = query.getColumnIndex(CallLog.Calls.NUMBER)
            val nameIndex = query.getColumnIndex(CallLog.Calls.CACHED_NAME)
            val dateIndex = query.getColumnIndex(CallLog.Calls.DATE)
            val durationIndex = query.getColumnIndex(CallLog.Calls.DURATION)
            val typeIndex = query.getColumnIndex(CallLog.Calls.TYPE)
            val subIndex = query.getColumnIndex("subscription_id")
            val accountIndex = query.getColumnIndex(CallLog.Calls.PHONE_ACCOUNT_ID)
            val readIndex = query.getColumnIndex(CallLog.Calls.IS_READ)
            while (rows < size && query.moveToNext()) {
                rows += 1
                val id = query.getLong(idIndex)
                if (id <= lastId) continue
                lastId = id
                calls.add(
                    build(
                        providerId = id,
                        number = if (numberIndex >= 0) query.getString(numberIndex) else null,
                        cachedName = if (nameIndex >= 0) query.getString(nameIndex) else null,
                        date = if (dateIndex >= 0) query.getLong(dateIndex) else 0L,
                        duration = if (durationIndex >= 0) query.getInt(durationIndex) else 0,
                        rawType = if (typeIndex >= 0) query.getInt(typeIndex) else 0,
                        subId = if (subIndex >= 0) query.getInt(subIndex) else 0,
                        account = if (accountIndex >= 0) query.getString(accountIndex) else null,
                        isRead = if (readIndex >= 0) query.getInt(readIndex) != 0 else false
                    )
                )
            }
        }
        return Page(rows, lastId, calls)
    }

    /**
     * The calls of the last [windowDays] days, newest first, at most
     * [limit] rows.
     *
     * The window exists because a call row is written when the call
     * ENDS: the duration of a missed or rejected call is only final
     * after the fact, and the user opening the log marks it read. One
     * forward cursor alone would keep the first, wrong version.
     */
    fun recent(
        context: Context,
        windowDays: Int = RECENT_WINDOW_DAYS,
        limit: Int = RECENT_LIMIT
    ): List<CallLogEntity> {
        val since = System.currentTimeMillis() - windowDays.toLong() * 24L * 60L * 60L * 1000L
        val projection = arrayOf(
            CallLog.Calls._ID,
            CallLog.Calls.NUMBER,
            CallLog.Calls.CACHED_NAME,
            CallLog.Calls.DATE,
            CallLog.Calls.DURATION,
            CallLog.Calls.TYPE,
            "subscription_id",
            CallLog.Calls.PHONE_ACCOUNT_ID,
            CallLog.Calls.IS_READ
        )
        val result = context.contentResolver.query(
            CallLog.Calls.CONTENT_URI,
            projection,
            "${CallLog.Calls.DATE} >= ?",
            arrayOf(since.toString()),
            "${CallLog.Calls.DATE} DESC LIMIT ${limit.coerceIn(1, 200)}"
        ) ?: throw IllegalStateException("call log unavailable")

        val calls = mutableListOf<CallLogEntity>()
        result.use { query ->
            val idIndex = query.getColumnIndex(CallLog.Calls._ID)
            if (idIndex < 0) return emptyList()
            val numberIndex = query.getColumnIndex(CallLog.Calls.NUMBER)
            val nameIndex = query.getColumnIndex(CallLog.Calls.CACHED_NAME)
            val dateIndex = query.getColumnIndex(CallLog.Calls.DATE)
            val durationIndex = query.getColumnIndex(CallLog.Calls.DURATION)
            val typeIndex = query.getColumnIndex(CallLog.Calls.TYPE)
            val subIndex = query.getColumnIndex("subscription_id")
            val accountIndex = query.getColumnIndex(CallLog.Calls.PHONE_ACCOUNT_ID)
            val readIndex = query.getColumnIndex(CallLog.Calls.IS_READ)
            while (query.moveToNext()) {
                val id = query.getLong(idIndex)
                if (id <= 0L) continue
                calls.add(
                    build(
                        providerId = id,
                        number = if (numberIndex >= 0) query.getString(numberIndex) else null,
                        cachedName = if (nameIndex >= 0) query.getString(nameIndex) else null,
                        date = if (dateIndex >= 0) query.getLong(dateIndex) else 0L,
                        duration = if (durationIndex >= 0) query.getInt(durationIndex) else 0,
                        rawType = if (typeIndex >= 0) query.getInt(typeIndex) else 0,
                        subId = if (subIndex >= 0) query.getInt(subIndex) else 0,
                        account = if (accountIndex >= 0) query.getString(accountIndex) else null,
                        isRead = if (readIndex >= 0) query.getInt(readIndex) != 0 else false
                    )
                )
            }
        }
        return calls
    }

    /**
     * The kind of call a raw provider type means.
     *
     * `ANSWERED_EXTERNALLY` is a call the user answered somewhere else
     * (a car kit, a headset): for the relay it is an incoming call. An
     * unknown vendor value stays [CallLogEntity.KIND_UNKNOWN] instead of
     * being guessed.
     */
    fun kindOf(rawType: Int): String = when (rawType) {
        CallLog.Calls.INCOMING_TYPE -> CallLogEntity.KIND_INCOMING
        CallLog.Calls.OUTGOING_TYPE -> CallLogEntity.KIND_OUTGOING
        CallLog.Calls.MISSED_TYPE -> CallLogEntity.KIND_MISSED
        CallLog.Calls.REJECTED_TYPE -> CallLogEntity.KIND_REJECTED
        CallLog.Calls.BLOCKED_TYPE -> CallLogEntity.KIND_BLOCKED
        CallLog.Calls.VOICEMAIL_TYPE -> CallLogEntity.KIND_VOICEMAIL
        CallLog.Calls.ANSWERED_EXTERNALLY_TYPE -> CallLogEntity.KIND_INCOMING
        else -> CallLogEntity.KIND_UNKNOWN
    }

    /**
     * sha256 over the fields that make a call row unique.
     *
     * The provider id is part of it, so two calls to the same number in
     * the same second stay two rows. The hash is what the server uses to
     * decide "unchanged" versus "updated", so it must change only when
     * something the server stores changes.
     */
    fun contentHash(
        providerId: Long,
        number: String,
        date: Long,
        duration: Int,
        rawType: Int,
        subId: Int,
        isRead: Boolean
    ): String {
        val material = buildString {
            append(providerId).append('|')
            append(number).append('|')
            append(date).append('|')
            append(duration).append('|')
            append(rawType).append('|')
            append(subId).append('|')
            append(if (isRead) 1 else 0)
        }
        return sha256(material)
    }

    /** The local primary key of a row before the server has seen it. */
    fun localId(providerId: Long): String = "local-$providerId"

    private fun build(
        providerId: Long,
        number: String?,
        cachedName: String?,
        date: Long,
        duration: Int,
        rawType: Int,
        subId: Int,
        account: String?,
        isRead: Boolean
    ): CallLogEntity {
        // A private number is stored as an empty string, never as null:
        // the design contract keeps those rows out of a thread.
        val cleanNumber = number?.trim().orEmpty()
        val cleanName = cachedName?.trim()?.takeIf { it.isNotEmpty() }
        val seconds = epochSeconds(date)
        val hash = contentHash(
            providerId = providerId,
            number = cleanNumber,
            date = date,
            duration = duration,
            rawType = rawType,
            subId = subId,
            isRead = isRead
        )
        return CallLogEntity(
            id = localId(providerId),
            providerId = providerId,
            number = cleanNumber,
            cachedName = cleanName,
            kind = kindOf(rawType),
            rawType = rawType,
            startedAt = seconds,
            durationS = if (duration > 0) duration else 0,
            subId = subId.coerceIn(0, 99),
            phoneAccount = account?.trim()?.takeIf { it.isNotEmpty() },
            isRead = isRead,
            contentHash = hash
        )
    }

    /** The provider stores millis; the server wants seconds. */
    private fun epochSeconds(value: Long): Long =
        if (value > 100_000_000_000L) value / 1000L else value

    private fun sha256(value: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
        val bytes = digest.digest(value.toByteArray(Charsets.UTF_8))
        val builder = StringBuilder(bytes.size * 2)
        for (byte in bytes) {
            builder.append(HEX[(byte.toInt() shr 4) and 0x0f])
            builder.append(HEX[byte.toInt() and 0x0f])
        }
        return builder.toString()
    }

    private val HEX = "0123456789abcdef".toCharArray()

    /** The rolling window: seven days, as the design contract asks. */
    const val RECENT_WINDOW_DAYS = 7

    /** At most this many recent rows per run. */
    const val RECENT_LIMIT = 200
}
