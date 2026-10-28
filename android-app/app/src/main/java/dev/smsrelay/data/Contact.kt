package dev.smsrelay.data

/**
 * One entry of the phone's contact list.
 *
 * Only the display name and the phone number are ever read from the
 * contacts database and only these two fields ever leave the phone.
 * Neither of them is written to a log.
 */
data class Contact(
    val number: String,
    val name: String
)
