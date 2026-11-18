package dev.smsrelay.ui.messages

import android.content.Context
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import com.google.android.material.card.MaterialCardView
import com.google.android.material.R as MaterialR

/**
 * The notes at the top of the Messages screens.
 *
 * They are built in code instead of in a layout because a note that is
 * not needed must not exist: an invisible `TextView` still takes space
 * and is still read by a screen reader. Both screens need the same
 * shape, so it lives in one place.
 */
object MessagesBanner {

    /** A tappable card with one sentence. */
    fun card(context: Context, text: String, onClick: () -> Unit): View {
        val card = MaterialCardView(context).apply {
            radius = dp(context, 12).toFloat()
            cardElevation = 0f
            setCardBackgroundColor(
                AvatarDrawable.themeColor(
                    context,
                    MaterialR.attr.colorSecondaryContainer,
                    0xFFE8DEF8.toInt()
                )
            )
            isClickable = true
            isFocusable = true
            setOnClickListener { onClick() }
        }
        val label = TextView(context).apply {
            this.text = text
            setTextColor(
                AvatarDrawable.themeColor(
                    context,
                    MaterialR.attr.colorOnSecondaryContainer,
                    0xFF1D192B.toInt()
                )
            )
            textSize = 14f
            gravity = Gravity.START
            maxLines = 3
            setPadding(dp(context, 14), dp(context, 10), dp(context, 14), dp(context, 10))
        }
        card.addView(label)
        return card
    }

    /**
     * Adds one note to a banner column, spaced from the previous one.
     *
     * The layout params are built here: a `MarginLayoutParams` set by the
     * card itself would be converted by `LinearLayout` and its margins
     * silently dropped.
     */
    fun addTo(column: ViewGroup, context: Context, text: String, onClick: () -> Unit) {
        val params = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        ).apply { bottomMargin = dp(context, 8) }
        column.addView(card(context, text, onClick), params)
    }

    private fun dp(context: Context, value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()
}
