package dev.smsrelay.ui.dialer

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.provider.CallLog
import android.provider.ContactsContract
import android.telecom.TelecomManager
import androidx.core.content.ContextCompat
import dev.smsrelay.sms.SimCards
import dev.smsrelay.util.PhoneNumbers
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The reads behind the Phone home: recent calls, contacts, favorites and
 * the voicemail number.
 *
 * Every query runs on [Dispatchers.IO] and returns plain value objects.
 * A permission the user did not grant is reported as [Load.denied], never
 * as an empty list, because "the phone has no calls" and "this app may
 * not look" are two different states for the screen.
 *
 * Nothing here logs a number or a name. The repository is deliberately
 * read only for the contact list: the star and the name are shown, never
 * written back.
 */
object DialerRepository {

    /** The newest rows the Recents tab asks the provider for. */
    const val RECENTS_LIMIT = 200

    /** A guard for a contact list larger than any phone really has. */
    private const val MAX_CONTACTS = 5000

    /** Favorites are few; the number query is bounded the same way. */
    private const val MAX_FAVORITES = 60

    /** What a call log row means, after the provider's type is mapped. */
    enum class CallKind { INCOMING, OUTGOING, MISSED, REJECTED, BLOCKED, VOICEMAIL, OTHER }

    /**
     * One row of the call log.
     *
     * [number] may be empty: a private number is stored that way. The
     * row is still shown, with the "Private number" label.
     */
    data class RecentCall(
        val id: Long,
        val number: String,
        val cachedName: String?,
        val dateMillis: Long,
        val durationSeconds: Int,
        val kind: CallKind,
        val simLabel: String?,
        val isRead: Boolean
    )

    /** One number of one contact. */
    data class DialerContact(
        val contactId: Long,
        val lookupKey: String?,
        val name: String,
        val number: String,
        val starred: Boolean
    )

    /** A starred contact with the first number it has. */
    data class FavoriteContact(
        val contactId: Long,
        val name: String,
        val number: String
    )

    /**
     * The answer of one read.
     *
     * [failed] is a provider that refused or broke, [denied] a permission
     * the user did not grant. Both are reported; an empty [items] alone
     * never is, because an empty list is a normal answer.
     */
    data class Load<T>(
        val items: List<T>,
        val failed: Boolean = false,
        val denied: Boolean = false
    )

    // -------------------------------------------------------- permissions

    fun canReadCallLog(context: Context): Boolean =
        granted(context, Manifest.permission.READ_CALL_LOG)

    fun canWriteCallLog(context: Context): Boolean =
        granted(context, Manifest.permission.WRITE_CALL_LOG)

    fun canReadContacts(context: Context): Boolean =
        granted(context, Manifest.permission.READ_CONTACTS)

    private fun granted(context: Context, permission: String): Boolean =
        ContextCompat.checkSelfPermission(context, permission) ==
            PackageManager.PERMISSION_GRANTED

    // ------------------------------------------------------------ recents

    /**
     * The newest [limit] calls, newest first.
     *
     * The projection, the order and the limit are the contract of this
     * screen: `DATE DESC LIMIT n`. The provider rewrites `DURATION`,
     * `TYPE` and `IS_READ` when a call ends and when the user opens the
     * system log, so nothing about a row is cached here.
     */
    suspend fun recents(
        context: Context,
        limit: Int = RECENTS_LIMIT
    ): Load<RecentCall> = withContext(Dispatchers.IO) {
        if (!canReadCallLog(context)) {
            return@withContext Load<RecentCall>(emptyList(), denied = true)
        }
        val size = limit.coerceIn(1, RECENTS_LIMIT)
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
        // The limit is part of the sort order: not every vendor provider
        // honours a limit clause, and the cap is applied again below.
        val attempt = runCatching {
            context.contentResolver.query(
                CallLog.Calls.CONTENT_URI,
                projection,
                null,
                null,
                "${CallLog.Calls.DATE} DESC LIMIT $size"
            )
        }
        // The grant can be taken away between the check above and the
        // query; the provider then throws instead of answering, and that
        // is a permission state, not a broken log.
        if (attempt.exceptionOrNull() is SecurityException) {
            return@withContext Load<RecentCall>(emptyList(), denied = true)
        }
        val cursor = attempt.getOrNull()
            ?: return@withContext Load<RecentCall>(emptyList(), failed = true)

        // One read of the SIM list for the whole page, so a phone with a
        // single card shows no label at all instead of "SIM 1" on every
        // row.
        val cards = runCatching { SimCards.active(context) }.getOrDefault(emptyList())
        val labelSims = cards.size > 1

        val rows = mutableListOf<RecentCall>()
        cursor.use { query ->
            val idIndex = query.getColumnIndex(CallLog.Calls._ID)
            if (idIndex < 0) return@withContext Load<RecentCall>(emptyList(), failed = true)
            val numberIndex = query.getColumnIndex(CallLog.Calls.NUMBER)
            val nameIndex = query.getColumnIndex(CallLog.Calls.CACHED_NAME)
            val dateIndex = query.getColumnIndex(CallLog.Calls.DATE)
            val durationIndex = query.getColumnIndex(CallLog.Calls.DURATION)
            val typeIndex = query.getColumnIndex(CallLog.Calls.TYPE)
            val subIndex = query.getColumnIndex("subscription_id")
            val readIndex = query.getColumnIndex(CallLog.Calls.IS_READ)
            while (query.moveToNext() && rows.size < size) {
                val id = query.getLong(idIndex)
                if (id <= 0L) continue
                val subscriptionId = if (subIndex >= 0) query.getInt(subIndex) else -1
                val card = if (labelSims && subscriptionId >= 0) {
                    cards.firstOrNull { it.subscriptionId == subscriptionId }
                } else {
                    null
                }
                rows.add(
                    RecentCall(
                        id = id,
                        number = if (numberIndex >= 0) {
                            query.getString(numberIndex)?.trim().orEmpty()
                        } else {
                            ""
                        },
                        cachedName = if (nameIndex >= 0) {
                            query.getString(nameIndex)?.trim()?.takeIf { it.isNotEmpty() }
                        } else {
                            null
                        },
                        dateMillis = if (dateIndex >= 0) query.getLong(dateIndex) else 0L,
                        durationSeconds = if (durationIndex >= 0) {
                            query.getInt(durationIndex).coerceAtLeast(0)
                        } else {
                            0
                        },
                        kind = if (typeIndex >= 0) {
                            kindOf(query.getInt(typeIndex))
                        } else {
                            CallKind.OTHER
                        },
                        simLabel = card?.let { SimCards.label(context, it) },
                        isRead = if (readIndex >= 0) query.getInt(readIndex) != 0 else false
                    )
                )
            }
        }
        Load<RecentCall>(rows)
    }

    /**
     * The kind of a raw provider type.
     *
     * `ANSWERED_EXTERNALLY` is a call the user took somewhere else (a car
     * kit, a headset) and reads as incoming. An unknown vendor value
     * stays [CallKind.OTHER] instead of being guessed.
     */
    private fun kindOf(rawType: Int): CallKind = when (rawType) {
        CallLog.Calls.INCOMING_TYPE -> CallKind.INCOMING
        CallLog.Calls.OUTGOING_TYPE -> CallKind.OUTGOING
        CallLog.Calls.MISSED_TYPE -> CallKind.MISSED
        CallLog.Calls.REJECTED_TYPE -> CallKind.REJECTED
        CallLog.Calls.BLOCKED_TYPE -> CallKind.BLOCKED
        CallLog.Calls.VOICEMAIL_TYPE -> CallKind.VOICEMAIL
        CallLog.Calls.ANSWERED_EXTERNALLY_TYPE -> CallKind.INCOMING
        else -> CallKind.OTHER
    }

    /** Deletes one call log row; false when the permission is missing. */
    suspend fun deleteCall(context: Context, id: Long): Boolean = withContext(Dispatchers.IO) {
        if (!canWriteCallLog(context)) return@withContext false
        runCatching {
            context.contentResolver.delete(
                ContentUris.withAppendedId(CallLog.Calls.CONTENT_URI, id),
                null,
                null
            )
        }.getOrDefault(0) > 0
    }

    // ----------------------------------------------------------- contacts

    /**
     * Every number of every contact, one row per number.
     *
     * The same number stored twice (a work and a private entry) is kept
     * once, first name wins, and the order is the contact name. The star
     * comes from a second, tiny query on the contacts table rather than
     * from a `starred` column of the phone view: the contacts table is
     * the one place the column is guaranteed to exist.
     */
    suspend fun contacts(context: Context): Load<DialerContact> = withContext(Dispatchers.IO) {
        if (!canReadContacts(context)) {
            return@withContext Load<DialerContact>(emptyList(), denied = true)
        }
        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER,
            ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.Contacts.LOOKUP_KEY
        )
        val attempt = runCatching {
            context.contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                projection,
                null,
                null,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + " ASC"
            )
        }
        if (attempt.exceptionOrNull() is SecurityException) {
            return@withContext Load<DialerContact>(emptyList(), denied = true)
        }
        val cursor = attempt.getOrNull()
            ?: return@withContext Load<DialerContact>(emptyList(), failed = true)

        val starred = starredContactIds(context)
        val rows = mutableListOf<DialerContact>()
        val seen = HashSet<String>()
        cursor.use { query ->
            val nameIndex = query.getColumnIndex(
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME
            )
            val numberIndex = query.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
            if (nameIndex < 0 || numberIndex < 0) {
                return@withContext Load<DialerContact>(emptyList(), failed = true)
            }
            val idIndex = query.getColumnIndex(ContactsContract.CommonDataKinds.Phone.CONTACT_ID)
            val keyIndex = query.getColumnIndex(ContactsContract.Contacts.LOOKUP_KEY)
            while (query.moveToNext() && rows.size < MAX_CONTACTS) {
                val name = query.getString(nameIndex)?.trim().orEmpty()
                val number = query.getString(numberIndex)?.trim().orEmpty()
                if (name.isEmpty() || number.isEmpty()) continue
                // Two spellings of one number are one row: normalising is
                // what makes "+98 912…" and "0912…" collapse.
                val key = PhoneNumbers.normalize(number) ?: number.filter { it.isDigit() }
                if (key.isEmpty() || !seen.add(key)) continue
                val contactId = if (idIndex >= 0) query.getLong(idIndex) else 0L
                rows.add(
                    DialerContact(
                        contactId = contactId,
                        lookupKey = if (keyIndex >= 0) query.getString(keyIndex) else null,
                        name = name,
                        number = number,
                        starred = contactId in starred
                    )
                )
            }
        }
        // The provider's collation is not the user's: sorting the small
        // in-memory page by folded name keeps "ali" and "Ali" together.
        Load<DialerContact>(rows.sortedWith(compareBy({ it.name.lowercase() }, { it.name })))
    }

    /**
     * The ids of the starred contacts, or an empty set.
     *
     * A failure here is not worth a screen of its own: the star is a
     * decoration, so the list simply shows no star and the Favorites tab
     * reports its own state.
     */
    private fun starredContactIds(context: Context): Set<Long> {
        val projection = arrayOf(ContactsContract.Contacts._ID)
        val cursor = runCatching {
            context.contentResolver.query(
                ContactsContract.Contacts.CONTENT_URI,
                projection,
                ContactsContract.Contacts.STARRED + " = 1",
                null,
                null
            )
        }.getOrNull() ?: return emptySet()
        val ids = HashSet<Long>()
        cursor.use { query ->
            val idIndex = query.getColumnIndex(ContactsContract.Contacts._ID)
            if (idIndex < 0) return emptySet()
            while (query.moveToNext() && ids.size < MAX_CONTACTS) {
                ids.add(query.getLong(idIndex))
            }
        }
        return ids
    }

    // ---------------------------------------------------------- favorites

    /**
     * The starred contacts that have at least one phone number.
     *
     * The contacts table answers "who is starred"; one bounded query on
     * the phone table then fetches the numbers, and the first number of a
     * contact wins. Favorites are few by nature, so a per-contact query
     * would only add round trips.
     */
    suspend fun favorites(context: Context): Load<FavoriteContact> = withContext(Dispatchers.IO) {
        if (!canReadContacts(context)) {
            return@withContext Load<FavoriteContact>(emptyList(), denied = true)
        }
        val projection = arrayOf(
            ContactsContract.Contacts._ID,
            ContactsContract.Contacts.DISPLAY_NAME
        )
        val attempt = runCatching {
            context.contentResolver.query(
                ContactsContract.Contacts.CONTENT_URI,
                projection,
                ContactsContract.Contacts.STARRED + " = 1 AND " +
                    ContactsContract.Contacts.HAS_PHONE_NUMBER + " = 1",
                null,
                ContactsContract.Contacts.DISPLAY_NAME + " ASC"
            )
        }
        if (attempt.exceptionOrNull() is SecurityException) {
            return@withContext Load<FavoriteContact>(emptyList(), denied = true)
        }
        val cursor = attempt.getOrNull()
            ?: return@withContext Load<FavoriteContact>(emptyList(), failed = true)

        val names = LinkedHashMap<Long, String>()
        cursor.use { query ->
            val idIndex = query.getColumnIndex(ContactsContract.Contacts._ID)
            if (idIndex < 0) return@withContext Load<FavoriteContact>(emptyList(), failed = true)
            val nameIndex = query.getColumnIndex(ContactsContract.Contacts.DISPLAY_NAME)
            while (query.moveToNext() && names.size < MAX_FAVORITES) {
                val id = query.getLong(idIndex)
                if (id <= 0L) continue
                val name = if (nameIndex >= 0) {
                    query.getString(nameIndex)?.trim().orEmpty()
                } else {
                    ""
                }
                names[id] = name
            }
        }
        if (names.isEmpty()) return@withContext Load<FavoriteContact>(emptyList())

        val numbers = firstNumbers(context, names.keys.toList())
        val rows = names.mapNotNull { (id, name) ->
            val number = numbers[id] ?: return@mapNotNull null
            FavoriteContact(contactId = id, name = name, number = number)
        }
        Load<FavoriteContact>(rows)
    }

    /**
     * The first phone number of each of [contactIds].
     *
     * One query with one placeholder per contact: the favourites are
     * bounded by [MAX_FAVORITES], so the statement stays small. A
     * contact without a row is simply absent from the map.
     */
    private fun firstNumbers(context: Context, contactIds: List<Long>): Map<Long, String> {
        if (contactIds.isEmpty()) return emptyMap()
        val placeholders = contactIds.joinToString(", ") { "?" }
        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.CONTACT_ID,
            ContactsContract.CommonDataKinds.Phone.NUMBER
        )
        val cursor = runCatching {
            context.contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                projection,
                ContactsContract.CommonDataKinds.Phone.CONTACT_ID + " IN ($placeholders)",
                contactIds.map { it.toString() }.toTypedArray(),
                ContactsContract.CommonDataKinds.Phone.CONTACT_ID + " ASC"
            )
        }.getOrNull() ?: return emptyMap()
        val numbers = HashMap<Long, String>()
        cursor.use { query ->
            val idIndex = query.getColumnIndex(ContactsContract.CommonDataKinds.Phone.CONTACT_ID)
            if (idIndex < 0) return emptyMap()
            val numberIndex = query.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
            while (query.moveToNext()) {
                val id = query.getLong(idIndex)
                if (numbers.containsKey(id)) continue
                val number = if (numberIndex >= 0) {
                    query.getString(numberIndex)?.trim().orEmpty()
                } else {
                    ""
                }
                if (number.isNotEmpty()) numbers[id] = number
            }
        }
        return numbers
    }

    // ---------------------------------------------------------- voicemail

    /**
     * The voicemail number the carrier configured, or null.
     *
     * The public lookup takes the account it should ask about, so it is
     * asked about the first call-capable account of this phone: a carrier
     * sets the number per account, and without an account the answer is
     * unknown rather than guessed.
     *
     * Both calls can throw `SecurityException` without `READ_PHONE_STATE`:
     * listing the accounts needs it, and so does reading the number. A
     * refusal, a phone with no call-capable account, or a card that never
     * received a number all come back as null, and the screen says so
     * instead of dialling a made-up number.
     */
    suspend fun voicemailNumber(context: Context): String? = withContext(Dispatchers.IO) {
        val manager = runCatching {
            context.getSystemService(TelecomManager::class.java)
        }.getOrNull() ?: return@withContext null
        runCatching {
            val handle = manager.callCapablePhoneAccounts.firstOrNull()
            if (handle == null) null else manager.getVoiceMailNumber(handle)?.trim()
        }.getOrNull()?.takeIf { it.isNotEmpty() }
    }
}
