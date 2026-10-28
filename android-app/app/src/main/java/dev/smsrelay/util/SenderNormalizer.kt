package dev.smsrelay.util

/**
 * Normalises a sender address so the same person is always the same key.
 *
 * The same message can reach the phone through two paths that spell the
 * sender differently: the SMS broadcast carries the address as the
 * network displays it ("0912 000 0001"), while the notification of the
 * messaging app carries the contact's stored E.164 form
 * ("+989120000001"). Comparing the raw strings therefore never matched
 * and every message looked like a duplicate.
 *
 * The rules:
 *  - drop everything that is not a digit, except a leading "+";
 *  - a local Iranian number starting with 0 becomes +98 (0919… -> +98919…);
 *  - 0098… becomes +98…;
 *  - a bare country code (98…) gets the "+";
 *  - anything else that is not already international keeps its digits.
 */
object SenderNormalizer {

    fun normalize(sender: String): String {
        val trimmed = sender.trim()
        if (trimmed.isEmpty()) return trimmed
        val plus = trimmed.startsWith("+")
        val digits = trimmed.filter { it.isDigit() }
        if (digits.isEmpty()) return trimmed.lowercase()

        return when {
            plus -> "+" + digits
            digits.startsWith("0098") -> "+" + digits.drop(2)
            digits.startsWith("009") -> "+" + digits.drop(2)
            digits.startsWith("98") && digits.length >= 12 -> "+" + digits
            digits.startsWith("0") && digits.length in 10..12 ->
                "+98" + digits.drop(1)
            else -> digits
        }
    }

    /** True when both spellings denote the same sender. */
    fun sameSender(left: String, right: String): Boolean =
        normalize(left) == normalize(right)
}
