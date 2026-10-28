package dev.smsrelay.data

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.provider.ContactsContract
import androidx.core.content.ContextCompat

/**
 * Reads the contact list of the phone.
 *
 * Only the display name and the phone number are read; nothing else
 * from the contacts database is touched. Names and numbers are never
 * written to a log.
 */
object ContactsRepository {

    /** The server accepts at most this many contacts per upload. */
    const val MAX_CONTACTS = 3000

    /** How long the name index is kept before the contacts are read again. */
    private const val CACHE_MS = 60_000L

    /** The last name index and the moment it was made. */
    @Volatile
    private var cachedIndex: Pair<Long, Map<String, String>>? = null

    /** The app may only read the contacts when the user allowed it. */
    fun hasPermission(context: Context): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CONTACTS) ==
            PackageManager.PERMISSION_GRANTED

    /**
     * Every contact that has a name and a number.
     *
     * Returns null when the contacts database could not be read at all:
     * the caller must not treat that as "the phone has no contacts",
     * because uploading an empty list would wipe the list on the
     * server.
     */
    fun readContacts(context: Context): List<Contact>? {
        if (!hasPermission(context)) return null
        val contacts = mutableListOf<Contact>()
        val seenNumbers = mutableSetOf<String>()
        val projection = arrayOf(
            ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
            ContactsContract.CommonDataKinds.Phone.NUMBER
        )
        val cursor = runCatching {
            context.contentResolver.query(
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                projection,
                null,
                null,
                ContactsContract.CommonDataKinds.Phone.CONTACT_ID + " ASC"
            )
        }.getOrNull() ?: return null
        cursor.use {
            val nameColumn = it.getColumnIndex(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
            val numberColumn = it.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
            if (nameColumn < 0 || numberColumn < 0) return null
            while (it.moveToNext()) {
                val name = it.getString(nameColumn)?.trim().orEmpty()
                val number = it.getString(numberColumn)?.trim().orEmpty()
                if (name.isEmpty() || number.isEmpty()) continue
                // The same number can be stored more than once (a
                // contact with a work and a private number); the first
                // name wins.
                if (!seenNumbers.add(number)) continue
                contacts.add(Contact(number = number, name = name))
                if (contacts.size >= MAX_CONTACTS) break
            }
        }
        return contacts
    }

    /**
     * "name -> number" for every contact. The first match wins, so a
     * contact that is stored twice keeps its first number.
     *
     * The keys are lower case, so the lookup is case insensitive.
     *
     * The result is kept for a short moment: a batch of commands from
     * Telegram reads the contacts database only once.
     */
    fun nameIndex(context: Context): Map<String, String> {
        val cached = cachedIndex
        val now = System.currentTimeMillis()
        if (cached != null && now - cached.first < CACHE_MS) {
            return cached.second
        }
        val index = buildNameIndex(context)
        // An empty index is not kept: a contacts database that could not
        // be read must not look like a phone without contacts.
        if (index.isNotEmpty()) cachedIndex = now to index
        return index
    }

    private fun buildNameIndex(context: Context): Map<String, String> {
        val index = LinkedHashMap<String, String>()
        val contacts = readContacts(context) ?: return index
        for (contact in contacts) {
            val key = contact.name.lowercase()
            if (key.isEmpty()) continue
            if (!index.containsKey(key)) index[key] = contact.number
        }
        return index
    }
}
