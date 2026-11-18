package dev.smsrelay.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.Editable
import android.text.TextWatcher
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.EditText
import android.widget.ImageButton
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.color.DynamicColors
import com.google.android.material.snackbar.Snackbar
import dev.smsrelay.R
import dev.smsrelay.SettingsStore
import dev.smsrelay.messages.SmsMessage
import dev.smsrelay.messages.SmsObserver
import dev.smsrelay.messages.SmsPage
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.messages.SmsThread
import dev.smsrelay.messages.SmsType
import dev.smsrelay.net.ImportMessage
import dev.smsrelay.net.ImportResult
import dev.smsrelay.net.RelayClient
import dev.smsrelay.sms.SimCards
import dev.smsrelay.sms.SmsSender
import dev.smsrelay.ui.messages.AvatarDrawable
import dev.smsrelay.ui.messages.MessageDates
import dev.smsrelay.ui.messages.MessagesBanner
import dev.smsrelay.ui.messages.ThreadAdapter
import dev.smsrelay.ui.messages.ThreadLabels
import dev.smsrelay.ui.messages.ThreadRows
import dev.smsrelay.util.RoleState
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * One conversation: its messages, and a composer for one to one SMS.
 *
 * It shows the messages of one thread of the phone's SMS store with day
 * separators, incoming and outgoing bubbles and an honest status line.
 * A one to one thread also has a composer: the text is handed to the
 * radio, the accepted message is written back to the provider and then
 * mirrored to the relay as outbound history. A group or an MMS only
 * conversation keeps the read-only note in the same place, so the screen
 * never offers a send that cannot work.
 *
 * Opening the thread marks it read. A phone that does not let this app
 * write the store keeps the unread dot; nothing here crashes on that.
 *
 * The screen can be started three ways:
 *  - [intentForThread] with an existing thread row from the list, which
 *    already says whether the thread is a group and what it is called;
 *  - [intentForAddress] with a number from the contact picker, which is
 *    looked up in the existing one to one conversations instead of being
 *    written to the provider;
 *  - by the platform, restoring a screen that only carries its extras.
 *
 * A group is never dialled: the header hides the call button, because
 * the only number the screen could offer would be one member's private
 * line, not the conversation.
 */
class ThreadActivity : AppCompatActivity() {

    private lateinit var toolbar: MaterialToolbar
    private lateinit var list: RecyclerView
    private lateinit var adapter: ThreadAdapter
    private lateinit var state: TextView
    private lateinit var progress: View
    private lateinit var loadOlder: View
    private lateinit var banners: ViewGroup
    private lateinit var composer: View
    private lateinit var composerInput: EditText
    private lateinit var sendButton: ImageButton
    private lateinit var sendProgress: View
    private lateinit var composerNotice: TextView
    private lateinit var notice: TextView
    private lateinit var avatar: TextView
    private lateinit var title: TextView
    private lateinit var call: View

    private var threadId: Long = -1L

    /** The other side of a one to one conversation; empty for a group. */
    private var address: String = ""

    private var displayName: String? = null

    /** The group label carried from the list, when the row was a group. */
    private var groupLabel: String? = null

    /** How many people the list said are in the group. */
    private var groupCount: Int = 0

    /** True when the conversation has more than one person on the other side. */
    private var isGroup: Boolean = false

    /**
     * True when the store refused to be read.
     *
     * A thread that cannot be read cannot be sent to either: the screen
     * would insert a row it is not allowed to see. The composer is
     * replaced by the same notice the list shows.
     */
    private var readFailed: Boolean = false

    /** True while a send is in flight; the send button is inert then. */
    private var sending: Boolean = false

    /**
     * True once a page has actually been read.
     *
     * The composer asks whether the thread is MMS-only, and "no SMS rows
     * but a snippet" only means that *after* a read. Before the first
     * page arrives every thread looks empty, so the flag keeps the bar
     * from flashing an MMS notice on a normal conversation.
     */
    private var pageLoaded: Boolean = false

    /** The settings of this device, read lazily by the send path. */
    private val settings by lazy { SettingsStore(this) }

    /**
     * The provider's one-line snippet of the conversation.
     *
     * It is the only view of an MMS message this app has, so it is what
     * tells an MMS-only thread apart from an empty one.
     */
    private var snippet: String = ""

    /** The messages currently shown, oldest first. */
    private val messages = ArrayList<SmsMessage>()

    private var hasOlder = false
    private var loadingOlder = false
    private var observer: SmsObserver? = null

    /** Bumped per load, so a slow answer cannot overwrite a newer one. */
    private var loadGeneration = 0

    /**
     * Forces the next page to end at the newest message.
     *
     * A send reloads the thread itself, but the provider observer usually
     * wakes up on the very same write and its load can win the race. Its
     * pass would then keep the old scroll position and leave the fresh
     * bubble just below the fold, so the wish to land on it is recorded
     * here and honoured by whichever pass arrives first.
     */
    private var scrollToEndNext = false

    /**
     * False until the thread has been shown once.
     *
     * The first load scrolls to the newest message; every later load
     * (returning from the dialer, a provider change) keeps the position
     * the user was reading.
     */
    private var opened = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            DynamicColors.applyToActivityIfAvailable(this)
        }
        WindowCompat.setDecorFitsSystemWindows(window, false)
        setContentView(R.layout.activity_thread)

        toolbar = findViewById(R.id.toolbarThread)
        list = findViewById(R.id.listMessages)
        state = findViewById(R.id.textThreadState)
        progress = findViewById(R.id.progressThread)
        loadOlder = findViewById(R.id.buttonLoadOlder)
        banners = findViewById(R.id.layoutThreadBanners)
        composer = findViewById(R.id.layoutComposer)
        composerInput = findViewById(R.id.inputComposer)
        sendButton = findViewById(R.id.buttonSend)
        sendProgress = findViewById(R.id.progressSend)
        composerNotice = findViewById(R.id.textComposerNotice)
        notice = findViewById(R.id.textThreadNotice)
        avatar = findViewById(R.id.textThreadAvatar)
        title = findViewById(R.id.textThreadTitle)
        call = findViewById(R.id.buttonThreadCall)

        threadId = intent.getLongExtra(EXTRA_THREAD_ID, -1L)
        address = intent.getStringExtra(EXTRA_ADDRESS).orEmpty()
        displayName = intent.getStringExtra(EXTRA_NAME)
        isGroup = intent.getBooleanExtra(EXTRA_IS_GROUP, false)
        groupLabel = intent.getStringExtra(EXTRA_LABEL)
        groupCount = intent.getIntExtra(EXTRA_GROUP_COUNT, 0)
        snippet = intent.getStringExtra(EXTRA_SNIPPET).orEmpty()
        // A group never has a single address. The extra is ignored rather
        // than trusted, because dialling one member of a group would be a
        // private call the user did not ask for.
        if (isGroup) address = ""

        toolbar.setNavigationOnClickListener { finish() }

        adapter = ThreadAdapter()
        list.layoutManager = LinearLayoutManager(this)
        list.adapter = adapter
        list.setHasFixedSize(true)

        loadOlder.setOnClickListener { loadOlderPage() }
        call.setOnClickListener { dial() }
        bindComposer()

        applyInsets()
        renderHeader()
        renderComposer()
        showLoading()
    }

    override fun onResume() {
        super.onResume()
        // The role can be granted while this screen sits behind the
        // settings; the note is re-read, never assumed.
        renderBanners()
        // The master sending switch can change there too, and the bar has
        // to say so the moment the user comes back.
        renderComposer()
    }

    override fun onStart() {
        super.onStart()
        val created = SmsObserver(this) { load(keepScroll = true) }
        observer = created
        created.start()
        // Loading here instead of in `onCreate` covers both the first open
        // and the return from the dialer that this screen starts.
        load(keepScroll = opened)
        opened = true
    }

    override fun onStop() {
        super.onStop()
        observer?.stop()
        observer = null
    }

    // ------------------------------------------------------------ insets

    /**
     * Edge to edge: the whole column takes the top, left and right
     * insets, and the composer takes the bottom inset plus the keyboard.
     *
     * The keyboard inset is what `adjustResize` no longer does by itself
     * once the window fits the system windows: the composer is padded by
     * the height the IME covers, so the field and the send button stay
     * visible above it, and the list keeps the rest of the screen.
     *
     * The top inset is padding of the column, never of the toolbar: the
     * toolbar has a fixed `?attr/actionBarSize` height, and a status bar
     * worth of padding inside it would squeeze the name into the rest.
     */
    private fun applyInsets() {
        val root = findViewById<View>(R.id.rootThread)
        val composerBottom = dp(8)
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            root.updatePadding(top = bars.top, left = bars.left, right = bars.right)
            // The bar is at the bottom of the column, so its own padding
            // has to cover both the navigation bar and the keyboard.
            composer.updatePadding(bottom = composerBottom + maxOf(bars.bottom, ime.bottom))
            composerNotice.updatePadding(
                bottom = dp(10) + maxOf(bars.bottom, ime.bottom)
            )
            // Scrolling while the keyboard opens must end at the newest
            // message, not wherever the shrunken viewport left the list.
            if (ime.bottom > 0 && adapter.itemCount > 0) {
                list.scrollToPosition(adapter.itemCount - 1)
            }
            insets
        }
    }

    // ------------------------------------------------------------ loading

    /** Reads the newest page of the thread and shows it. */
    private fun load(keepScroll: Boolean = false) {
        val generation = ++loadGeneration
        lifecycleScope.launch {
            val app = this@ThreadActivity
            // The conversation row is the source of truth for the label,
            // the group flag and the snippet: it also covers a screen the
            // platform restored with nothing but its thread id, and a
            // thread that gained a member after the list was drawn.
            if (threadId > 0L) {
                val row = SmsRepository.threadFor(app, threadId)
                if (generation != loadGeneration) return@launch
                if (row != null) {
                    applyThread(row)
                }
            }
            if (threadId <= 0L && address.isNotBlank()) {
                threadId = SmsRepository.threadIdForAddress(app, address)
                if (threadId > 0L) {
                    val row = SmsRepository.threadFor(app, threadId)
                    if (generation != loadGeneration) return@launch
                    if (row != null) applyThread(row)
                }
            }
            if (address.isBlank() && !isGroup && threadId > 0L) {
                address = SmsRepository.addressForThread(app, threadId)
            }
            if (displayName.isNullOrBlank() && address.isNotBlank()) {
                displayName = SmsRepository.nameForAddress(app, address)
            }
            renderHeader()

            if (threadId <= 0L) {
                // No thread in the store yet: an honest empty screen, not
                // a crash and not an invented conversation.
                if (generation != loadGeneration) return@launch
                messages.clear()
                showPage(SmsPage(emptyList(), false, false), keepScroll)
                return@launch
            }

            val page = SmsRepository.messages(app, threadId)
            if (generation != loadGeneration) return@launch
            messages.clear()
            messages.addAll(page.messages)
            // The messages are shown first and the read state is written
            // after, so the bubbles appear at once and the dot clears right
            // behind them. The observer wakes up on that write, but the
            // next pass finds nothing unread, changes no row and stops
            // there instead of looping.
            showPage(page, keepScroll)
            // Only an actually unread page triggers a write. `update` on
            // the SMS table notifies the observer even when it changes
            // nothing, so writing unconditionally would make the observer
            // reload the thread every 300 ms forever.
            if (page.messages.any { !it.read && it.type == SmsType.INBOX }) {
                SmsRepository.markThreadRead(app, threadId)
            }
        }
    }

    /**
     * Takes the conversation row as the truth about who is talking.
     *
     * The list passes the same facts as extras, but a restored screen has
     * none of them and a group can gain or lose a member while the screen
     * is away, so the row is read again and wins.
     */
    private fun applyThread(row: SmsThread) {
        isGroup = row.isGroup
        address = row.address
        displayName = row.name
        groupLabel = if (row.isGroup) ThreadLabels.of(this, row) else null
        groupCount = row.recipientCount
        snippet = row.snippet
        renderComposer()
    }

    /** Prepends the page before the oldest message on screen. */
    private fun loadOlderPage() {
        if (loadingOlder || !hasOlder || threadId <= 0L) return
        val oldest = messages.firstOrNull() ?: return
        loadingOlder = true
        loadOlder.visibility = View.GONE
        lifecycleScope.launch {
            val page = SmsRepository.olderMessages(this@ThreadActivity, threadId, oldest)
            loadingOlder = false
            if (page.failed || page.messages.isEmpty()) {
                hasOlder = false
                loadOlder.visibility = View.GONE
                return@launch
            }
            messages.addAll(0, page.messages)
            hasOlder = page.hasOlder
            // Prepending at index 0 keeps the message the user was reading
            // in place: the diff inserts the older rows above it.
            submit(scrollToEnd = false)
            loadOlder.visibility = if (hasOlder) View.VISIBLE else View.GONE
        }
    }

    /** Hands the current messages to the adapter, grouped by day. */
    private fun showPage(page: SmsPage, keepScroll: Boolean) {
        progress.visibility = View.GONE
        hasOlder = page.hasOlder
        pageLoaded = true
        readFailed = page.failed && messages.isEmpty()
        // A send asks for the end explicitly: the observer's pass can beat
        // the reload, and the bubble must still be visible whichever load
        // wins. The wish is consumed once, so the next pass is normal.
        val toEnd = !keepScroll || scrollToEndNext
        scrollToEndNext = false
        if (readFailed) {
            notice.visibility = View.GONE
            showState(getString(R.string.messages_error_read))
            renderComposer()
            return
        }
        when {
            messages.isNotEmpty() -> showState(null)
            // The provider lists MMS threads here too, and this app reads
            // the SMS table only. A thread whose SMS side is empty but
            // whose snippet is not is an MMS conversation, and saying so
            // is better than pretending the conversation never happened.
            snippet.isNotBlank() -> showState(getString(R.string.messages_mms_unsupported))
            else -> showState(getString(R.string.thread_empty))
        }
        showMmsNote()
        renderComposer()
        submit(scrollToEnd = toEnd)
        loadOlder.visibility = if (hasOlder) View.VISIBLE else View.GONE
    }

    /**
     * The one line note under a thread that holds both kinds of message.
     *
     * It appears only when the provider's snippet of the newest message
     * is not the body of the newest SMS on screen, which is the cheap way
     * to say "there is an MMS here that this screen cannot draw". The
     * comparison tolerates the provider's truncation by accepting either
     * value as a prefix of the other, so a long SMS does not raise a
     * false note.
     */
    private fun showMmsNote() {
        val newest = messages.lastOrNull()
        val visible = messages.isNotEmpty() &&
            snippet.isNotBlank() &&
            newest != null &&
            !sameText(snippet, newest.body)
        if (visible) {
            notice.text = getString(R.string.messages_mms_partial)
            notice.visibility = View.VISIBLE
        } else {
            notice.visibility = View.GONE
        }
    }

    /** True when two snippet-sized strings describe the same message. */
    private fun sameText(a: String, b: String): Boolean {
        val left = a.trim().replace(Regex("\\s+"), " ").lowercase()
        val right = b.trim().replace(Regex("\\s+"), " ").lowercase()
        if (left.isEmpty() || right.isEmpty()) return false
        return left.startsWith(right) || right.startsWith(left)
    }

    /**
     * Submits the rows.
     *
     * The scroll runs inside the commit callback, not right after
     * `submitList`: `ListAdapter` diffs on a background thread, so at that
     * moment the list still holds the previous rows and scrolling would
     * land on the wrong position.
     */
    private fun submit(scrollToEnd: Boolean) {
        val labels = simLabels()
        val rows = ThreadRows.rows(
            messages = messages,
            keyOf = { message -> MessageDates.day(message.dateMillis).toEpochDay() },
            labelOf = { key -> MessageDates.dayHeaderForEpochDay(this, key) },
            noteOf = { message -> bubbleNote(message, labels) }
        )
        adapter.submitList(rows) {
            if (scrollToEnd && rows.isNotEmpty()) {
                list.scrollToPosition(rows.size - 1)
            }
        }
    }

    /**
     * The word after the time under a bubble.
     *
     * The failure and the pocket states come first: they are the facts a
     * person needs. A SIM note is added only when the phone still reports
     * that card, so nothing is guessed.
     */
    private fun bubbleNote(message: SmsMessage, labels: Map<Int, String>): String? = when {
        message.type == SmsType.FAILED -> getString(R.string.message_failed)
        message.type == SmsType.DRAFT -> getString(R.string.message_draft)
        message.isPending -> getString(R.string.message_sending)
        else -> labels[message.subId]
    }

    /**
     * "subscription id -> SIM label", read once per load.
     *
     * Only the cards the phone still reports are in the map, so a message
     * from a removed card shows no SIM note instead of a wrong one.
     */
    private fun simLabels(): Map<Int, String> = try {
        SimCards.active(this).associate { card ->
            card.subscriptionId to SimCards.label(this, card)
        }
    } catch (e: Exception) {
        emptyMap()
    }

    // ------------------------------------------------------------ header

    /**
     * The toolbar: initials, the name, the group label or the number, and
     * the call action for a one to one conversation.
     *
     * A group hides the call button entirely. Its only address would be
     * one member's private number, and a call placed from a group header
     * would silently ring that person alone, so the button is not offered
     * rather than offered and disabled.
     */
    private fun renderHeader() {
        val name = displayName
        val label = when {
            isGroup -> groupLabel?.takeIf { it.isNotBlank() }
                ?: if (groupCount > 0) {
                    getString(R.string.messages_group_label, groupCount)
                } else {
                    getString(R.string.messages_group)
                }
            !name.isNullOrBlank() -> name
            else -> SmsRepository.displayNumber(address).ifBlank {
                getString(R.string.messages_unknown_sender)
            }
        }
        title.text = label
        avatar.text = AvatarDrawable.initials(label)
        avatar.background = AvatarDrawable.circle(
            if (isGroup) "group:$threadId" else address
        )
        avatar.contentDescription = getString(R.string.messages_avatar_of, label)
        val callable = !isGroup && address.isNotBlank()
        call.visibility = if (callable) View.VISIBLE else View.GONE
        call.isEnabled = callable
        call.alpha = if (callable) 1f else 0.4f
    }

    /**
     * Starts the system dialer with this number.
     *
     * `ACTION_DIAL` never places a call by itself: the user still presses
     * the call button in whichever app handles it. That is exactly what
     * a chat header should do, and it keeps this screen free of the
     * telephony permission. A group never reaches this function: the
     * button is gone for a group and the guard repeats the fact.
     */
    private fun dial() {
        if (isGroup || address.isBlank()) return
        val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:" + Uri.encode(address)))
        try {
            startActivity(intent)
        } catch (e: Exception) {
            startActivity(Intent(this, dev.smsrelay.calls.DialerActivity::class.java))
        }
    }

    // ----------------------------------------------------------- composer

    /**
     * Wires the composer once.
     *
     * The text watcher is the only thing that enables the button, so the
     * button can never be tappable with an empty field; the click itself
     * is guarded by [sending] as well, because a tap can land on the very
     * frame the flag is set.
     */
    private fun bindComposer() {
        composerInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) = Unit
            override fun afterTextChanged(s: Editable?) {
                updateSendEnabled()
            }
        })
        // Tapping the field opens the keyboard, which shrinks the list.
        // Whatever the user was reading, writing a message means looking
        // at the newest one, so the list is sent to the end the moment the
        // field takes the focus.
        composerInput.setOnFocusChangeListener { _, hasFocus ->
            if (hasFocus) scrollToNewest()
        }
        sendButton.setOnClickListener { sendComposer() }
        updateSendEnabled()
    }

    /** Puts the last bubble at the bottom of the list, if there is one. */
    private fun scrollToNewest() {
        if (adapter.itemCount > 0) list.scrollToPosition(adapter.itemCount - 1)
    }

    /**
     * The send button follows the text and the in-flight state.
     *
     * `isEnabled` is what blocks the tap; the alpha is only the visual
     * answer, so a disabled button is never mistaken for a working one.
     */
    private fun updateSendEnabled() {
        val ready = !sending && canSend() && composerInput.text.toString().isNotBlank()
        sendButton.isEnabled = ready
        // The plane steps aside for the spinner instead of sitting under it.
        sendButton.alpha = when {
            sending -> 0f
            ready -> 1f
            else -> 0.4f
        }
        sendProgress.visibility = if (sending) View.VISIBLE else View.GONE
        // The label follows the state, so a screen reader says what the
        // button is doing instead of repeating "Send" while it waits.
        sendButton.contentDescription = getString(
            if (sending) R.string.composer_sending else R.string.composer_send
        )
    }

    /**
     * True when this conversation can be written to at all.
     *
     * A group, an MMS only thread, a thread without a dialable address and
     * a store that refused to be read all get a sentence instead of a
     * field, so no button is ever offered that would fail on tap.
     */
    private fun canSend(): Boolean =
        !isGroup && !readFailed && !isMmsOnly() && address.isNotBlank()

    /**
     * True when the thread has no SMS this app could answer.
     *
     * The provider lists MMS conversations next to SMS ones and this app
     * reads the SMS table only, so "an empty page with a snippet" is an
     * MMS conversation. Before the first page arrives every thread looks
     * empty, hence [pageLoaded]: without it a normal thread would flash the
     * MMS sentence while it loads.
     *
     * A blank [address] is *not* an MMS thread: that is a conversation
     * whose canonical address could not be resolved, and the bar says so
     * separately instead of blaming MMS.
     */
    private fun isMmsOnly(): Boolean {
        if (!pageLoaded) return false
        return messages.isEmpty() && snippet.isNotBlank()
    }

    /**
     * Draws the bar, or the one sentence that replaces it.
     *
     * The bar and the notice occupy the same place at the bottom of the
     * column, so the list keeps its height when the state changes and a
     * group thread does not look like a broken one.
     */
    private fun renderComposer() {
        // Nothing is offered while the page is still being read: before the
        // first result an MMS only thread and a normal one look identical,
        // and the wrong bar for a moment is worse than a bar that arrives
        // with the messages.
        if (!pageLoaded && !readFailed) {
            composer.visibility = View.GONE
            composerNotice.visibility = View.GONE
            return
        }
        val reason = when {
            readFailed -> getString(R.string.messages_error_read)
            isGroup -> getString(R.string.messages_group_send_unsupported)
            // No resolved number means no target: sending would fail with
            // `invalid_target`, so the field is not offered at all. This is
            // checked before the MMS case, because a one to one thread with
            // an unresolved canonical address is not an MMS conversation.
            address.isBlank() -> getString(R.string.messages_no_address)
            isMmsOnly() -> getString(R.string.messages_mms_send_unsupported)
            else -> null
        }
        if (reason == null) {
            composer.visibility = View.VISIBLE
            composerNotice.visibility = View.GONE
            updateSendEnabled()
        } else {
            composer.visibility = View.GONE
            composerNotice.text = reason
            composerNotice.visibility = View.VISIBLE
            hideKeyboard()
        }
    }

    /**
     * Sends the draft over the SIM, then stores and mirrors it.
     *
     * The radio is asked first: nothing is written to the store before the
     * phone accepts the message, so a rejected number or a missing card
     * never leaves a row behind. The provider copy comes from
     * [SmsRepository.insertSent] and the relay mirror is best effort after
     * it, on a background thread, so a slow or missing server can neither
     * delay the bubble nor lose the message.
     */
    private fun sendComposer() {
        if (sending) return
        // `SettingsStore.sendingEnabled` is deliberately NOT checked here.
        // That switch exists so a remote command (Telegram, the Mini App)
        // cannot make this phone send without its owner present; a person
        // typing in this screen and tapping Send is that consent itself.
        // The permission and the radio are the only gates, and both are
        // answered by SmsSender.
        val body = composerInput.text.toString()
        val target = address.trim()
        if (body.isBlank()) return
        if (!SmsSender.isValidTarget(target)) {
            showSendFailure(SmsSender.REASON_INVALID_TARGET)
            return
        }
        sending = true
        updateSendEnabled()
        val draft = body
        lifecycleScope.launch {
            // Both steps run on the same background hop. The card is read
            // first only so the mirrored row can carry the number it went
            // out with; the send itself is told to pick the card, so the
            // rule stays in one place (`SmsSender.resolveSubscriptionId`).
            val prepared = withContext(Dispatchers.IO) {
                val card = try {
                    SmsSender.resolveSubscriptionId(this@ThreadActivity, settings) ?: 0
                } catch (e: Exception) {
                    0
                }
                // No explicit card: the message leaves with the default /
                // user-chosen SIM, exactly like the fallback does.
                val outcome = SmsSender.send(
                    context = applicationContext,
                    settings = settings,
                    destination = target,
                    body = draft,
                    subscriptionId = null
                )
                card to outcome
            }
            val subId = prepared.first
            when (val outcome = prepared.second) {
                is SmsSender.Outcome.Sent -> onSent(target, draft, subId)
                is SmsSender.Outcome.Failed -> {
                    sending = false
                    updateSendEnabled()
                    // The draft stays exactly as it was typed, and the
                    // field takes the focus back so the retry is one tap.
                    composerInput.setText(draft)
                    composerInput.setSelection(composerInput.text.length)
                    composerInput.requestFocus()
                    showSendFailure(outcome.reason)
                }
            }
        }
    }

    /** Stores the accepted message, mirrors it, and shows the thread again. */
    private suspend fun onSent(target: String, body: String, subId: Int) {
        val sentAt = System.currentTimeMillis()
        val stored = SmsRepository.insertSent(this, target, body, sentAt, subId)
        // The composer is cleared only after the store answered: a send
        // whose row was refused still happened on the radio, and the
        // snackbar says so instead of leaving a bubble that does not exist.
        sending = false
        composerInput.setText("")
        updateSendEnabled()
        composerInput.requestFocus()
        if (stored.rowId == null) {
            Snackbar.make(
                findViewById(R.id.rootThread),
                getString(R.string.composer_sent_not_stored),
                Snackbar.LENGTH_LONG
            ).show()
        }
        // The row the provider just wrote already knows its thread; only a
        // store that refused the copy leaves the id to be looked up. That
        // lookup is read-only and never creates a conversation.
        if (stored.threadId > 0L) {
            threadId = stored.threadId
        } else if (threadId <= 0L) {
            threadId = SmsRepository.threadIdForAddress(this, target)
        }
        scrollToEndNext = true
        load(keepScroll = false)
        // The mirror never gates the screen: it runs after the local row
        // exists and its result is not part of the send outcome.
        if (stored.rowId != null) {
            mirrorSent(stored.rowId, target, body, sentAt, subId)
        }
    }

    /**
     * Copies the sent message to the relay as outbound history.
     *
     * This is deliberately best effort: the message is already on the
     * radio and in the store, so a server that is unreachable, unpaired or
     * slow must not turn a successful send into an error, and must not be
     * waited for. The server stores the row as history (it is not
     * forwarded to Telegram), which is what puts the outbound bubble in
     * the Mini App. A durable outbound queue is a later phase; until then
     * a dropped mirror is simply missing history, never a lost message.
     *
     * Only the category of a failure is logged: never the number, the
     * body or a contact name.
     */
    private suspend fun mirrorSent(
        providerId: Long,
        target: String,
        body: String,
        sentAtMillis: Long,
        subId: Int
    ) {
        val base = settings.baseUri
        val token = settings.token
        if (base.isBlank() || token.isBlank()) return
        val result = withContext(Dispatchers.IO) {
            try {
                val client = RelayClient(base, token) { settings.handleUnauthorized() }
                client.importBatch(
                    listOf(
                        ImportMessage(
                            providerId = providerId,
                            sender = target,
                            body = body,
                            // The worker sends epoch seconds; DATE is millis.
                            receivedAt = sentAtMillis / 1000,
                            subId = subId.coerceIn(0, 99),
                            direction = ImportMessage.DIRECTION_OUT
                        )
                    )
                )
            } catch (e: Exception) {
                null
            }
        }
        // A failed mirror leaves the local message untouched and silent on
        // screen: the user's send succeeded. The category is all that is
        // ever recorded.
        if (result == null) {
            Log.d(TAG, "relay mirror skipped: network")
        } else if (result is ImportResult.Rejected || result is ImportResult.Unauthorized) {
            Log.d(TAG, "relay mirror skipped: refused")
        }
    }

    /** Maps a sender reason to a sentence and keeps the draft. */
    private fun showSendFailure(reason: String) {
        val text = getString(
            R.string.composer_failed,
            getString(
                when (reason) {
                    SmsSender.REASON_PERMISSION -> R.string.reason_permission
                    SmsSender.REASON_INVALID_TARGET -> R.string.reason_invalid_target
                    SmsSender.REASON_EMPTY_BODY -> R.string.reason_empty_body
                    SmsSender.REASON_NO_MANAGER -> R.string.reason_no_sms_service
                    SmsSender.REASON_SIM_UNAVAILABLE -> R.string.reason_sim_unavailable
                    else -> R.string.reason_send_error
                }
            )
        )
        Snackbar.make(findViewById(R.id.rootThread), text, Snackbar.LENGTH_LONG).show()
    }

    /** Drops the keyboard without leaving the field. */
    private fun hideKeyboard() {
        try {
            val manager = getSystemService(InputMethodManager::class.java) ?: return
            manager.hideSoftInputFromWindow(composerInput.windowToken, 0)
        } catch (e: Exception) {
            // No keyboard, or no window token yet: nothing to hide.
        }
    }

    // ----------------------------------------------------------- banners

    /**
     * The one note this screen needs: an empty store because the app is
     * not the default SMS app. It is built in code because it exists only
     * in that state.
     */
    private fun renderBanners() {
        banners.removeAllViews()
        if (RoleState.isDefaultSmsApp(this)) return
        MessagesBanner.addTo(
            column = banners,
            context = this,
            text = getString(R.string.messages_role_banner)
        ) { openDefaultSmsSettings() }
    }

    /**
     * Opens the place where the default-SMS role can be granted.
     *
     * Android 10+ has a one tap role request; the default-apps settings
     * are the fallback. A phone that offers neither keeps the note.
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

    // ------------------------------------------------------------ state

    private fun showLoading() {
        progress.visibility = View.VISIBLE
        state.visibility = View.GONE
    }

    private fun showState(text: String?) {
        if (text == null) {
            state.visibility = View.GONE
        } else {
            state.text = text
            state.visibility = View.VISIBLE
            progress.visibility = View.GONE
        }
        // The bubble list steps aside while a full explanation is on
        // screen, and comes back the moment there is something to show.
        list.visibility = if (text == null) View.VISIBLE else View.GONE
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {

        /** The log tag of this screen; never carries a number or a body. */
        private const val TAG = "ThreadActivity"

        /** The thread to open, from the conversation list. */
        const val EXTRA_THREAD_ID = "thread_id"

        /**
         * The other side of a one to one conversation, as the list spells
         * it. Never set for a group: a group has no single address.
         */
        const val EXTRA_ADDRESS = "address"

        /** The resolved contact name, when the caller already has it. */
        const val EXTRA_NAME = "name"

        /** True when the conversation has more than one recipient. */
        const val EXTRA_IS_GROUP = "is_group"

        /** The label the list already computed for a group. */
        const val EXTRA_LABEL = "label"

        /** How many people are in a group, for the label before the row loads. */
        const val EXTRA_GROUP_COUNT = "group_count"

        /** The provider's one-line snippet, the only view of an MMS thread. */
        const val EXTRA_SNIPPET = "snippet"

        /**
         * Opens an existing conversation from the list.
         *
         * The label is computed here, from the row, so the toolbar has
         * the right title on the first frame instead of after the reload.
         * A group's address is deliberately not passed: it has none.
         */
        fun intentFor(context: Context, thread: SmsThread): Intent =
            intentForThread(
                context = context,
                threadId = thread.threadId,
                isGroup = thread.isGroup,
                label = ThreadLabels.of(context, thread),
                snippet = thread.snippet,
                address = thread.address,
                name = thread.name,
                groupCount = thread.recipientCount
            )

        /** Opens an existing thread by id, with what the caller knows of it. */
        fun intentForThread(
            context: Context,
            threadId: Long,
            isGroup: Boolean,
            label: String?,
            snippet: String = "",
            address: String = "",
            name: String? = null,
            groupCount: Int = 0
        ): Intent {
            val intent = Intent(context, ThreadActivity::class.java)
                .putExtra(EXTRA_THREAD_ID, threadId)
                .putExtra(EXTRA_IS_GROUP, isGroup)
                .putExtra(EXTRA_SNIPPET, snippet)
                .putExtra(EXTRA_GROUP_COUNT, groupCount)
            if (label != null) intent.putExtra(EXTRA_LABEL, label)
            if (name != null) intent.putExtra(EXTRA_NAME, name)
            // A group is never given an address to dial.
            if (!isGroup && address.isNotBlank()) intent.putExtra(EXTRA_ADDRESS, address)
            return intent
        }

        /** Opens a number that may not have a thread yet. */
        fun intentForAddress(context: Context, address: String, name: String? = null): Intent =
            Intent(context, ThreadActivity::class.java)
                .putExtra(EXTRA_ADDRESS, address)
                .putExtra(EXTRA_NAME, name)
    }
}
