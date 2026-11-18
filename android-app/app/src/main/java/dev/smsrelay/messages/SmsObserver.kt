package dev.smsrelay.messages

import android.content.Context
import android.database.ContentObserver
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.Telephony

/**
 * Watches the two provider tables the Messages screens draw from.
 *
 * One observer for both URIs: a new message always changes the
 * conversation list as well, and a screen only needs one reason to
 * reload. The callback is debounced by [DEBOUNCE_MS] because the provider
 * can notify several times for one write, and it always runs on the main
 * thread so a screen can touch its views directly.
 *
 * It holds the application context only, and [stop] is called by the
 * screen, so a finished screen is never kept alive by the observer.
 */
class SmsObserver(
    context: Context,
    private val onChanged: () -> Unit
) : ContentObserver(Handler(Looper.getMainLooper())) {

    private val appContext: Context = context.applicationContext
    private val handler = Handler(Looper.getMainLooper())
    private var registered = false
    private var scheduled = false

    /** Starts listening; safe to call twice. */
    fun start() {
        if (registered) return
        try {
            val resolver = appContext.contentResolver
            resolver.registerContentObserver(Telephony.Sms.CONTENT_URI, true, this)
            resolver.registerContentObserver(
                Uri.parse("content://mms-sms/conversations"),
                true,
                this
            )
            registered = true
        } catch (e: Exception) {
            registered = false
        }
    }

    /** Stops listening; the screens call this in `onStop`. */
    fun stop() {
        if (!registered) return
        registered = false
        try {
            appContext.contentResolver.unregisterContentObserver(this)
        } catch (e: Exception) {
            // Already gone: nothing to unregister.
        }
        handler.removeCallbacksAndMessages(null)
        scheduled = false
    }

    override fun onChange(selfChange: Boolean) {
        schedule()
    }

    override fun onChange(selfChange: Boolean, uri: Uri?) {
        schedule()
    }

    /** Collapses a burst of provider notifications into one reload. */
    private fun schedule() {
        if (!registered || scheduled) return
        scheduled = true
        handler.postDelayed({
            scheduled = false
            if (registered) onChanged()
        }, DEBOUNCE_MS)
    }

    private companion object {
        const val DEBOUNCE_MS = 300L
    }
}
