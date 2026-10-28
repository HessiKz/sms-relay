package dev.smsrelay.contacts

import android.content.Context
import dev.smsrelay.SettingsStore
import dev.smsrelay.data.ContactsRepository
import dev.smsrelay.net.RelayClient
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Uploads the contact list of the phone to the relay server.
 *
 * The server replaces the whole list with whatever arrives, so the
 * phone sends everything it has. The upload always runs on its own
 * thread: the contacts database and the network must never hold up the
 * user interface.
 */
object ContactsSync {

    /** How old the list on the server may be before it is sent again. */
    const val AUTO_SYNC_INTERVAL_MS = 6L * 60 * 60 * 1000

    /** A failed upload is not repeated before this time. */
    const val RETRY_INTERVAL_MS = 15L * 60 * 1000

    /** Only one upload at a time, whatever asks for it. */
    private val running = AtomicBoolean(false)

    /** What happened during one sync. */
    enum class Outcome {
        /** The list was sent to the server. */
        Uploaded,

        /** There was nothing to do. */
        NothingToDo,

        /** The app may not read the contacts. */
        NoPermission,

        /** The upload did not arrive. */
        Failed
    }

    fun hasPermission(context: Context): Boolean =
        ContactsRepository.hasPermission(context)

    /**
     * True when the list on the server is older than the list of the
     * phone. The list is sent again at most every six hours.
     */
    fun isDue(context: Context): Boolean {
        if (!hasPermission(context)) return false
        val settings = SettingsStore(context)
        val now = System.currentTimeMillis()
        val lastSync = settings.lastContactsSyncAt
        val fresh = lastSync > 0L && now - lastSync < AUTO_SYNC_INTERVAL_MS
        if (fresh) return false
        // A failed attempt is not repeated over and over again.
        val lastAttempt = settings.lastContactsAttemptAt
        return lastAttempt <= 0L || now - lastAttempt >= RETRY_INTERVAL_MS
    }

    /**
     * Uploads the list now, on a background thread.
     *
     * [onDone] is called on that background thread; the caller moves the
     * result to the user interface itself.
     */
    fun sync(context: Context, onDone: ((Outcome) -> Unit)? = null) {
        val app = context.applicationContext
        if (!hasPermission(app)) {
            onDone?.invoke(Outcome.NoPermission)
            return
        }
        if (!running.compareAndSet(false, true)) {
            // Another sync is already on its way.
            onDone?.invoke(Outcome.NothingToDo)
            return
        }
        Thread {
            val outcome = upload(app)
            running.set(false)
            onDone?.invoke(outcome)
        }.start()
    }

    /** Uploads the list when it is due. */
    fun syncIfDue(context: Context, onDone: ((Outcome) -> Unit)? = null) {
        if (!isDue(context)) {
            onDone?.invoke(Outcome.NothingToDo)
            return
        }
        sync(context, onDone)
    }

    private fun upload(context: Context): Outcome {
        val settings = SettingsStore(context)
        settings.lastContactsAttemptAt = System.currentTimeMillis()

        val contacts = ContactsRepository.readContacts(context)
            ?: return Outcome.Failed
        if (contacts.isEmpty()) {
            // Nothing to send. The list on the server is left alone
            // instead of being wiped.
            return Outcome.NothingToDo
        }

        val client = RelayClient(settings.baseUri, settings.token)
        if (!client.uploadContacts(contacts)) return Outcome.Failed

        settings.lastContactsSyncAt = System.currentTimeMillis()
        settings.contactsCount = contacts.size
        return Outcome.Uploaded
    }
}
