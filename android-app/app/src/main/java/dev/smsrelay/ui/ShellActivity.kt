package dev.smsrelay.ui

import android.Manifest
import android.app.role.RoleManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.ContactsContract
import android.provider.Settings
import android.provider.Telephony
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
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
import com.google.android.material.bottomnavigation.BottomNavigationView
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
import dev.smsrelay.util.Diagnostics
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
    private lateinit var bottomNav: BottomNavigationView

    private val settings by lazy { SettingsStore(this) }

    /**
     * The id of the diagnostics entry, added in code.
     *
     * A generated id instead of a fixed one: the menu file is shared with
     * the Phone home, which must not show this item, and no number of
     * ours may collide with a resource id of the build.
     */
    private val menuDiagnostics: Int = View.generateViewId()

    /**
     * The system dialog that asks for the default SMS app.
     *
     * It is a launcher, not a bare `startActivity`, because the answer is
     * what decides whether the list is read again: without it the screen
     * sat on an empty list until the user left and came back.
     */
    private val smsRoleLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result -> handleSmsRoleResult(result.resultCode) }

    /**
     * The SMS permission dialog that follows the role.
     *
     * A launcher instead of the deprecated `requestPermissions`: the
     * answer arrives here whatever the activity did in between, and the
     * list is read again, because the provider is only readable now.
     */
    private val smsPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { load() }

    /** Everything read from the phone, before the search filters it. */
    private var loaded: List<SmsThread> = emptyList()

    /** The current search text; empty when the field is closed. */
    private var query: String = ""

    private var observer: SmsObserver? = null

    /** Bumped per load, so a slow answer cannot overwrite a newer one. */
    private var loadGeneration = 0

    /**
     * True while the selected item is set from code.
     *
     * `selectedItemId` fires the listener like a tap would. Setting the
     * id back to Messages on every resume must therefore not reopen a
     * screen; the flag makes that assignment silent.
     */
    private var suppressNav = false

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
        bottomNav = findViewById(R.id.bottomNav)

        toolbar.title = getString(R.string.messages_title)
        toolbar.setOnMenuItemClickListener(::onMenuItem)
        // The diagnostics entry is added here instead of in the menu file:
        // the file is shared with the Phone home, which has no business
        // with the SMS role, and one extra item must not appear there.
        toolbar.menu.add(Menu.NONE, menuDiagnostics, Menu.NONE, R.string.diagnostics_title)
            .setShowAsAction(MenuItem.SHOW_AS_ACTION_NEVER)
        bindSearch()

        adapter = ConversationAdapter { thread ->
            startActivity(ThreadActivity.intentFor(this, thread))
        }
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter
        list.setHasFixedSize(true)

        fab.setOnClickListener { openContactPicker() }

        installBottomNav()
        applyInsets()
        showLoading()
    }

    override fun onResume() {
        super.onResume()
        // Coming back to this screen always means the Messages page, even
        // when the user left it on the Phone or Settings page: the bar is
        // read back from the screen it is drawn on. Silent, so returning
        // does not open a second Phone home.
        selectNavSilently(R.id.nav_messages)
        // The role and the pairing can change while this screen sits
        // behind another app; the notes are re-read, never assumed. The
        // list itself is refreshed by `onStart` on a real return (the
        // settings screen) and by [handleSmsRoleResult] after the role
        // dialog, so nothing is read twice here.
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

    // -------------------------------------------------------- navigation

    /**
     * The page bar of the Messages home.
     *
     * The bar is a switch, never a stack builder: every destination is
     * started with [Intent.FLAG_ACTIVITY_REORDER_TO_FRONT] and this
     * screen is never finished, so tapping Phone, Contacts and Messages
     * in any order leaves exactly one instance of each top-level screen
     * behind the user instead of a pile of them.
     *
     * A tap never changes what this screen shows - Messages is this
     * screen - so the listener only dispatches and reports the tap as
     * handled. Coming back here re-selects Messages in [onResume], which
     * is also what repairs the highlight after the user returns from a
     * screen the bar had moved to.
     */
    private fun installBottomNav() {
        bottomNav.setOnItemSelectedListener { item ->
            if (suppressNav) return@setOnItemSelectedListener true
            when (item.itemId) {
                R.id.nav_messages -> Unit
                R.id.nav_phone -> startTopLevel(
                    DialerHomeActivity.intentForTab(this, DialerHomeActivity.TAB_RECENTS)
                )
                R.id.nav_contacts -> startTopLevel(
                    DialerHomeActivity.intentForTab(this, DialerHomeActivity.TAB_CONTACTS)
                )
                R.id.nav_settings -> startTopLevel(Intent(this, MainActivity::class.java))
                else -> return@setOnItemSelectedListener false
            }
            true
        }
        selectNavSilently(R.id.nav_messages)
    }

    /**
     * Selects an item without running the listener.
     *
     * `selectedItemId` is the only way to move the highlight, and it
     * fires the listener exactly like a tap. The flag is reset in a
     * `finally`, so a throw inside the assignment cannot leave the bar
     * permanently deaf.
     */
    private fun selectNavSilently(itemId: Int) {
        if (bottomNav.selectedItemId == itemId) return
        suppressNav = true
        try {
            bottomNav.selectedItemId = itemId
        } finally {
            suppressNav = false
        }
    }

    /**
     * Brings a top-level screen forward instead of starting another copy.
     *
     * `REORDER_TO_FRONT` raises the instance already in this task instead
     * of creating another one, and the platform delivers this intent to
     * that instance. That is how "Contacts" from here lands on the
     * Contacts tab of the Phone home the user already had:
     * [DialerHomeActivity] reads its tab extra in `onNewIntent` as well as
     * in `onCreate`.
     */
    private fun startTopLevel(intent: Intent) {
        intent.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT)
        startActivity(intent)
    }

    // ------------------------------------------------------------ insets

    /**
     * Edge to edge without guessing at a status bar height.
     *
     * The whole content column takes the top, left and right insets, so
     * the toolbar, the notes and the rows all sit inside the safe area
     * and the notes keep their own `paddingStart`/`paddingEnd`. The list
     * keeps only the space its FAB floats over, because the page bar now
     * sits below it as a real row: the list ends where the bar begins,
     * so the bottom inset must not be added to it a second time. The bar
     * itself takes that inset and fills the gesture area with its own
     * surface colour instead of leaving a strip of window background
     * under it. The FAB moves in from the sides only.
     */
    private fun applyInsets() {
        val fabParams: ViewGroup.MarginLayoutParams =
            fab.layoutParams as ViewGroup.MarginLayoutParams
        val fabMarginEnd = fabParams.marginEnd
        val content = findViewById<View>(R.id.layoutShellContent)
        val listBottom = resources.getDimensionPixelSize(R.dimen.messages_list_bottom_padding)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.rootShell)) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            content.updatePadding(top = bars.top, left = bars.left, right = bars.right)
            list.updatePadding(bottom = listBottom)
            fabParams.marginEnd = fabMarginEnd + bars.right
            fab.layoutParams = fabParams
            insets
        }
        // The page bar takes the bottom inset itself, and only that one.
        //
        // Material installs its own insets listener on this view in its
        // constructor (`BottomNavigationView.applyWindowInsets`), so
        // setting ours replaces it rather than stacking on it. Replacing
        // is deliberate: Material's listener also adds the left and right
        // system insets, and the content column above already takes those,
        // which would pad the bar twice on a landscape phone with a side
        // navigation bar. The bar is a child of that column, so it still
        // receives the insets even though the root listener above does not
        // consume them.
        val navBottom = bottomNav.paddingBottom
        ViewCompat.setOnApplyWindowInsetsListener(bottomNav) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.updatePadding(bottom = navBottom + bars.bottom)
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
            val roleHeld = RoleState.isDefaultSmsApp(this@ShellActivity)
            if (loaded.isEmpty() && !roleHeld) {
                // Without the role the provider refuses by design (or
                // answers nothing), so a read error would send the user to
                // the wrong switch. The state names the role, and the card
                // above it is the one tap that fixes it.
                showState(getString(R.string.messages_role_needed_state))
            } else if (result.failed && loaded.isEmpty()) {
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
            // on its FAB, and stays the only `tel:` handler. It goes
            // through the same reorder path as the page bar, so the menu
            // and the bar can never leave two Phone homes behind.
            startTopLevel(
                DialerHomeActivity.intentForTab(this, DialerHomeActivity.TAB_RECENTS)
            )
            true
        }
        R.id.action_setup -> {
            startTopLevel(Intent(this, MainActivity::class.java))
            true
        }
        menuDiagnostics -> {
            showDiagnostics()
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
            startTopLevel(Intent(this, MainActivity::class.java))
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

    /**
     * Draws the notes the user can act on, and nothing else.
     *
     * The role note is first and is the prominent one: while the app is
     * not the default SMS app the list is empty by design, so it must not
     * look like a footnote. A long press on it opens the diagnostics.
     */
    private fun renderBanners() {
        banners.removeAllViews()
        val roleHeld = RoleState.isDefaultSmsApp(this)
        if (!roleHeld) {
            val card = MessagesBanner.prominentCard(
                context = this,
                text = getString(R.string.messages_role_card)
            ) { requestSmsRole() }
            card.setOnLongClickListener {
                showDiagnostics()
                true
            }
            val params = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { bottomMargin = dp(12) }
            banners.addView(card, params)
        }
        if (!isRelayPaired()) {
            MessagesBanner.addTo(
                column = banners,
                context = this,
                text = getString(R.string.messages_relay_banner)
            ) { startTopLevel(Intent(this, MainActivity::class.java)) }
        }
    }

    private fun isRelayPaired(): Boolean =
        settings.hasToken() && settings.baseUri.isNotBlank()

    // -------------------------------------------------------------- roles

    /**
     * Asks to become the default SMS app.
     *
     * The flow is the one of the setup screen, because a bare
     * `startActivity` told nobody the answer: Android 10+ asks through
     * the role manager, Android 8 and 9 through `ACTION_CHANGE_DEFAULT`,
     * and the settings screen is the fallback when the phone offers
     * neither. Whichever path is taken, [handleSmsRoleResult] reads the
     * role again instead of trusting a result code.
     */
    private fun requestSmsRole() {
        if (RoleState.isDefaultSmsApp(this)) {
            Toast.makeText(this, R.string.default_sms_already, Toast.LENGTH_SHORT).show()
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            if (!RoleState.isRoleAvailable(this)) {
                // The phone has no SMS role at all; the settings are the
                // only place the choice can be made.
                Toast.makeText(this, R.string.default_sms_unavailable, Toast.LENGTH_LONG).show()
                openSmsSettings()
                return
            }
            val intent = runCatching {
                getSystemService(RoleManager::class.java)
                    ?.createRequestRoleIntent(RoleManager.ROLE_SMS)
            }.getOrNull()
            if (intent == null) {
                openSmsSettings()
                return
            }
            runCatching { smsRoleLauncher.launch(intent) }
                .onFailure { openSmsSettings() }
            return
        }
        val intent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
            .putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
        runCatching { smsRoleLauncher.launch(intent) }
            .onFailure { openSmsSettings() }
    }

    /**
     * The system dialog is closed. The role itself decides, not the
     * result code: a vendor build can answer OK and change nothing.
     */
    private fun handleSmsRoleResult(resultCode: Int) {
        val held = RoleState.isDefaultSmsApp(this)
        if (held) {
            Toast.makeText(this, R.string.default_sms_granted, Toast.LENGTH_SHORT).show()
            requestSmsPermissions()
            load()
        } else if (resultCode == RESULT_OK) {
            Toast.makeText(this, R.string.default_sms_not_applied, Toast.LENGTH_LONG).show()
        } else {
            Toast.makeText(this, R.string.default_sms_refused, Toast.LENGTH_LONG).show()
        }
        renderBanners()
    }

    /**
     * The SMS permissions of the newly granted role.
     *
     * Android hands the group over with the role, but a provider that
     * still refuses is exactly the case this screen must survive: the
     * request costs one dialog and is only made when something is
     * actually missing.
     */
    private fun requestSmsPermissions() {
        val missing = arrayOf(
            Manifest.permission.READ_SMS,
            Manifest.permission.RECEIVE_SMS,
            Manifest.permission.SEND_SMS
        ).filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isEmpty()) return
        runCatching { smsPermissionLauncher.launch(missing.toTypedArray()) }
    }

    /**
     * The settings screen where the default SMS app is chosen by hand.
     *
     * The default-apps screen is the first choice; the app's own page is
     * always there as the last resort, and a phone that has neither gets
     * one honest toast.
     */
    private fun openSmsSettings() {
        if (runCatching {
                startActivity(Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS))
            }.isSuccess
        ) {
            Toast.makeText(this, R.string.default_sms_settings_opened, Toast.LENGTH_LONG).show()
            return
        }
        if (runCatching { startActivity(Diagnostics.appDetailsIntent(this)) }.isSuccess) {
            Toast.makeText(this, R.string.default_sms_manual, Toast.LENGTH_LONG).show()
            return
        }
        Toast.makeText(this, R.string.default_sms_unavailable, Toast.LENGTH_LONG).show()
    }

    /**
     * The facts behind the role, in one scrollable dialog.
     *
     * It exists because "no messages" has two very different causes - the
     * app is not eligible for the role, or the role is simply not granted
     * - and only the phone can tell which one it is.
     */
    private fun showDiagnostics() {
        val text = RoleState.diagnosticsReport(this)
        val view = TextView(this).apply {
            this.text = text
            setPadding(dp(20), dp(8), dp(20), dp(8))
            setTextIsSelectable(true)
            textSize = 14f
        }
        val scroll = ScrollView(this).apply { addView(view) }
        AlertDialog.Builder(this)
            .setTitle(R.string.diagnostics_title)
            .setView(scroll)
            .setPositiveButton(android.R.string.ok, null)
            .show()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    private companion object {
        /** Marks a SearchView whose listeners are already installed. */
        const val TAG_SEARCH_BOUND = "search_bound"
    }
}
