package dev.smsrelay.ui.messages

import android.content.Context
import dev.smsrelay.R
import java.time.Instant
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * The dates of the Messages screens.
 *
 * One place decides what "Today" means, so the list row, the day
 * separator and the bubble clock can never disagree about midnight.
 * Everything is local time of the phone: a message from 23:59 belongs to
 * the day the user experienced, not to a UTC date.
 *
 * `minSdk` is 26, so `java.time` is available without desugaring.
 */
object MessageDates {

    /** "09:41" - the clock of the phone. */
    fun time(millis: Long): String = format(millis, "HH:mm")

    /**
     * The stamp of a conversation row: the time for today, the word for
     * yesterday, and a short date before that.
     */
    fun rowLabel(context: Context, millis: Long): String {
        val day = day(millis)
        val today = LocalDate.now()
        return when (day) {
            today -> time(millis)
            today.minusDays(1) -> context.getString(R.string.messages_yesterday)
            else -> if (day.year == today.year) {
                format(millis, "d MMM")
            } else {
                format(millis, "d MMM yyyy")
            }
        }
    }

    /**
     * The label of a day given its epoch day.
     *
     * The thread list groups by the epoch day it computed from the
     * message, so this function must not convert back through a millis
     * value: it would land on midnight and, in a daylight-saving zone,
     * could name the day before.
     */
    fun dayHeaderForEpochDay(context: Context, epochDay: Long): String {
        val day = try {
            LocalDate.ofEpochDay(epochDay)
        } catch (e: Exception) {
            return ""
        }
        val today = LocalDate.now()
        return when {
            day == today -> context.getString(R.string.day_today)
            day == today.minusDays(1) -> context.getString(R.string.messages_yesterday)
            day.year == today.year -> dayLabel(day, "d MMM")
            else -> dayLabel(day, "d MMM yyyy")
        }
    }

    private fun dayLabel(day: LocalDate, pattern: String): String = try {
        DateTimeFormatter.ofPattern(pattern, Locale.getDefault()).format(day)
    } catch (e: Exception) {
        ""
    }

    /** The local day of a moment, used to group messages. */
    fun day(millis: Long): LocalDate =
        Instant.ofEpochMilli(millis).atZone(ZoneId.systemDefault()).toLocalDate()

    private fun format(millis: Long, pattern: String): String {
        if (millis <= 0L) return ""
        return try {
            val moment = LocalDateTime.ofInstant(
                Instant.ofEpochMilli(millis),
                ZoneId.systemDefault()
            )
            DateTimeFormatter.ofPattern(pattern, Locale.getDefault()).format(moment)
        } catch (e: Exception) {
            ""
        }
    }
}
