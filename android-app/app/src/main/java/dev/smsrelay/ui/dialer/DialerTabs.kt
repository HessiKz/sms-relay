package dev.smsrelay.ui.dialer

import android.content.Context
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextView
import androidx.annotation.StringRes
import com.google.android.material.bottomsheet.BottomSheetDialog
import dev.smsrelay.R
import dev.smsrelay.ui.messages.AvatarDrawable
import java.util.Locale
import com.google.android.material.R as MaterialR

/**
 * The four tabs of the Phone home.
 *
 * The tab is the one piece of state the screen keeps: it decides which
 * read the list shows, which empty sentence belongs under it and which
 * permission it needs. Keeping that in one place means the tab strip, the
 * list and the state text cannot disagree.
 */
enum class DialerTab(@StringRes val titleRes: Int) {
    FAVORITES(R.string.dialer_tab_favorites),
    RECENTS(R.string.dialer_tab_recents),
    CONTACTS(R.string.dialer_tab_contacts),
    VOICEMAIL(R.string.dialer_tab_voicemail);

    companion object {
        /**
         * The tab at [ordinal], or [RECENTS] for a value that cannot be
         * one: a bundle written by an older build must not crash a newer
         * one.
         */
        fun of(ordinal: Int): DialerTab =
            values().firstOrNull { it.ordinal == ordinal } ?: RECENTS
    }
}

/**
 * The small formatting the dialer rows share.
 *
 * One place turns seconds into a label, so the row and its content
 * description always say the same thing.
 */
object DialerFormat {

    /** "0:42", "12:05" or "1:02:33" - a call length, never "0:00". */
    fun duration(seconds: Int): String {
        val total = seconds.coerceAtLeast(0)
        val hours = total / 3600
        val minutes = (total % 3600) / 60
        val rest = total % 60
        return if (hours > 0) {
            String.format(Locale.getDefault(), "%d:%02d:%02d", hours, minutes, rest)
        } else {
            String.format(Locale.getDefault(), "%d:%02d", minutes, rest)
        }
    }

    /**
     * The second line of a row: the parts that carry information, joined
     * by a dot.
     *
     * Empty parts are dropped instead of leaving a stray separator, so a
     * phone with one SIM and a missed call shows one clean line.
     */
    fun meta(parts: List<String?>): String =
        parts.mapNotNull { it?.trim()?.takeIf { text -> text.isNotEmpty() } }
            .joinToString(" · ")

    /** True when a duration is worth printing: a connected call only. */
    fun hasDuration(kind: DialerRepository.CallKind, seconds: Int): Boolean =
        seconds > 0 && when (kind) {
            DialerRepository.CallKind.INCOMING,
            DialerRepository.CallKind.OUTGOING,
            DialerRepository.CallKind.OTHER -> true
            else -> false
        }
}

/**
 * The long-press sheet of a row.
 *
 * A bottom sheet with plain text rows instead of a dialog: a call log row
 * has four actions, and a sheet keeps them under the thumb that opened
 * it. It is built in code like [dev.smsrelay.ui.messages.MessagesBanner],
 * because an unused action must not exist in a layout.
 */
object DialerMenus {

    /** One line of the sheet. */
    class Action(
        val label: String,
        val destructive: Boolean = false,
        val action: () -> Unit
    )

    fun show(context: Context, actions: List<Action>) {
        if (actions.isEmpty()) return
        val sheet = BottomSheetDialog(context)
        val column = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(context, 8), 0, dp(context, 8))
        }
        for (item in actions) {
            column.addView(row(context, sheet, item))
        }
        sheet.setContentView(column)
        sheet.show()
    }

    private fun row(context: Context, sheet: BottomSheetDialog, item: Action): TextView =
        TextView(context).apply {
            text = item.label
            textSize = 16f
            gravity = Gravity.CENTER_VERTICAL
            minHeight = dp(context, 48)
            isClickable = true
            isFocusable = true
            setPadding(dp(context, 24), dp(context, 12), dp(context, 24), dp(context, 12))
            setTextColor(
                AvatarDrawable.themeColor(
                    context,
                    if (item.destructive) {
                        MaterialR.attr.colorError
                    } else {
                        MaterialR.attr.colorOnSurface
                    },
                    0xFF1D1B20.toInt()
                )
            )
            // The ripple of the current theme, resolved instead of named:
            // the sheet lives outside this app's layouts.
            val ripple = android.util.TypedValue()
            if (context.theme.resolveAttribute(
                    android.R.attr.selectableItemBackground,
                    ripple,
                    true
                ) && ripple.resourceId != 0
            ) {
                setBackgroundResource(ripple.resourceId)
            }
            setOnClickListener {
                sheet.dismiss()
                item.action()
            }
        }

    private fun dp(context: Context, value: Int): Int =
        (value * context.resources.displayMetrics.density).toInt()
}
