package dev.smsrelay.util

import android.content.Context
import dev.smsrelay.R

/**
 * "3 minutes ago" in plain words.
 *
 * The app talks to a person, not to a machine, so a timestamp is
 * written as a duration instead of a date.
 */
object RelativeTime {

    private const val MINUTE_MS = 60_000L
    private const val HOUR_MS = 60 * MINUTE_MS
    private const val DAY_MS = 24 * HOUR_MS

    fun describe(context: Context, millis: Long): String {
        if (millis <= 0L) return context.getString(R.string.status_never)
        val delta = System.currentTimeMillis() - millis
        if (delta < MINUTE_MS) return context.getString(R.string.time_just_now)
        val minutes = delta / MINUTE_MS
        if (minutes < 60) return context.getString(R.string.time_minutes, minutes)
        val hours = minutes / 60
        if (hours < 24) return context.getString(R.string.time_hours, hours)
        val days = hours / 24
        return context.getString(R.string.time_days, days)
    }
}
