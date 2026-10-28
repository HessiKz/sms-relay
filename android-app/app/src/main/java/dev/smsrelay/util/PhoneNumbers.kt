package dev.smsrelay.util

/**
 * Turns what a human typed into a number the radio can dial.
 *
 * The same number is written in many ways: with a country code, with
 * the local leading zero, with spaces or dashes, or with Persian
 * numerals. Everything is normalised here so that the rest of the app
 * only ever deals with plain digits.
 */
object PhoneNumbers {

    /** Persian digits ۰-۹ (U+06F0). */
    private const val PERSIAN_FIRST = 0x06F0

    /** Arabic-Indic digits ٠-٩ (U+0660). */
    private const val ARABIC_FIRST = 0x0660

    /** Full width digits ０-９ (U+FF10). */
    private const val FULLWIDTH_FIRST = 0xFF10

    /** Space, dash, brackets and the like between the digits. */
    private val SEPARATORS = Regex("[\\s\\-()./\\u200c\\u2013\\u2014]")

    /** The shortest thing that can still be a phone number. */
    private const val MIN_DIGITS = 5

    /**
     * Replaces every non-ASCII digit with its ASCII twin.
     *
     * Persian and Arabic keyboards produce the same numbers as the
     * Latin ones, so "۰۹۱۲..." must become "0912..." before anything
     * else can look at it.
     */
    fun toAsciiDigits(text: String): String {
        val builder = StringBuilder(text.length)
        for (character in text) {
            val code = character.code
            val digit = when {
                code in PERSIAN_FIRST..(PERSIAN_FIRST + 9) -> code - PERSIAN_FIRST
                code in ARABIC_FIRST..(ARABIC_FIRST + 9) -> code - ARABIC_FIRST
                code in FULLWIDTH_FIRST..(FULLWIDTH_FIRST + 9) -> code - FULLWIDTH_FIRST
                else -> -1
            }
            if (digit >= 0) {
                builder.append(('0'.code + digit).toChar())
            } else {
                builder.append(character)
            }
        }
        return builder.toString()
    }

    /**
     * The number in one plain form, or null when the text is not a
     * number at all.
     *
     * A local Iranian number becomes the international one:
     * "0912..." -> "+98912...", "0098912..." -> "+98912..." and
     * "98912..." -> "+98912...". A number that already carries a
     * country code keeps it, a bare number keeps the digits it has.
     */
    fun normalize(raw: String): String? {
        val text = toAsciiDigits(raw).trim()
        if (text.isEmpty()) return null
        val cleaned = text.replace(SEPARATORS, "")
        if (cleaned.isEmpty()) return null

        var digits = cleaned
        var international = false
        if (digits.startsWith("+")) {
            international = true
            digits = digits.substring(1)
        } else if (digits.startsWith("00")) {
            international = true
            digits = digits.substring(2)
        }
        if (digits.isEmpty() || !digits.all { it in '0'..'9' }) return null
        if (digits.length < MIN_DIGITS) return null

        if (international) return "+" + digits

        // A local Iranian number: 0912... -> +98912...
        if (digits.startsWith("0")) {
            return if (digits.length in 10..13) "+98" + digits.substring(1) else digits
        }
        // The same number without the leading zero: 98912... -> +98912...
        if (digits.startsWith("98") && digits.length == 12) return "+" + digits
        return digits
    }
}
