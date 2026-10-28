package dev.smsrelay.contacts

import android.content.Context
import dev.smsrelay.data.ContactsRepository
import dev.smsrelay.util.PhoneNumbers

/**
 * Turns the destination of a message into a number the radio can dial.
 *
 * The target is usually a number, but the bot may also send the name
 * of a contact ("Alice"). The phone has the contact list, so it can
 * answer that question itself instead of failing.
 */
object ContactResolver {

    /**
     * The number to send to, or null when the target is neither a
     * number nor a known contact.
     */
    fun resolve(context: Context, target: String): String? {
        val text = target.trim()
        if (text.isEmpty()) return null

        // A number is used as it is, only normalised.
        PhoneNumbers.normalize(text)?.let { return it }

        // A name needs the contact list of the phone.
        if (!ContactsRepository.hasPermission(context)) return null
        val index = ContactsRepository.nameIndex(context)
        if (index.isEmpty()) return null

        // First the exact name.
        index[text.lowercase()]?.let { number ->
            PhoneNumbers.normalize(number)?.let { return it }
        }

        // Then a part of a name ("Alice" inside "Alice Karimi").
        val wanted = text.lowercase()
        for ((name, number) in index) {
            if (name.contains(wanted)) {
                PhoneNumbers.normalize(number)?.let { return it }
            }
        }
        return null
    }
}
