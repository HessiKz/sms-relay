package dev.smsrelay.messages

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.SystemClock
import android.provider.Telephony
import dev.smsrelay.data.ContactsRepository
import dev.smsrelay.util.PhoneNumbers
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/** The conversation list of the phone, plus whether reading it failed. */
data class SmsThreads(
    val threads: List<SmsThread>,
    val failed: Boolean
)

/**
 * A page of one thread.
 *
 * [hasOlder] is true when the provider still holds messages older than
 * the first row of [messages]; the thread screen then shows "Load older
 * messages". It is derived from the read itself (one extra row), so it
 * never promises more than the store has.
 */
data class SmsPage(
    val messages: List<SmsMessage>,
    val hasOlder: Boolean,
    val failed: Boolean
)

/**
 * Reads the real SMS store of the phone.
 *
 * This is the read model of the local Messages client: the relay's Room
 * queue stays what it is (the upload queue) and is never rendered here.
 * Every method is a `suspend` function that hops to [Dispatchers.IO], so
 * a caller on the main thread cannot block on the provider.
 *
 * The provider is allowed to refuse: a phone where the app does not hold
 * the SMS role answers `SecurityException` (or null for a query). Both
 * are reported as an empty read plus the failure flag, never a crash. No
 * number, name or body is ever logged.
 */
object SmsRepository {

    /** The conversation list, with its snippets and read state. */
    private val CONVERSATIONS_URI: Uri = Uri.parse("content://mms-sms/conversations?simple=true")

    /** The platform's SMS table: the source of every thread. */
    private val SMS_URI: Uri = Telephony.Sms.CONTENT_URI

    /** One canonical address id, resolved to the address it stands for. */
    private const val CANONICAL_ADDRESS_BASE = "content://mms-sms/canonical-addresses"

    /** How many messages one page of a thread holds. */
    const val MESSAGE_LIMIT = 500

    /** How many rows the unread scan may touch. */
    private const val UNREAD_SCAN_LIMIT = 2000

    /** The projection of a conversation row; the order is the one used below. */
    private val CONVERSATION_PROJECTION = arrayOf(
        "_id", "date", "recipient_ids", "snippet"
    )

    /** The projection of a message row; the order is the one used below. */
    private val MESSAGE_PROJECTION = arrayOf(
        "_id", "thread_id", "address", "body", "date",
        "type", "read", "seen", "status", "subscription_id"
    )

    /**
     * How long the contact map stays valid.
     *
     * Reading the contacts means a query over up to 3000 rows, and the
     * observer can fire several times for one incoming SMS. A minute is
     * short enough that a newly added contact appears while the user is
     * still in the app, and long enough that a burst of notifications
     * costs one read. Only age invalidates it: a leftover map would
     * outlive the permission and keep showing names after it was revoked,
     * which is why the permission is checked before the cache is used.
     */
    private const val CONTACT_CACHE_MS = 60_000L

    /** The last contact map read, and when it was read. */
    private class ContactCache(val names: Map<String, String>, val readAt: Long)

    /** The one cache slot; a single volatile reference, so a reader never
     *  sees a half-updated pair of fields. */
    @Volatile
    private var contactCache: ContactCache? = null

    // ---------------------------------------------------------------- reads

    /**
     * Every conversation of the phone, newest first.
     *
     * The recipient ids of a row are resolved through
     * `canonical-addresses`, one lookup per distinct id and cached for
     * the whole call, because a thread usually names one or two
     * recipients and the same people repeat in every row.
     *
     * Every id is resolved, not just the first one: a row with two or
     * more recipients is a group, and a group must not be shown - or
     * dialled - as a private chat with whoever happened to be first.
     */
    suspend fun conversations(context: Context): SmsThreads = withContext(Dispatchers.IO) {
        val appContext = context.applicationContext
        val names = contactNames(appContext)
        val addressCache = HashMap<String, String>()
        val unread = unreadCounts(appContext)
        val threads = ArrayList<SmsThread>()
        try {
            appContext.contentResolver.query(
                CONVERSATIONS_URI,
                CONVERSATION_PROJECTION,
                null,
                null,
                "date DESC"
            ).use { cursor ->
                if (cursor == null) return@withContext SmsThreads(emptyList(), true)
                val idCol = cursor.getColumnIndex("_id")
                val dateCol = cursor.getColumnIndex("date")
                val recipientsCol = cursor.getColumnIndex("recipient_ids")
                val snippetCol = cursor.getColumnIndex("snippet")
                if (idCol < 0 || recipientsCol < 0) {
                    return@withContext SmsThreads(emptyList(), true)
                }
                while (cursor.moveToNext()) {
                    val threadId = cursor.getLong(idCol)
                    val recipients = resolveRecipients(
                        appContext,
                        cursor.getString(recipientsCol).orEmpty(),
                        addressCache
                    )
                    if (recipients.isEmpty()) continue
                    val recipientNames = recipients.map { number ->
                        names[numberKey(number)].orEmpty()
                    }
                    threads.add(
                        SmsThread(
                            threadId = threadId,
                            // A group has no single other side; keeping one
                            // member's number here would be a wrong answer
                            // to "who is this conversation with".
                            address = if (recipients.size == 1) recipients[0] else "",
                            recipients = recipients,
                            name = if (recipients.size == 1) {
                                names[numberKey(recipients[0])]
                            } else {
                                null
                            },
                            recipientNames = recipientNames,
                            snippet = if (snippetCol >= 0) {
                                cursor.getString(snippetCol).orEmpty()
                            } else {
                                ""
                            },
                            dateMillis = if (dateCol >= 0) cursor.getLong(dateCol) else 0L,
                            unread = unread[threadId] ?: 0
                        )
                    )
                }
            }
        } catch (e: SecurityException) {
            return@withContext SmsThreads(emptyList(), true)
        } catch (e: Exception) {
            return@withContext SmsThreads(emptyList(), true)
        }
        // The provider already sorts, but a row without a date would
        // otherwise sit at the wrong end of the list on some builds.
        SmsThreads(threads.sortedByDescending { it.dateMillis }, false)
    }

    /**
     * The newest [limit] messages of one thread, oldest first.
     *
     * The newest are read with `DATE DESC` and then reversed, so a thread
     * with ten thousand messages costs the same as a short one. One extra
     * row is asked for: if it arrives, older messages exist and the
     * screen may offer "Load older messages".
     */
    suspend fun messages(
        context: Context,
        threadId: Long,
        limit: Int = MESSAGE_LIMIT
    ): SmsPage = withContext(Dispatchers.IO) {
        readMessages(context, threadId, limit, null)
    }

    /**
     * The page before [oldest], for "Load older messages".
     *
     * The key is the date *and* the id: two messages of one thread can
     * share a second, and a date-only key would drop one of them.
     */
    suspend fun olderMessages(
        context: Context,
        threadId: Long,
        oldest: SmsMessage,
        limit: Int = MESSAGE_LIMIT
    ): SmsPage = withContext(Dispatchers.IO) {
        readMessages(context, threadId, limit, oldest)
    }

    private fun readMessages(
        context: Context,
        threadId: Long,
        limit: Int,
        olderThan: SmsMessage?
    ): SmsPage {
        val appContext = context.applicationContext
        val messages = ArrayList<SmsMessage>()
        val selection: String
        val args: Array<String>
        if (olderThan == null) {
            selection = "thread_id = ?"
            args = arrayOf(threadId.toString())
        } else {
            selection = "thread_id = ? AND (date < ? OR (date = ? AND _id < ?))"
            args = arrayOf(
                threadId.toString(),
                olderThan.dateMillis.toString(),
                olderThan.dateMillis.toString(),
                olderThan.id.toString()
            )
        }
        try {
            appContext.contentResolver.query(
                SMS_URI,
                MESSAGE_PROJECTION,
                selection,
                args,
                "date DESC, _id DESC LIMIT ${limit + 1}"
            ).use { cursor ->
                if (cursor == null) return SmsPage(emptyList(), false, true)
                val idCol = cursor.getColumnIndex("_id")
                val threadCol = cursor.getColumnIndex("thread_id")
                val addressCol = cursor.getColumnIndex("address")
                val bodyCol = cursor.getColumnIndex("body")
                val dateCol = cursor.getColumnIndex("date")
                val typeCol = cursor.getColumnIndex("type")
                val readCol = cursor.getColumnIndex("read")
                val seenCol = cursor.getColumnIndex("seen")
                val statusCol = cursor.getColumnIndex("status")
                val subCol = cursor.getColumnIndex("subscription_id")
                while (cursor.moveToNext()) {
                    messages.add(
                        SmsMessage(
                            id = if (idCol >= 0) cursor.getLong(idCol) else 0L,
                            threadId = if (threadCol >= 0) {
                                cursor.getLong(threadCol)
                            } else {
                                threadId
                            },
                            address = if (addressCol >= 0) {
                                cursor.getString(addressCol).orEmpty()
                            } else {
                                ""
                            },
                            body = if (bodyCol >= 0) {
                                cursor.getString(bodyCol).orEmpty()
                            } else {
                                ""
                            },
                            dateMillis = if (dateCol >= 0) cursor.getLong(dateCol) else 0L,
                            type = SmsType.fromProvider(
                                if (typeCol >= 0) cursor.getInt(typeCol) else 0
                            ),
                            read = readCol >= 0 && cursor.getInt(readCol) == 1,
                            seen = seenCol >= 0 && cursor.getInt(seenCol) == 1,
                            status = if (statusCol >= 0) cursor.getInt(statusCol) else 0,
                            subId = if (subCol >= 0) cursor.getInt(subCol) else -1
                        )
                    )
                }
            }
        } catch (e: SecurityException) {
            return SmsPage(emptyList(), false, true)
        } catch (e: Exception) {
            return SmsPage(emptyList(), false, true)
        }
        // The extra row is evidence of older messages, not content.
        val hasOlder = messages.size > limit
        if (hasOlder) messages.removeAt(messages.size - 1)
        messages.reverse()
        return SmsPage(messages, hasOlder, false)
    }

    /**
     * The thread of one address, or -1 when the phone has none yet.
     *
     * A picked contact starts the thread screen with a number and no
     * thread id, because the platform only creates a thread when a
     * message is stored. This lookup keeps that start read-only: it
     * searches the existing conversations instead of asking the provider
     * to create one, so opening a chat never writes to the SMS store.
     *
     * Only a one to one thread can match. A group that happens to contain
     * the address is skipped, because opening it from a contact picker
     * would show the whole group while the user asked for that person.
     */
    suspend fun threadIdForAddress(context: Context, address: String): Long =
        withContext(Dispatchers.IO) {
            val wanted = numberKey(address)
            if (wanted.isEmpty()) return@withContext -1L
            val appContext = context.applicationContext
            val cache = HashMap<String, String>()
            try {
                appContext.contentResolver.query(
                    CONVERSATIONS_URI,
                    arrayOf("_id", "recipient_ids"),
                    null,
                    null,
                    "date DESC"
                ).use { cursor ->
                    if (cursor == null) return@withContext -1L
                    val idCol = cursor.getColumnIndex("_id")
                    val recipientsCol = cursor.getColumnIndex("recipient_ids")
                    if (idCol < 0 || recipientsCol < 0) return@withContext -1L
                    while (cursor.moveToNext()) {
                        val recipients = resolveRecipients(
                            appContext,
                            cursor.getString(recipientsCol).orEmpty(),
                            cache
                        )
                        if (recipients.size != 1) continue
                        if (numberKey(recipients[0]) == wanted) {
                            return@withContext cursor.getLong(idCol)
                        }
                    }
                }
            } catch (e: Exception) {
                return@withContext -1L
            }
            -1L
        }

    /**
     * One conversation row by id, or null when it no longer exists.
     *
     * The thread screen is started from the list, but it can also be
     * recreated by the platform. Reading the row again means the screen
     * always knows whether it is a group and what the provider's snippet
     * says, instead of trusting what it was launched with.
     */
    suspend fun threadFor(context: Context, threadId: Long): SmsThread? =
        withContext(Dispatchers.IO) {
            if (threadId <= 0L) return@withContext null
            val appContext = context.applicationContext
            val names = contactNames(appContext)
            val cache = HashMap<String, String>()
            try {
                appContext.contentResolver.query(
                    CONVERSATIONS_URI,
                    CONVERSATION_PROJECTION,
                    "_id = ?",
                    arrayOf(threadId.toString()),
                    null
                ).use { cursor ->
                    if (cursor == null || !cursor.moveToFirst()) return@withContext null
                    val recipientsCol = cursor.getColumnIndex("recipient_ids")
                    if (recipientsCol < 0) return@withContext null
                    val recipients = resolveRecipients(
                        appContext,
                        cursor.getString(recipientsCol).orEmpty(),
                        cache
                    )
                    if (recipients.isEmpty()) return@withContext null
                    val dateCol = cursor.getColumnIndex("date")
                    val snippetCol = cursor.getColumnIndex("snippet")
                    SmsThread(
                        threadId = threadId,
                        address = if (recipients.size == 1) recipients[0] else "",
                        recipients = recipients,
                        name = if (recipients.size == 1) {
                            names[numberKey(recipients[0])]
                        } else {
                            null
                        },
                        recipientNames = recipients.map { number ->
                            names[numberKey(number)].orEmpty()
                        },
                        snippet = if (snippetCol >= 0) {
                            cursor.getString(snippetCol).orEmpty()
                        } else {
                            ""
                        },
                        dateMillis = if (dateCol >= 0) cursor.getLong(dateCol) else 0L,
                        unread = 0
                    )
                }
            } catch (e: Exception) {
                null
            }
        }

    // --------------------------------------------------------------- writes

    /**
     * The row just written to the provider, or why there is none.
     *
     * A `null` [rowId] is not a send failure: the radio may have accepted
     * the message while the store refused the copy, which is exactly the
     * case the screen has to report honestly instead of drawing a bubble
     * that does not exist.
     */
    data class StoredSms(
        /** `_ID` of the inserted row, or null when the store refused it. */
        val rowId: Long?,
        /** The thread the row landed in, when the provider reports it. */
        val threadId: Long
    )

    /**
     * Appends a sent message to the phone's SMS store.
     *
     * Only the plain `content://sms` table is written: no `MmsSms`
     * internals, no thread creation by hand. The provider assigns the
     * thread from the address and the conversation appears in the list
     * by itself, which is the same thing any other messaging app does.
     *
     * [sentAtMillis] is `DATE`/`DATE_SENT`: the moment the radio accepted
     * the message, not the moment the row was written, so the bubble sorts
     * where the user expects it. `READ`/`SEEN` are 1 because a message
     * this phone sent is never unread to its owner.
     *
     * A phone where the app does not hold the SMS role answers
     * [SecurityException]; that is returned as an empty row, never thrown
     * and never logged with the number or the body.
     */
    suspend fun insertSent(
        context: Context,
        address: String,
        body: String,
        sentAtMillis: Long,
        subId: Int
    ): StoredSms = withContext(Dispatchers.IO) {
        val target = address.trim()
        if (target.isEmpty() || body.isBlank()) return@withContext StoredSms(null, -1L)
        val values = ContentValues().apply {
            put(Telephony.Sms.ADDRESS, target)
            put(Telephony.Sms.BODY, body)
            put(Telephony.Sms.DATE, sentAtMillis)
            put(Telephony.Sms.DATE_SENT, sentAtMillis)
            put(Telephony.Sms.TYPE, Telephony.Sms.MESSAGE_TYPE_SENT)
            put(Telephony.Sms.READ, 1)
            put(Telephony.Sms.SEEN, 1)
            if (subId > 0) put(Telephony.Sms.SUBSCRIPTION_ID, subId)
        }
        val uri = try {
            context.applicationContext.contentResolver.insert(SMS_URI, values)
        } catch (e: SecurityException) {
            null
        } catch (e: Exception) {
            null
        } ?: return@withContext StoredSms(null, -1L)
        val rowId = uri.lastPathSegment?.toLongOrNull()
        StoredSms(rowId, if (rowId != null) threadIdOfRow(context, rowId) else -1L)
    }

    /** The `THREAD_ID` of one row, or -1 when the provider will not say. */
    private fun threadIdOfRow(context: Context, rowId: Long): Long = try {
        context.contentResolver.query(
            SMS_URI,
            arrayOf("thread_id"),
            "_id = ?",
            arrayOf(rowId.toString()),
            null
        ).use { cursor ->
            if (cursor != null && cursor.moveToFirst()) {
                val column = cursor.getColumnIndex("thread_id")
                if (column >= 0) cursor.getLong(column) else -1L
            } else {
                -1L
            }
        }
    } catch (e: Exception) {
        -1L
    }

    /**
     * Marks every message of one thread read and seen.
     *
     * Returns how many rows the provider changed. A phone that does not
     * let this app write the SMS store answers [SecurityException]; that
     * is not an error the user has to see, so it is reported as 0 and
     * the unread dot simply stays until the role is granted.
     */
    suspend fun markThreadRead(context: Context, threadId: Long): Int =
        withContext(Dispatchers.IO) {
            val values = ContentValues().apply {
                put(Telephony.Sms.READ, 1)
                put(Telephony.Sms.SEEN, 1)
            }
            try {
                context.applicationContext.contentResolver.update(
                    SMS_URI,
                    values,
                    "thread_id = ? AND (read = 0 OR seen = 0)",
                    arrayOf(threadId.toString())
                )
            } catch (e: SecurityException) {
                0
            } catch (e: Exception) {
                0
            }
        }

    // -------------------------------------------------------------- helpers

    /**
     * Every address behind a `RECIPIENT_IDS` value, in provider order.
     *
     * `RECIPIENT_IDS` is a space separated list of canonical address ids.
     * All of them are resolved, because the count is what tells a private
     * conversation from a group; an id that does not resolve is dropped
     * instead of being replaced by a guess. The cache is per call, so a
     * list of two hundred threads does not resolve the same id twice.
     */
    fun resolveRecipients(
        context: Context,
        recipientIds: String,
        cache: MutableMap<String, String>
    ): List<String> {
        val ids = recipientIds.split(' ', '\t', ',')
            .map { it.trim() }
            .filter { it.isNotEmpty() }
        if (ids.isEmpty()) return emptyList()
        val addresses = ArrayList<String>(ids.size)
        for (id in ids) {
            val address = if (cache.containsKey(id)) {
                cache[id].orEmpty()
            } else {
                queryCanonicalAddress(context, id).also { cache[id] = it }
            }
            if (address.isEmpty()) continue
            if (!addresses.contains(address)) addresses.add(address)
        }
        return addresses
    }

    private fun queryCanonicalAddress(context: Context, id: String): String {
        if (id.toLongOrNull() == null) return ""
        return try {
            context.contentResolver.query(
                Uri.parse("$CANONICAL_ADDRESS_BASE/$id"),
                arrayOf("address"),
                null,
                null,
                null
            ).use { cursor ->
                if (cursor != null && cursor.moveToFirst()) {
                    val column = cursor.getColumnIndex("address")
                    if (column >= 0) cursor.getString(column).orEmpty() else ""
                } else {
                    ""
                }
            }
        } catch (e: Exception) {
            ""
        }
    }

    /**
     * The address of one thread, used when the screen only has a thread
     * id (a restored screen, a link from another app).
     *
     * Empty for a group: a group has no single address, and returning one
     * member's number would let the caller dial that person privately.
     */
    suspend fun addressForThread(context: Context, threadId: Long): String =
        withContext(Dispatchers.IO) {
            try {
                context.applicationContext.contentResolver.query(
                    CONVERSATIONS_URI,
                    arrayOf("_id", "recipient_ids"),
                    "_id = ?",
                    arrayOf(threadId.toString()),
                    null
                ).use { cursor ->
                    if (cursor == null || !cursor.moveToFirst()) return@withContext ""
                    val column = cursor.getColumnIndex("recipient_ids")
                    if (column < 0) return@withContext ""
                    val recipients = resolveRecipients(
                        context.applicationContext,
                        cursor.getString(column).orEmpty(),
                        HashMap()
                    )
                    if (recipients.size == 1) recipients[0] else ""
                }
            } catch (e: Exception) {
                ""
            }
        }

    /**
     * Unread messages per thread, counted in one query.
     *
     * Only inbox rows count: a sent message is read by definition, and a
     * failed one is reported by its own bubble. Bounded by
     * [UNREAD_SCAN_LIMIT] so a phone that never marked anything read
     * cannot make the list slow.
     */
    private fun unreadCounts(context: Context): Map<Long, Int> {
        val counts = HashMap<Long, Int>()
        try {
            context.contentResolver.query(
                SMS_URI,
                arrayOf("thread_id"),
                "read = 0 AND type = ?",
                arrayOf(Telephony.Sms.MESSAGE_TYPE_INBOX.toString()),
                "_id DESC LIMIT $UNREAD_SCAN_LIMIT"
            ).use { cursor ->
                if (cursor == null) return emptyMap()
                val column = cursor.getColumnIndex("thread_id")
                if (column < 0) return emptyMap()
                while (cursor.moveToNext()) {
                    val threadId = cursor.getLong(column)
                    counts[threadId] = (counts[threadId] ?: 0) + 1
                }
            }
        } catch (e: Exception) {
            return emptyMap()
        }
        return counts
    }

    /**
     * "number -> name" for the conversations on the screen.
     *
     * Keyed by the normalised number, so "+98 912 …" and "0912 …" find
     * the same person. Without the contacts permission the answer is an
     * empty map and the rows show numbers, which is correct rather than
     * broken; the permission is re-checked before the cache is used, so
     * revoking it takes effect at once instead of after a minute.
     *
     * The read itself is cached for [CONTACT_CACHE_MS]: it costs a query
     * over up to 3000 rows, and the observer can fire several times for
     * one incoming message.
     */
    private fun contactNames(context: Context): Map<String, String> {
        if (!ContactsRepository.hasPermission(context)) {
            contactCache = null
            return emptyMap()
        }
        val now = SystemClock.elapsedRealtime()
        contactCache?.let { cached ->
            if (now - cached.readAt < CONTACT_CACHE_MS) return cached.names
        }
        val names = readContactNames(context)
        contactCache = ContactCache(names, now)
        return names
    }

    /** The uncached read: contacts to a "normalised number -> name" map. */
    private fun readContactNames(context: Context): Map<String, String> {
        val contacts = try {
            ContactsRepository.readContacts(context)
        } catch (e: Exception) {
            null
        } ?: return emptyMap()
        val names = HashMap<String, String>(contacts.size * 2)
        for (contact in contacts) {
            val key = numberKey(contact.number)
            if (key.isEmpty()) continue
            if (!names.containsKey(key)) names[key] = contact.name
        }
        return names
    }

    /**
     * The contact name of one address, or null when it is not a contact.
     *
     * The thread screen uses it when it was started with a bare number
     * (a picked contact, a restored screen) and has no row of the
     * conversation list to take the name from.
     */
    suspend fun nameForAddress(context: Context, address: String): String? =
        withContext(Dispatchers.IO) {
            val key = numberKey(address)
            if (key.isEmpty()) return@withContext null
            contactNames(context.applicationContext)[key]?.takeIf { it.isNotBlank() }
        }

    /** A number in the one form both sides of a lookup agree on. */
    fun numberKey(raw: String): String {
        val normalized = try {
            PhoneNumbers.normalize(raw)
        } catch (e: Exception) {
            null
        } ?: return raw.trim()
        return normalized.filter { it.isDigit() || it == '+' }
            .ifEmpty { raw.trim() }
    }

    /** What a row shows for a number: grouped digits, or the raw value. */
    fun displayNumber(raw: String): String {
        val text = raw.trim()
        if (text.isEmpty()) return ""
        // A short code or an alphanumeric sender is already what the user
        // sees in their old messaging app.
        if (text.any { it.isLetter() } || text.length < 5) return text
        return try {
            val formatted = android.telephony.PhoneNumberUtils.formatNumber(text)
            formatted?.trim().orEmpty().ifEmpty { text }
        } catch (e: Exception) {
            text
        }
    }
}
