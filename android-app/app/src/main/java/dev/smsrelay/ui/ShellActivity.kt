package dev.smsrelay.ui

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.ContactsContract
import android.provider.Settings
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SearchView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.color.DynamicColors
import com.google.android.material.floatingactionbutton.ExtendedFloatingActionButton
import dev.smsrelay.R
import dev.smsrelay.SettingsStore
import dev.smsrelay.messages.SmsObserver
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.messages.SmsThread
import dev.smsrelay.ui.messages.ConversationAdapter
import dev.smsrelay.ui.messages.MessagesBanner
import dev.smsrelay.ui.messages.ThreadLabels
import dev.smsrelay.util.RoleState
import kotlinx.coroutines.launch

/**
 * The home of the app: the Messages list of the phone's own SMS store.
 *
 * This is the first visible step of the messaging-client replacement. It
 * reads `content://mms-sms/conversations` and `content://sms` (never the
 * relay's Room queue), shows one row per thread and opens a read-only
 * thread screen. Sending is a later phase and needs the relay mirror, so
 * nothing here writes a message.
 *
 * The screen is honest about the three states a user can be in:
 *  - the app does not hold the default-SMS role, so the store is empty;
 *  - the relay is not paired, so messages stay on this phone;
 *  - everything works and the list is simply full.
 *
 * Every colour comes from a theme attribute: `Theme.SmsRelay.Messages` is
 * Material 3 and day/night, and dynamic colour is applied when the
 * platform offers it.
 */
class ShellActivity : AppCompatActivity() {

    private lateinit var toolbar: MaterialToolbar
    private lateinit var list: RecyclerView
    private lateinit var adapter: ConversationAdapter
    private lateinit var state: TextView
    private lateinit var progress: View
    private lateinit var banners: ViewGroup
    private lateinit var fab: ExtendedFloatingActionButton

    private val settings by lazy { SettingsStore(this) }

    /** Everything read from the phone, before the search filters it. */
    private var loaded: List<SmsThread> = emptyList()

    /** The current search text; empty when the field is closed. */
    private var query: String = ""

    private var observer: SmsObserver? = null

    /** Bumped per load, so a slow answer cannot overwrite a newer one. */
    private var loadGeneration = 0

    /**
     * The contact picker of the extended FAB.
     *
     * It is the system picker over `CommonDataKinds.Phone`, so the app
     * never needs a contact screen of its own and never asks for a
     * permission it does not already hold. A cancelled picker quits
     * silently; a contact without a number gets one honest toast.
     */
    private val pickContact = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (result.resultCode != RESULT_OK) return@registerForActivityResult
        val uri: Uri = result.data?.data ?: return@registerForActivityResult
        val number = readPickedNumber(uri)
        if (number.isNullOrBlank()) {
            Toast.makeText(this, R.string.messages_pick_no_number, Toast.LENGTH_SHORT).show()
            return@registerForActivityResult
        }
        startActivity(ThreadActivity.intentForAddress(this, number))
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Dynamic colour before the views are inflated: it replaces the
        // theme's palette, so a later call would need a recreate to show.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            DynamicColors.applyToActivityIfAvailable(this)
        }
        WindowCompat.setDecorFitsSystemWindows(window, false)
        setContentView(R.layout.activity_shell)

        toolbar = findViewById(R.id.toolbarShell)
        list = findViewById(R.id.listConversations)
        state = findViewById(R.id.textMessagesState)
        progress = findViewById(R.id.progressMessages)
        banners = findViewById(R.id.layoutBanners)
        fab = findViewById(R.id.fabStartChat)

        toolbar.title = getString(R.string.messages_title)
        toolbar.setOnMenuItemClickListener(::onMenuItem)
        bindSearch()

        adapter = ConversationAdapter { thread ->
            startActivity(ThreadActivity.intentFor(this, thread))
        }
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter
        list.setHasFixedSize(true)

        fab.setOnClickListener { openContactPicker() }

        applyInsets()
        showLoading()
    }

    override fun onResume() {
        super.onResume()
        // The role and the pairing can change while this screen sits
        // behind another app; the notes are re-read, never assumed.
        renderBanners()
    }

    override fun onStart() {
        super.onStart()
        val created = SmsObserver(this) { load() }
        observer = created
        created.start()
        // Loading here instead of in `onCreate` covers both the first
        // open and the return from the thread screen or the settings.
        load()
    }

    override fun onStop() {
        super.onStop()
        observer?.stop()
        observer = null
    }

    // ------------------------------------------------------------ insets

    /**
     * Edge to edge without guessing at a status bar height.
     *
     * The whole content column takes the top, left and right insets, so
     * the toolbar, the notes and the rows all sit inside the safe area
     * and the notes keep their own `paddingStart`/`paddingEnd`. The list
     * takes the bottom inset plus the space the FAB floats over, so the
     * last row can still be scrolled clear of both. The FAB moves up by
     * the navigation inset.
     */
    private fun applyInsets() {
        val fabParams: ViewGroup.MarginLayoutParams =
            fab.layoutParams as ViewGroup.MarginLayoutParams
        val fabMarginEnd = fabParams.marginEnd
        val fabMarginBottom = fabParams.bottomMargin
        val content = findViewById<View>(R.id.layoutShellContent)
        val listBottom = resources.getDimensionPixelSize(R.dimen.messages_list_bottom_padding)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.rootShell)) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            content.updatePadding(top = bars.top, left = bars.left, right = bars.right)
            list.updatePadding(bottom = listBottom + bars.bottom)
            fabParams.marginEnd = fabMarginEnd + bars.right
            fabParams.bottomMargin = fabMarginBottom + bars.bottom
            fab.layoutParams = fabParams
            insets
        }
    }

    // ------------------------------------------------------------ loading

    /** Reads the store and hands the result to the adapter. */
    private fun load() {
        val generation = ++loadGeneration
        lifecycleScope.launch {
            val result = SmsRepository.conversations(this@ShellActivity)
            if (generation != loadGeneration) return@launch
            loaded = result.threads
            if (result.failed && loaded.isEmpty()) {
                showState(getString(R.string.messages_error_read))
            } else {
                renderList()
            }
            renderBanners()
        }
    }

    private fun renderList() {
        val visible = filter(loaded, query)
        progress.visibility = View.GONE
        adapter.submitList(visible)
        when {
            visible.isNotEmpty() -> showState(null)
            query.isNotEmpty() -> showState(getString(R.string.messages_no_results))
            else -> showState(getString(R.string.messages_empty))
        }
    }

    private fun showLoading() {
        progress.visibility = View.VISIBLE
        state.visibility = View.GONE
    }

    /** One line of text in the middle of the screen, or nothing. */
    private fun showState(text: String?) {
        if (text == null) {
            state.visibility = View.GONE
            return
        }
        state.text = text
        state.visibility = View.VISIBLE
        progress.visibility = View.GONE
    }

    /**
     * Case-insensitive search over the name, the number, the snippet and
     * the members of a group.
     *
     * It filters the list that was already read instead of querying the
     * provider again: the whole conversation list is in memory, and a
     * keystroke must never touch the SMS store.
     */
    private fun filter(threads: List<SmsThread>, raw: String): List<SmsThread> {
        val needle = raw.trim().lowercase()
        if (needle.isEmpty()) return threads
        return threads.filter { thread ->
            thread.name?.lowercase()?.contains(needle) == true ||
                ThreadLabels.of(this, thread).lowercase().contains(needle) ||
                SmsRepository.displayNumber(thread.address).lowercase().contains(needle) ||
                thread.recipients.any { number ->
                    number.lowercase().contains(needle) ||
                        SmsRepository.displayNumber(number).lowercase().contains(needle)
                } ||
                thread.snippet.lowercase().contains(needle)
        }
    }

    // ------------------------------------------------------------- search

    private fun bindSearch() {
        val item = toolbar.menu.findItem(R.id.action_search) ?: return
        item.setOnActionExpandListener(object : MenuItem.OnActionExpandListener {
            override fun onMenuItemActionExpand(item: MenuItem): Boolean {
                // The SearchView is created by the menu the first time the
                // item expands; configuring it here covers that moment.
                configureSearch(item.actionView as? SearchView)
                return true
            }

            override fun onMenuItemActionCollapse(item: MenuItem): Boolean {
                query = ""
                renderList()
                return true
            }
        })
        configureSearch(item.actionView as? SearchView)
    }

    /** Wires one SearchView to the in-memory filter. */
    private fun configureSearch(view: SearchView?) {
        if (view == null || view.tag == TAG_SEARCH_BOUND) return
        view.tag = TAG_SEARCH_BOUND
        view.queryHint = getString(R.string.messages_search_hint)
        view.setOnQueryTextListener(object : SearchView.OnQueryTextListener {
            override fun onQueryTextSubmit(text: String?): Boolean {
                query = text.orEmpty()
                renderList()
                view.clearFocus()
                return true
            }

            override fun onQueryTextChange(text: String?): Boolean {
                query = text.orEmpty()
                renderList()
                return true
            }
        })
        view.setOnCloseListener {
            query = ""
            renderList()
            false
        }
    }

    // -------------------------------------------------------------- menu

    private fun onMenuItem(item: MenuItem): Boolean = when (item.itemId) {
        R.id.action_dialer -> {
            // The menu entry now opens the Phone home (Favorites, Recents,
            // Contacts, Voicemail); the full dialpad is one tap further,
            // on its FAB, and stays the only `tel:` handler.
            startActivity(Intent(this, DialerHomeActivity::class.java))
            true
        }
        R.id.action_setup -> {
            startActivity(Intent(this, MainActivity::class.java))
            true
        }
        else -> false
    }

    // ------------------------------------------------------------ picker

    private fun openContactPicker() {
        if (!hasContactsPermission()) {
            // The picker itself needs the contacts permission. Asking for
            // it here would open a system dialog with no explanation, so
            // the setup screen (which explains every permission) is the
            // place to grant it.
            startActivity(Intent(this, MainActivity::class.java))
            return
        }
        val intent = Intent(
            Intent.ACTION_PICK,
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI
        )
        try {
            pickContact.launch(intent)
        } catch (e: Exception) {
            Toast.makeText(this, R.string.messages_pick_no_number, Toast.LENGTH_SHORT).show()
        }
    }

    private fun hasContactsPermission(): Boolean =
        ContextCompat.checkSelfPermission(
            this, Manifest.permission.READ_CONTACTS
        ) == PackageManager.PERMISSION_GRANTED

    /** The number behind a `CommonDataKinds.Phone` row, or null. */
    private fun readPickedNumber(uri: Uri): String? = try {
        contentResolver.query(
            uri,
            arrayOf(ContactsContract.CommonDataKinds.Phone.NUMBER),
            null,
            null,
            null
        ).use { cursor ->
            if (cursor != null && cursor.moveToFirst()) {
                val column = cursor.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
                if (column >= 0) cursor.getString(column)?.trim() else null
            } else {
                null
            }
        }
    } catch (e: Exception) {
        null
    }

    // ----------------------------------------------------------- banners

    /** Draws the two notes the user can act on, and nothing else. */
    private fun renderBanners() {
        banners.removeAllViews()
        if (!RoleState.isDefaultSmsApp(this)) {
            MessagesBanner.addTo(
                column = banners,
                context = this,
                text = getString(R.string.messages_role_banner)
            ) { openDefaultSmsSettings() }
        }
        if (!isRelayPaired()) {
            MessagesBanner.addTo(
                column = banners,
                context = this,
                text = getString(R.string.messages_relay_banner)
            ) { startActivity(Intent(this, MainActivity::class.java)) }
        }
    }

    private fun isRelayPaired(): Boolean =
        settings.hasToken() && settings.baseUri.isNotBlank()

    /**
     * Opens the place where the role can be granted.
     *
     * Android 10+ has a one tap role request; the default-apps settings
     * are the fallback for a phone that does not offer it. A phone that
     * offers neither keeps the note.
     */
    private fun openDefaultSmsSettings() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = getSystemService(android.app.role.RoleManager::class.java)
            if (roleManager != null && runCatching {
                    roleManager.isRoleAvailable(android.app.role.RoleManager.ROLE_SMS)
                }.getOrDefault(false)
            ) {
                try {
                    startActivity(
                        roleManager.createRequestRoleIntent(android.app.role.RoleManager.ROLE_SMS)
                    )
                    return
                } catch (e: Exception) {
                    // Fall through to the settings screen.
                }
            }
        }
        try {
            startActivity(Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS))
        } catch (e: Exception) {
            Toast.makeText(this, R.string.messages_role_banner, Toast.LENGTH_LONG).show()
        }
    }

    private companion object {
        /** Marks a SearchView whose listeners are already installed. */
        const val TAG_SEARCH_BOUND = "search_bound"
    }
}
