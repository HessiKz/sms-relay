package dev.smsrelay.ui.messages

import android.content.Context
import dev.smsrelay.R
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.messages.SmsThread

/**
 * The name a conversation is shown under.
 *
 * One place decides it, so the list row and the thread toolbar can never
 * disagree about who a conversation is with. A one to one thread is the
 * contact name or, without one, the number as the user would dial it. A
 * group is never represented by a number: it is the first names when the
 * contacts are known, and a plain "Group (n)" otherwise.
 */
object ThreadLabels {

    /** How many member names a group label spells out. */
    private const val NAMES_IN_LABEL = 2

    /** The label of one conversation. */
    fun of(context: Context, thread: SmsThread): String {
        if (thread.isGroup) {
            return group(
                context = context,
                names = thread.recipientNames,
                addresses = thread.recipients
            )
        }
        val name = thread.name
        if (!name.isNullOrBlank()) return name
        return SmsRepository.displayNumber(thread.address).ifBlank {
            context.getString(R.string.messages_unknown_sender)
        }
    }

    /**
     * The label of a group.
     *
     * Contact names come first, at most [NAMES_IN_LABEL] of them, with
     * the remaining people counted after a "+". When no member is a
     * contact the label is the count alone: a group has no phone number
     * to show, and inventing one would be a lie about who is in it.
     */
    fun group(context: Context, names: List<String>, addresses: List<String>): String {
        val known = ArrayList<String>(NAMES_IN_LABEL)
        for (name in names) {
            if (name.isBlank()) continue
            if (known.size == NAMES_IN_LABEL) break
            known.add(name.trim())
        }
        val count = maxOf(names.size, addresses.size)
        if (known.isEmpty()) return if (count > 0) {
            context.getString(R.string.messages_group_label, count)
        } else {
            context.getString(R.string.messages_group)
        }
        val shown = known.joinToString(", ")
        val rest = count - known.size
        return if (rest > 0) {
            context.getString(R.string.messages_group_more, shown, rest)
        } else {
            shown
        }
    }
}
