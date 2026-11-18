package dev.smsrelay.ui

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.SearchView
import androidx.core.app.ActivityCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import com.google.android.material.color.DynamicColors
import com.google.android.material.floatingactionbutton.FloatingActionButton
import com.google.android.material.tabs.TabLayout
import dev.smsrelay.R
import dev.smsrelay.calls.DialerActivity
import dev.smsrelay.ui.dialer.ContactsAdapter
import dev.smsrelay.ui.dialer.DialerMenus
import dev.smsrelay.ui.dialer.DialerRepository
import dev.smsrelay.ui.dialer.DialerTab
import dev.smsrelay.ui.dialer.FavoritesAdapter
import dev.smsrelay.ui.dialer.RecentsAdapter
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.ui.messages.MessagesBanner
import kotlinx.coroutines.launch

/**
 * The Phone home: the dialer hub of the app.
 *
 * Four tabs over one list - the starred contacts, the phone's own call
 * log, every contact, and the carrier's voicemail - a search field that
 * filters the tab in front of the user, and a dialpad FAB that opens
 * [DialerActivity], the screen that already owns SIM choice, "call via"
 * and the emergency path.
 *
 * Two rules shape this screen:
 *
 *  - nothing here ever places a call. A row taps into `ACTION_DIAL`, so
 *    the number lands in the dialpad and the user presses the green
 *    button themselves. An emergency number in a log row must never
 *    start a call from a single touch, and the dialpad is the one place
 *    that knows how to route one.
 *  - every read is reported as it happened. A permission the user did not
 *    grant, a provider that failed and a genuinely empty list are three
 *    different states with three different sentences, never one empty
 *    list.
 *
 * All colours come from theme attributes of `Theme.SmsRelay.Messages`,
 * which is Material 3 and day/night; dynamic colour is applied when the
 * platform offers it.
 */
class DialerHomeActivity : AppCompatActivity() {

    private lateinit var toolbar: MaterialToolbar
    private lateinit var tabs: TabLayout
    private lateinit var list: RecyclerView
    private lateinit var banners: ViewGroup
    private lateinit var stateColumn: View
    private lateinit var stateText: TextView
    private lateinit var stateButton: MaterialButton
    private lateinit var progress: View
    private lateinit var fab: FloatingActionButton

    private lateinit var recents: RecentsAdapter
    private lateinit var contacts: ContactsAdapter
    private lateinit var favorites: FavoritesAdapter

    /** The tab in front of the user; the only state this screen keeps. */
    private var tab: DialerTab = DialerTab.RECENTS

    /** Everything read for the current tab, before the search filters it. */
    private var recentsItems: List<DialerRepository.RecentCall> = emptyList()
    private var contactItems: List<DialerRepository.DialerContact> = emptyList()
    private var favoriteItems: List<DialerRepository.FavoriteContact> = emptyList()

    /** The current search text; empty when the field is closed. */
    private var query: String = ""

    /**
     * True while a read is in flight.
     *
     * A restore of the search text draws the tab before the read has
     * answered; without this the list would flash "No calls yet" over the
     * spinner, which looks like an empty phone for a moment.
     */
    private var loading: Boolean = true

    /**
     * The read in flight.
     *
     * A tab change while the previous read is still running must not be
     * overwritten by its late answer, so every load bumps the generation
     * and only the newest one paints.
     */
    private var loadGeneration = 0

    /** The permission asked for last, so several can be requested at once. */
    private var pendingPermission: String = ""

    /**
     * The permissions this session already asked for.
     *
     * Android shows the dialog twice and then stops: at that point a tap
     * on the note must not look dead, so it goes to the app's settings
     * page instead of asking again.
     */
    private val askedPermissions = mutableSetOf<String>()

    /**
     * True once the screen has been resumed at least once.
     *
     * `onCreate` selects the tab, and a selection loads the tab; without
     * this flag the first open would read the call log twice, once for the
     * selection and once for the resume that follows it.
     */
    private var resumed = false

    /**
     * The runtime permissions the screen reads with.
     *
     * The result is not inspected: the user may have granted one and
     * denied another, so the screen simply re-reads the tab and draws the
     * banner if a permission is still missing. Asking from here instead of
     * sending the user to the setup screen keeps the promise of the
     * banner, which says the tab needs one tap.
     */
    private val requestPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { _ ->
        // The answer itself is not inspected: the permission may have been
        // granted, refused, or granted for one tab and refused for the
        // other. Redrawing the note and re-reading the tab answers all
        // three, because both paths already ask the system what is granted.
        pendingPermission = ""
        renderBanners()
        loadCurrentTab()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Dynamic colour before the views are inflated: it replaces the
        // theme's palette, so a later call would need a recreate to show.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            DynamicColors.applyToActivityIfAvailable(this)
        }
        WindowCompat.setDecorFitsSystemWindows(window, false)
        setContentView(R.layout.activity_dialer_home)

        toolbar = findViewById(R.id.toolbarDialer)
        tabs = findViewById(R.id.tabDialer)
        list = findViewById(R.id.listDialer)
        banners = findViewById(R.id.layoutDialerBanners)
        stateColumn = findViewById(R.id.layoutDialerState)
        stateText = findViewById(R.id.textDialerState)
        stateButton = findViewById(R.id.buttonDialerState)
        progress = findViewById(R.id.progressDialer)
        fab = findViewById(R.id.fabDialpad)

        toolbar.title = getString(R.string.dialer_title)
        toolbar.setOnMenuItemClickListener(::onMenuItem)
        bindSearch()

        recents = RecentsAdapter(
            onClick = { call -> dial(call.number) },
            onLongClick = ::showRecentActions
        )
        contacts = ContactsAdapter(
            onClick = { contact -> dial(contact.number) },
            onLongClick = ::showContactActions
        )
        favorites = FavoritesAdapter(
            onClick = { favorite -> dial(favorite.number) },
            onLongClick = ::showFavoriteActions
        )

        list.layoutManager = LinearLayoutManager(this)
        list.adapter = recents
        list.setHasFixedSize(true)

        installTabs()
        fab.setOnClickListener { openDialpad(null) }

        // The search belongs to the screen, not to one tab: it comes back
        // with the tab it was typed on, so a rotation does not silently
        // widen a filtered list.
        query = savedInstanceState?.getString(STATE_QUERY).orEmpty()

        // `TabLayout` mirrors this selection and fires its listener, which
        // draws the right adapter and loads that tab; a missing value (a
        // first open, or a bundle from an older build) falls back to the
        // call log.
        tab = DialerTab.of(savedInstanceState?.getInt(STATE_TAB, -1) ?: -1)
        tabs.getTabAt(tab.ordinal)?.select()
        restoreSearch()

        applyInsets()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putInt(STATE_TAB, tab.ordinal)
        outState.putString(STATE_QUERY, query)
    }

    override fun onResume() {
        super.onResume()
        resumed = true
        // A call ends, the log changes, a contact is starred in another
        // app: the visible tab is re-read, never assumed. `onResume` also
        // covers the return from the dialpad and from the permission
        // dialog.
        loadCurrentTab()
        renderBanners()
    }

    // ------------------------------------------------------------- tabs

    /**
     * The four tab labels.
     *
     * They are added here instead of in the layout because the enum is
     * their only source: a tab strip built in XML could drift from the
     * list of tabs the code knows how to draw.
     */
    private fun installTabs() {
        for (value in DialerTab.values()) {
            tabs.addTab(tabs.newTab().setText(value.titleRes).setTag(value))
        }
        tabs.addOnTabSelectedListener(object : TabLayout.OnTabSelectedListener {
            override fun onTabSelected(selected: TabLayout.Tab) {
                tab = selected.tag as? DialerTab ?: DialerTab.RECENTS
                if (resumed) loadCurrentTab()
                renderBanners()
            }

            override fun onTabUnselected(selected: TabLayout.Tab) = Unit
            override fun onTabReselected(selected: TabLayout.Tab) = Unit
        })
    }

    /**
     * The adapter of a tab, or null for the one tab that has no rows.
     *
     * Voicemail is a sentence and a button, not a list, so the tab keeps
     * whatever adapter was there and hides the list instead.
     */
    private fun adapterFor(value: DialerTab): RecyclerView.Adapter<*>? = when (value) {
        DialerTab.RECENTS -> recents
        DialerTab.CONTACTS -> contacts
        DialerTab.FAVORITES -> favorites
        DialerTab.VOICEMAIL -> null
    }

    // ----------------------------------------------------------- loading

    /** Reads whatever the visible tab shows, or draws its state. */
    private fun loadCurrentTab() {
        val generation = ++loadGeneration
        // The one list swaps its adapter per tab. Setting it here instead
        // of in the tab listener means a restored tab and a tapped tab
        // take the same path, and the voicemail tab can hide the list.
        val adapter = adapterFor(tab)
        if (adapter != null) {
            if (list.adapter !== adapter) list.adapter = adapter
            list.visibility = View.VISIBLE
        }
        when (tab) {
            DialerTab.RECENTS -> loadRecents(generation)
            DialerTab.CONTACTS -> loadContacts(generation)
            DialerTab.FAVORITES -> loadFavorites(generation)
            DialerTab.VOICEMAIL -> loadVoicemail()
        }
    }

    private fun loadRecents(generation: Int) {
        if (!DialerRepository.canReadCallLog(this)) {
            showRecentsPermission()
            return
        }
        showLoading()
        lifecycleScope.launch {
            val result = DialerRepository.recents(this@DialerHomeActivity)
            if (generation != loadGeneration) return@launch
            // The permission can be revoked between the check above and
            // the read; the repository reports that instead of returning
            // an empty log, so the same sentence is drawn either way.
            if (result.denied) {
                showRecentsPermission()
                return@launch
            }
            recentsItems = result.items
            if (result.failed && recentsItems.isEmpty()) {
                showState(getString(R.string.dialer_error_read), null, null)
            } else {
                renderRecents()
            }
        }
    }

    private fun loadContacts(generation: Int) {
        if (!DialerRepository.canReadContacts(this)) {
            showContactsPermission()
            return
        }
        showLoading()
        lifecycleScope.launch {
            val result = DialerRepository.contacts(this@DialerHomeActivity)
            if (generation != loadGeneration) return@launch
            if (result.denied) {
                showContactsPermission()
                return@launch
            }
            contactItems = result.items
            if (result.failed && contactItems.isEmpty()) {
                showState(getString(R.string.dialer_error_read), null, null)
            } else {
                renderContacts()
            }
        }
    }

    private fun loadFavorites(generation: Int) {
        if (!DialerRepository.canReadContacts(this)) {
            showContactsPermission()
            return
        }
        showLoading()
        lifecycleScope.launch {
            val result = DialerRepository.favorites(this@DialerHomeActivity)
            if (generation != loadGeneration) return@launch
            if (result.denied) {
                showContactsPermission()
                return@launch
            }
            favoriteItems = result.items
            if (result.failed && favoriteItems.isEmpty()) {
                showState(getString(R.string.dialer_error_read), null, null)
            } else {
                renderFavorites()
            }
        }
    }

    /**
     * The missing call log permission.
     *
     * It is a full state with a button, not a banner: the tab has nothing
     * to show, so the sentence has to be where the list would be. The
     * banner above would only add a second copy of the same words.
     */
    private fun showRecentsPermission() {
        recentsItems = emptyList()
        recents.submitList(emptyList())
        showState(
            text = getString(R.string.dialer_permission_call_log),
            button = R.string.dialer_permission_grant
        ) { askFor(Manifest.permission.READ_CALL_LOG) }
        renderBanners()
    }

    /** The missing contacts permission, for Contacts and Favorites. */
    private fun showContactsPermission() {
        if (tab == DialerTab.FAVORITES) {
            favoriteItems = emptyList()
            favorites.submitList(emptyList())
        } else {
            contactItems = emptyList()
            contacts.submitList(emptyList())
        }
        showState(
            text = getString(R.string.dialer_permission_contacts),
            button = R.string.dialer_permission_grant
        ) { askFor(Manifest.permission.READ_CONTACTS) }
        renderBanners()
    }

    /**
     * The voicemail tab.
     *
     * There is no visual voicemail to show and this build does not read
     * the carrier's voicemail provider, so the tab is one honest sentence
     * and one button. The number is read through `TelecomManager`, which
     * is the carrier's own answer, never a guess.
     */
    private fun loadVoicemail() {
        list.visibility = View.GONE
        progress.visibility = View.GONE
        showState(
            text = getString(R.string.dialer_voicemail_placeholder),
            button = R.string.dialer_voicemail_call
        ) { callVoicemail() }
    }

    // ---------------------------------------------------------- painting

    private fun renderRecents() {
        // A search must not turn "this tab may not read the log" into "no
        // matching calls": the permission state outranks the filter.
        if (!DialerRepository.canReadCallLog(this)) {
            showRecentsPermission()
            return
        }
        loading = false
        val visible = filterRecents(recentsItems, query)
        progress.visibility = View.GONE
        list.visibility = View.VISIBLE
        recents.submitList(visible)
        when {
            visible.isNotEmpty() -> showState(null, null, null)
            query.trim().isNotEmpty() -> showState(
                getString(R.string.messages_no_results), null, null
            )
            else -> showState(getString(R.string.dialer_recents_empty), null, null)
        }
    }

    private fun renderContacts() {
        if (!DialerRepository.canReadContacts(this)) {
            showContactsPermission()
            return
        }
        loading = false
        val visible = filterContacts(contactItems, query)
        progress.visibility = View.GONE
        list.visibility = View.VISIBLE
        contacts.submitList(visible)
        when {
            visible.isNotEmpty() -> showState(null, null, null)
            query.trim().isNotEmpty() -> showState(
                getString(R.string.messages_no_results), null, null
            )
            else -> showState(getString(R.string.dialer_contacts_empty), null, null)
        }
    }

    private fun renderFavorites() {
        if (!DialerRepository.canReadContacts(this)) {
            showContactsPermission()
            return
        }
        loading = false
        val visible = filterFavorites(favoriteItems, query)
        progress.visibility = View.GONE
        list.visibility = View.VISIBLE
        favorites.submitList(visible)
        when {
            visible.isNotEmpty() -> showState(null, null, null)
            query.trim().isNotEmpty() -> showState(
                getString(R.string.messages_no_results), null, null
            )
            else -> showState(
                text = getString(R.string.dialer_favorites_empty),
                button = R.string.dialer_open_contacts
            ) { selectTab(DialerTab.CONTACTS) }
        }
    }

    private fun showLoading() {
        loading = true
        list.visibility = View.VISIBLE
        progress.visibility = View.VISIBLE
        stateColumn.visibility = View.GONE
    }

    /**
     * The one state column: a sentence and an optional button.
     *
     * A null [text] hides the column, so "the tab has rows" and "the tab
     * has something to say" can never both be true.
     */
    private fun showState(text: String?, button: Int?, onClick: (() -> Unit)?) {
        loading = false
        if (text == null) {
            stateColumn.visibility = View.GONE
            progress.visibility = View.GONE
            return
        }
        stateText.text = text
        progress.visibility = View.GONE
        if (button != null && onClick != null) {
            stateButton.setText(button)
            stateButton.visibility = View.VISIBLE
            stateButton.setOnClickListener { onClick() }
        } else {
            stateButton.visibility = View.GONE
        }
        stateColumn.visibility = View.VISIBLE
    }

    // ------------------------------------------------------------ search

    private fun bindSearch() {
        val item = toolbar.menu.findItem(R.id.action_search) ?: return
        item.title = getString(R.string.dialer_search)
        item.setOnActionExpandListener(object : MenuItem.OnActionExpandListener {
            override fun onMenuItemActionExpand(item: MenuItem): Boolean {
                // The SearchView is created by the menu the first time the
                // item expands; configuring it here covers that moment.
                configureSearch(item.actionView as? SearchView)
                return true
            }

            override fun onMenuItemActionCollapse(item: MenuItem): Boolean {
                query = ""
                renderCurrent()
                return true
            }
        })
        configureSearch(item.actionView as? SearchView)
    }

    /**
     * Re-opens the search field with the text of the saved instance.
     *
     * The field itself is a menu action view, and its text is not part of
     * the activity's saved state; without this a rotated screen would show
     * an unfiltered list under a field the user still believes is
     * filtering.
     */
    private fun restoreSearch() {
        if (query.isEmpty()) return
        val item = toolbar.menu.findItem(R.id.action_search) ?: return
        item.expandActionView()
        (item.actionView as? SearchView)?.setQuery(query, false)
    }

    /** Wires one SearchView to the in-memory filter of the visible tab. */
    private fun configureSearch(view: SearchView?) {
        if (view == null || view.tag == TAG_SEARCH_BOUND) return
        view.tag = TAG_SEARCH_BOUND
        view.queryHint = getString(R.string.dialer_search_hint)
        view.setOnQueryTextListener(object : SearchView.OnQueryTextListener {
            override fun onQueryTextSubmit(text: String?): Boolean {
                query = text.orEmpty()
                renderCurrent()
                view.clearFocus()
                return true
            }

            override fun onQueryTextChange(text: String?): Boolean {
                query = text.orEmpty()
                renderCurrent()
                return true
            }
        })
        view.setOnCloseListener {
            query = ""
            renderCurrent()
            false
        }
    }

    /** Re-applies the filter to the tab that is already in memory. */
    private fun renderCurrent() {
        // Nothing to filter yet: the read that is in flight will draw the
        // list with this query when it answers.
        if (loading) return
        when (tab) {
            DialerTab.RECENTS -> renderRecents()
            DialerTab.CONTACTS -> renderContacts()
            DialerTab.FAVORITES -> renderFavorites()
            DialerTab.VOICEMAIL -> Unit
        }
    }

    /**
     * Case-insensitive search over the name and the number.
     *
     * It filters what was already read instead of querying the provider
     * again: a keystroke must never touch the call log or the contacts
     * database. The formatted number is searched too, so "0912 34" finds
     * a row the phone stores with a country code.
     */
    private fun filterRecents(
        items: List<DialerRepository.RecentCall>,
        raw: String
    ): List<DialerRepository.RecentCall> {
        val needle = raw.trim().lowercase()
        if (needle.isEmpty()) return items
        return items.filter { call ->
            call.cachedName?.lowercase()?.contains(needle) == true ||
                dialerNumber(call.number).contains(needle) ||
                call.number.contains(needle)
        }
    }

    private fun filterContacts(
        items: List<DialerRepository.DialerContact>,
        raw: String
    ): List<DialerRepository.DialerContact> {
        val needle = raw.trim().lowercase()
        if (needle.isEmpty()) return items
        return items.filter { contact ->
            contact.name.lowercase().contains(needle) ||
                dialerNumber(contact.number).contains(needle) ||
                contact.number.contains(needle)
        }
    }

    private fun filterFavorites(
        items: List<DialerRepository.FavoriteContact>,
        raw: String
    ): List<DialerRepository.FavoriteContact> {
        val needle = raw.trim().lowercase()
        if (needle.isEmpty()) return items
        return items.filter { favorite ->
            favorite.name.lowercase().contains(needle) ||
                dialerNumber(favorite.number).contains(needle) ||
                favorite.number.contains(needle)
        }
    }

    private fun dialerNumber(raw: String): String =
        SmsRepository.displayNumber(raw).lowercase()

    // ------------------------------------------------------------ actions

    private fun selectTab(value: DialerTab) {
        tabs.getTabAt(value.ordinal)?.select()
    }

    /**
     * Opens the dialpad with [number] in the field, or empty.
     *
     * `ACTION_DIAL` and not a call: the platform and [DialerActivity] both
     * apply the emergency rules, and the user presses the green button.
     */
    private fun openDialpad(number: String?) {
        val intent = Intent(this, DialerActivity::class.java)
            .setAction(Intent.ACTION_DIAL)
        if (!number.isNullOrBlank()) {
            intent.data = Uri.parse("tel:" + Uri.encode(number))
        }
        try {
            startActivity(intent)
        } catch (e: ActivityNotFoundException) {
            Toast.makeText(this, R.string.dialer_call_failed, Toast.LENGTH_LONG).show()
        }
    }

    /**
     * The one place a row turns into a dialpad number.
     *
     * A row without a number (a private caller) gets one honest toast
     * instead of an empty dialpad that looks like a bug.
     */
    private fun dial(number: String) {
        if (number.isBlank()) {
            Toast.makeText(this, R.string.dialer_no_number, Toast.LENGTH_SHORT).show()
            return
        }
        openDialpad(number)
    }

    private fun showRecentActions(call: DialerRepository.RecentCall) {
        val actions = mutableListOf<DialerMenus.Action>()
        if (call.number.isNotBlank()) {
            actions += DialerMenus.Action(getString(R.string.dialer_call)) {
                openDialpad(call.number)
            }
            actions += DialerMenus.Action(getString(R.string.dialer_action_sms)) {
                openThread(call.number, call.cachedName)
            }
            actions += DialerMenus.Action(getString(R.string.dialer_action_copy)) {
                copyNumber(call.number)
            }
        }
        actions += DialerMenus.Action(
            label = getString(R.string.dialer_action_delete),
            destructive = true
        ) {
            deleteCall(call)
        }
        DialerMenus.show(this, actions)
    }

    private fun showContactActions(contact: DialerRepository.DialerContact) {
        showPersonActions(contact.number, contact.name)
    }

    private fun showFavoriteActions(favorite: DialerRepository.FavoriteContact) {
        showPersonActions(favorite.number, favorite.name)
    }

    /**
     * The three actions a contact and a favorite share.
     *
     * A row without a number cannot reach any of them, so it says why in
     * one toast instead of opening a sheet of dead entries. The
     * repository never returns such a row today; the guard is here so a
     * future read cannot turn a contact into a blank sheet.
     */
    private fun showPersonActions(number: String, name: String) {
        if (number.isBlank()) {
            Toast.makeText(this, R.string.dialer_no_number, Toast.LENGTH_SHORT).show()
            return
        }
        val actions = listOf(
            DialerMenus.Action(getString(R.string.dialer_call)) { openDialpad(number) },
            DialerMenus.Action(getString(R.string.dialer_action_sms)) { openThread(number, name) },
            DialerMenus.Action(getString(R.string.dialer_action_copy)) { copyNumber(number) }
        )
        DialerMenus.show(this, actions)
    }

    /**
     * The SMS action: the thread screen of this number.
     *
     * [ThreadActivity.intentForAddress] is the same entry point the
     * Messages list and the contact picker use, so a number that has no
     * thread yet still opens a composer.
     */
    private fun openThread(number: String, name: String?) {
        startActivity(ThreadActivity.intentForAddress(this, number, name))
    }

    private fun copyNumber(number: String) {
        if (number.isBlank()) {
            Toast.makeText(this, R.string.dialer_copy_failed, Toast.LENGTH_SHORT).show()
            return
        }
        val clipboard = getSystemService(ClipboardManager::class.java)
        if (clipboard == null) {
            Toast.makeText(this, R.string.dialer_copy_failed, Toast.LENGTH_SHORT).show()
            return
        }
        clipboard.setPrimaryClip(ClipData.newPlainText(getString(R.string.app_name), number))
        Toast.makeText(this, R.string.dialer_copied, Toast.LENGTH_SHORT).show()
    }

    /**
     * Deletes one call from the log and re-reads the tab.
     *
     * The write needs `WRITE_CALL_LOG`, a permission of its own: the read
     * is not enough. A refusal is one toast, and the row stays, because
     * pretending it went away would be a lie.
     */
    private fun deleteCall(call: DialerRepository.RecentCall) {
        lifecycleScope.launch {
            val deleted = DialerRepository.deleteCall(this@DialerHomeActivity, call.id)
            if (deleted) {
                recentsItems = recentsItems.filterNot { it.id == call.id }
                renderRecents()
            } else {
                Toast.makeText(
                    this@DialerHomeActivity,
                    R.string.dialer_delete_failed,
                    Toast.LENGTH_SHORT
                ).show()
            }
        }
    }

    /**
     * Dials the carrier's voicemail number.
     *
     * `TelecomManager.getVoiceMailNumber` is the carrier's answer; there
     * is no fallback number, because dialling a made-up one would reach a
     * stranger. A phone that has none gets one sentence instead.
     */
    private fun callVoicemail() {
        lifecycleScope.launch {
            val number = DialerRepository.voicemailNumber(this@DialerHomeActivity)
            if (number.isNullOrBlank()) {
                Toast.makeText(
                    this@DialerHomeActivity,
                    R.string.dialer_voicemail_missing,
                    Toast.LENGTH_LONG
                ).show()
            } else {
                openDialpad(number)
            }
        }
    }

    // -------------------------------------------------------- permissions

    /**
     * Asks for one runtime permission the visible tab needs.
     *
     * Android answers the dialog twice and then refuses to show it again.
     * Once that point is reached the note goes to the app's settings page,
     * because a control that does nothing is worse than one that opens a
     * screen the user can act on.
     */
    private fun askFor(permission: String) {
        // A second sheet while one is open is dropped: Android answers the
        // first only, and the note is still drawn for the other.
        if (pendingPermission.isNotEmpty()) return
        val canAskAgain = ActivityCompat.shouldShowRequestPermissionRationale(this, permission)
        if (permission in askedPermissions && !canAskAgain) {
            openAppSettings()
            return
        }
        askedPermissions += permission
        pendingPermission = permission
        try {
            requestPermission.launch(permission)
        } catch (e: Exception) {
            pendingPermission = ""
            openAppSettings()
        }
    }

    /**
     * The fallback when the dialog cannot be shown (a second denial, a
     * policy that forbids asking): the app's own page in the settings,
     * where the switch is one tap away.
     */
    private fun openAppSettings() {
        try {
            startActivity(
                Intent(
                    android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                    Uri.fromParts("package", packageName, null)
                )
            )
        } catch (e: Exception) {
            Toast.makeText(this, R.string.dialer_permission_settings, Toast.LENGTH_LONG).show()
        }
    }

    // ------------------------------------------------------------ banners

    /**
     * The one note above the list.
     *
     * A missing *read* permission is said in the middle of the screen,
     * where its button is, so a card here would repeat it. This card
     * exists for the permission that has no other place: the call log can
     * be read but not written, so "Delete from call log" would fail with
     * nothing on screen explaining why. It is quiet, one line, and only
     * while the log is the visible tab.
     */
    private fun renderBanners() {
        banners.removeAllViews()
        if (tab != DialerTab.RECENTS) return
        if (!DialerRepository.canReadCallLog(this)) return
        if (DialerRepository.canWriteCallLog(this)) return
        MessagesBanner.addTo(
            column = banners,
            context = this,
            text = getString(R.string.dialer_permission_write_banner)
        ) { askFor(Manifest.permission.WRITE_CALL_LOG) }
    }

    // -------------------------------------------------------------- menu

    private fun onMenuItem(item: MenuItem): Boolean = when (item.itemId) {
        R.id.action_dialer -> {
            // "Dialer" in this screen's menu is the full dialpad, the same
            // screen the FAB opens.
            openDialpad(null)
            true
        }
        R.id.action_setup -> {
            startActivity(Intent(this, MainActivity::class.java))
            true
        }
        else -> false
    }

    // ------------------------------------------------------------- insets

    /**
     * Edge to edge without guessing at a status bar height.
     *
     * The content column takes the top, left and right insets, so the
     * toolbar, the tabs and the rows all sit inside the safe area. The
     * list takes the bottom inset plus the space the FAB floats over, so
     * the last row can still be scrolled clear of both, and the FAB moves
     * up by the navigation inset.
     */
    private fun applyInsets() {
        val fabParams = fab.layoutParams as ViewGroup.MarginLayoutParams
        val fabMarginEnd = fabParams.marginEnd
        val fabMarginBottom = fabParams.bottomMargin
        val content = findViewById<View>(R.id.layoutDialerContent)
        val listBottom = resources.getDimensionPixelSize(R.dimen.messages_list_bottom_padding)
        ViewCompat.setOnApplyWindowInsetsListener(findViewById(R.id.rootDialerHome)) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            content.updatePadding(top = bars.top, left = bars.left, right = bars.right)
            list.updatePadding(bottom = listBottom + bars.bottom)
            fabParams.marginEnd = fabMarginEnd + bars.right
            fabParams.marginBottom = fabMarginBottom + bars.bottom
            fab.layoutParams = fabParams
            insets
        }
    }

    private companion object {
        /** The selected tab across a rotation or a process restart. */
        const val STATE_TAB = "dialer_tab"

        /** The search text, so a rotation keeps the list filtered. */
        const val STATE_QUERY = "dialer_query"

        /** Marks a SearchView whose listeners are already installed. */
        const val TAG_SEARCH_BOUND = "search_bound"
    }
}
