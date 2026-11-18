package dev.smsrelay.messages

/**
 * One row of the phone's conversation list.
 *
 * It is the shape of a row of `content://mms-sms/conversations?simple=true`
 * after the recipient ids were turned into addresses, plus the contact
 * names once the repository resolved them. Nothing here is a relay
 * record: this is the real SMS store of the phone.
 *
 * A thread is either one person ([recipients] holds one address) or a
 * group ([recipients] holds two or more). A group has no single address,
 * so [address] is empty for it: the screen must never dial one member and
 * present the thread as a private chat with that person.
 *
 * No number, name or snippet is ever written to a log.
 */
data class SmsThread(
    /** `_ID` of the thread: the value the thread screen is opened with. */
    val threadId: Long,
    /**
     * The other side of a one to one conversation, as the provider spells
     * it. Empty for a group, which has no single other side.
     */
    val address: String,
    /** Every resolved recipient address, in provider order. */
    val recipients: List<String>,
    /** The contact name of a one to one conversation, or null. */
    val name: String?,
    /**
     * The contact names of [recipients], same order and same size: an
     * empty string where the address is not a contact.
     */
    val recipientNames: List<String>,
    /** The newest message, one line. Also the only view of an MMS thread. */
    val snippet: String,
    /** `DATE` of the newest message. */
    val dateMillis: Long,
    /** How many messages of this thread are still unread. */
    val unread: Int
) {
    /** True when more than one person is on the other side. */
    val isGroup: Boolean
        get() = recipients.size > 1

    /** How many people are on the other side of the conversation. */
    val recipientCount: Int
        get() = recipients.size

    /**
     * The stable key of the avatar colour.
     *
     * A group keys off its thread id, so it keeps one colour even when a
     * member is added, and never borrows the colour of a single person.
     */
    val avatarKey: String
        get() = if (isGroup) "group:$threadId" else address
}
