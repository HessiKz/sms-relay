package dev.smsrelay.ui.messages

import android.content.Context
import android.graphics.drawable.Drawable
import android.graphics.drawable.GradientDrawable
import android.util.TypedValue
import androidx.core.content.ContextCompat

/**
 * The circle with initials that stands in for a contact photo.
 *
 * The phone's real photo is not read (no contact photo permission is
 * asked for and none is cached), so every conversation gets a coloured
 * circle with one or two letters. The colour is derived from the address,
 * so the same person keeps the same colour on every launch and in both
 * lists, and no state has to be stored for it.
 *
 * The palette is mid-tone and the letters are white: both stay readable
 * in light and dark mode without asking the theme for a second colour.
 */
object AvatarDrawable {

    /** Mid-tone colours, all dark enough for white text on top. */
    private val PALETTE = intArrayOf(
        0xFF5C6BC0.toInt(), // indigo
        0xFF26A69A.toInt(), // teal
        0xFFEF6C00.toInt(), // orange
        0xFF8E24AA.toInt(), // purple
        0xFF00897B.toInt(), // green-teal
        0xFFC2185B.toInt(), // pink
        0xFF3949AB.toInt(), // deep indigo
        0xFF00838F.toInt(), // cyan
        0xFF6D4C41.toInt(), // brown
        0xFF546E7A.toInt() // blue grey
    )

    /** The colour of one conversation: stable for the same address. */
    fun colorFor(key: String): Int {
        if (key.isEmpty()) return PALETTE[0]
        // A small, stable hash. `String.hashCode` is specified by the
        // language, so the colour survives a reinstall.
        var hash = 0
        for (character in key) {
            hash = hash * 31 + character.code
        }
        val index = ((hash % PALETTE.size) + PALETTE.size) % PALETTE.size
        return PALETTE[index]
    }

    /**
     * One or two letters for the circle.
     *
     * A name gives its initials ("Ali Karimi" -> "AK"). A bare number
     * gives its last two digits, which is how a person recognises an
     * unknown sender in a list of numbers.
     */
    fun initials(label: String): String {
        val text = label.trim()
        if (text.isEmpty()) return "#"
        if (text.any { it.isLetter() }) {
            val words = text.split(' ', '\t', '.', '-', '_')
                .filter { it.isNotBlank() && it.any { character -> character.isLetter() } }
            return when {
                words.isEmpty() -> text.take(1).uppercase()
                words.size == 1 -> words[0].take(1).uppercase()
                else -> (words[0].take(1) + words[1].take(1)).uppercase()
            }
        }
        val digits = text.filter { it.isDigit() }
        return if (digits.length >= 2) digits.takeLast(2) else text.take(2)
    }

    /** The circle itself, tinted for one conversation. */
    fun circle(key: String): Drawable = GradientDrawable().apply {
        shape = GradientDrawable.OVAL
        setColor(colorFor(key))
    }

    /** A colour of the current theme, with a fallback for old themes. */
    fun themeColor(context: Context, attr: Int, fallback: Int): Int {
        val value = TypedValue()
        if (!context.theme.resolveAttribute(attr, value, true)) return fallback
        return if (value.resourceId != 0) {
            runCatching { ContextCompat.getColor(context, value.resourceId) }.getOrDefault(fallback)
        } else {
            value.data
        }
    }
}
