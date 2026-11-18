package dev.smsrelay.calls

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.annotation.StringRes
import dev.smsrelay.R
import dev.smsrelay.util.PhoneNumbers

/**
 * The "call via another app" catalog.
 *
 * Not every phone has every messenger, and not every messenger can be
 * started for a given number. This object answers two questions for the
 * dialer:
 *
 *  - which entries make sense on THIS phone ([available]); and
 *  - how the URL of an entry is built for a number ([Entry.buildUrl]).
 *
 * An `https` link is always offered: a browser can open it even when the
 * app is missing, and the page itself offers the app. A custom scheme
 * (`viber://`, `skype:`, `tg://`, `facetime://`) is only offered when the
 * package manager says something on this phone handles it, because a dead
 * button is worse than a missing one.
 *
 * Nothing here places a call by itself: [open] only hands the URL to
 * another app. Placing a call stays in the dialer, whose rules are
 * stricter (SIM choice, emergency passthrough).
 */
object CallDeepLinks {

    /** The entry starts a voice or video call. */
    const val CAPABILITY_CALL = "call"

    /** The entry opens a chat with the number. */
    const val CAPABILITY_CHAT = "chat"

    /**
     * One way to reach a number through another app.
     *
     * [buildUrl] receives whatever the user typed and must return a URL
     * the other app understands; it must not throw for a partial number,
     * because the row is built while the user is still typing.
     */
    data class Entry(
        val id: String,
        @StringRes val labelRes: Int,
        @StringRes val platformNoteRes: Int? = null,
        val capability: String,
        /** The scheme the package manager is asked about. */
        val scheme: String,
        /** `https` links work through a browser, so they always show. */
        val alwaysVisible: Boolean = false,
        val buildUrl: (String) -> String
    ) {
        fun label(context: Context): String = context.getString(labelRes)

        fun platformNote(context: Context): String? =
            platformNoteRes?.let { context.getString(it) }
    }

    /** A number that only exists for the "does anything handle this?" probe. */
    private const val PROBE_NUMBER = "+989120000000"

    /**
     * Every entry of the catalog, in the order the dialer shows them.
     *
     * `tel:` comes first because it is the only entry that is the phone
     * itself. FaceTime is marked iOS-only: on Android the scheme resolves
     * to nothing on a normal phone, so the row simply does not appear.
     */
    val ALL: List<Entry> = listOf(
        Entry(
            id = "tel",
            labelRes = R.string.deeplink_tel,
            capability = CAPABILITY_CALL,
            scheme = "tel",
            buildUrl = { number -> "tel:" + e164(number) }
        ),
        Entry(
            id = "whatsapp",
            labelRes = R.string.deeplink_whatsapp,
            capability = CAPABILITY_CHAT,
            scheme = "https",
            alwaysVisible = true,
            // wa.me takes the number without the plus.
            buildUrl = { number -> "https://wa.me/" + bareDigits(number) }
        ),
        Entry(
            id = "telegram",
            labelRes = R.string.deeplink_telegram,
            capability = CAPABILITY_CHAT,
            scheme = "tg",
            // `tg://` only exists when Telegram is installed, so the row
            // is hidden otherwise. The `https://t.me/+<digits>` form is
            // NOT a phone lookup on Telegram: it is an invite hash, and
            // it would open a dead invite page instead of a chat.
            buildUrl = { number -> "tg://resolve?phone=" + bareDigits(number) }
        ),
        Entry(
            id = "signal",
            labelRes = R.string.deeplink_signal,
            capability = CAPABILITY_CHAT,
            scheme = "https",
            alwaysVisible = true,
            buildUrl = { number -> "https://signal.me/#p/+" + bareDigits(number) }
        ),
        Entry(
            id = "viber",
            labelRes = R.string.deeplink_viber,
            capability = CAPABILITY_CHAT,
            scheme = "viber",
            buildUrl = { number -> "viber://chat?number=%2B" + bareDigits(number) }
        ),
        Entry(
            id = "skype",
            labelRes = R.string.deeplink_skype,
            capability = CAPABILITY_CALL,
            scheme = "skype",
            buildUrl = { number -> "skype:" + e164(number) + "?call" }
        ),
        Entry(
            id = "bale",
            labelRes = R.string.deeplink_bale,
            platformNoteRes = R.string.deeplink_bale_note,
            capability = CAPABILITY_CHAT,
            scheme = "https",
            alwaysVisible = true,
            // Bale has no per-number link: the app opens and the user
            // searches the number there.
            buildUrl = { _ -> "https://ble.ir/" }
        ),
        Entry(
            id = "rubika",
            labelRes = R.string.deeplink_rubika,
            platformNoteRes = R.string.deeplink_rubika_note,
            capability = CAPABILITY_CHAT,
            scheme = "https",
            alwaysVisible = true,
            buildUrl = { _ -> "https://rubika.ir/" }
        ),
        Entry(
            id = "facetime",
            labelRes = R.string.deeplink_facetime,
            platformNoteRes = R.string.deeplink_facetime_note,
            capability = CAPABILITY_CALL,
            scheme = "facetime",
            buildUrl = { number -> "facetime://" + e164(number) }
        )
    )

    /**
     * The entries that can be used on this phone.
     *
     * The answer is computed on every call, because an app can be
     * installed or removed while the relay runs.
     */
    fun available(context: Context): List<Entry> = ALL.filter { entry ->
        entry.alwaysVisible || resolves(context, entry)
    }

    /** True when some app on this phone claims the URL of [entry]. */
    fun resolves(context: Context, entry: Entry): Boolean {
        val intent = runCatching {
            Intent(Intent.ACTION_VIEW, Uri.parse(entry.buildUrl(PROBE_NUMBER)))
        }.getOrNull() ?: return false
        return runCatching {
            context.packageManager.resolveActivity(intent, 0) != null
        }.getOrDefault(false)
    }

    /**
     * Hands the number to the app of [entry].
     *
     * Returns false when the URL cannot be built or no app accepted it;
     * the caller shows a message instead of the app dying with an
     * `ActivityNotFoundException`.
     */
    fun open(context: Context, entry: Entry, number: String): Boolean {
        val url = runCatching { entry.buildUrl(number) }.getOrNull()
            ?.takeIf { it.isNotBlank() } ?: return false
        val intent = runCatching {
            Intent(Intent.ACTION_VIEW, Uri.parse(url))
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }.getOrNull() ?: return false
        return runCatching { context.startActivity(intent) }.isSuccess
    }

    /**
     * The number as plain digits, with no plus, no spaces.
     *
     * This is what `wa.me`, Signal and Telegram take, and none of those
     * three accept the `+`: the `+` belongs to the URL, not to the value.
     */
    private fun bareDigits(number: String): String {
        val normalized = PhoneNumbers.normalize(number) ?: number.trim()
        return PhoneNumbers.toAsciiDigits(normalized)
            .filter { it.isDigit() }
    }

    /**
     * The number in E.164 form, keeping a leading `+`.
     *
     * `tel:`, `skype:` and `facetime://` all need the country-code marker:
     * dropping it turns an international number into a local one, which
     * the other app then dials in the wrong country. The `+` is dropped
     * only when the number is not international at all, so a short code
     * still works.
     */
    private fun e164(number: String): String {
        val normalized = PhoneNumbers.normalize(number) ?: number.trim()
        val ascii = PhoneNumbers.toAsciiDigits(normalized)
        val international = ascii.startsWith("+") || ascii.startsWith("00")
        // `tel:` also accepts the USSD characters, and a code like
        // `*123#` must reach the radio unchanged.
        val digits = ascii.filter { it.isDigit() || it == '*' || it == '#' }
        return when {
            digits.isEmpty() -> ""
            international -> "+" + digits
            else -> digits
        }
    }
}
