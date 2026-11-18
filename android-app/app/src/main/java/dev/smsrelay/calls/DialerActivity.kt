package dev.smsrelay.calls

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.os.BundleCompat
import dev.smsrelay.R
import dev.smsrelay.util.PhoneNumbers
import dev.smsrelay.util.RoleState

/**
 * The dialpad, and the handler of `tel:` links.
 *
 * This wave ships a dialer that WORKS OFFLINE: it needs no pairing, no
 * server and no network. That is a hard rule of the calls design, because
 * an emergency call must never depend on the relay being reachable.
 *
 * What it deliberately is not: an `InCallService`. The app does not hold
 * `ROLE_DIALER` yet, so the platform dialer still owns the in-call UI;
 * this screen only starts a call and gets out of the way.
 *
 * Emergency numbers are passed straight to the platform, with no SIM
 * choice and no backend call, so the phone's own emergency rules apply
 * unchanged.
 */
class DialerActivity : AppCompatActivity() {

    private lateinit var textNumber: TextView
    private lateinit var textHint: TextView
    private lateinit var layoutCallVia: LinearLayout
    private lateinit var textCallViaEmpty: TextView
    private lateinit var textSimState: TextView
    private lateinit var spinnerSim: Spinner

    /** The digits on the screen; never logged. */
    private var entered = StringBuilder()

    /**
     * The SIM the next call goes out with. Null means: let the platform
     * pick, which is the user's default card.
     */
    private var account: PhoneAccountHandle? = null

    /** The call-capable cards of this phone, in the order they are shown. */
    private var accounts: List<PhoneAccountHandle> = emptyList()

    /**
     * The "call via" entries of THIS phone, resolved once when the screen
     * is created. The package manager is not asked again on every
     * keystroke: a list that changes while the user types is worse than a
     * list that is one app stale.
     */
    private var callViaEntries: List<CallDeepLinks.Entry> = emptyList()

    /** The "Setup" button and the role note, added in code. */
    private var buttonSetup: Button? = null
    private var textRoleNote: TextView? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_dialer)

        textNumber = findViewById(R.id.textDialerNumber)
        textHint = findViewById(R.id.textDialerHint)
        layoutCallVia = findViewById(R.id.layoutCallVia)
        textCallViaEmpty = findViewById(R.id.textCallViaEmpty)
        textSimState = findViewById(R.id.textDialerSimState)
        spinnerSim = findViewById(R.id.spinnerDialerSim)

        // A tel: link is the number that was tapped in another app.
        readIntentNumber(intent)
        savedInstanceState?.getString(STATE_ENTERED)?.let {
            entered = StringBuilder(it)
        }
        // The chosen card survives a rotation: losing it mid-dial would
        // silently send the call out with the other SIM.
        account = savedInstanceState?.let {
            BundleCompat.getParcelable(it, STATE_ACCOUNT, PhoneAccountHandle::class.java)
        }

        bindKeypad()
        loadAccounts()
        callViaEntries = CallDeepLinks.available(this)
        installSetupControls()
        render()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        // A second tel: link while the dialer is open replaces the field
        // instead of being ignored.
        setIntent(intent)
        entered = StringBuilder()
        readIntentNumber(intent)
        render()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString(STATE_ENTERED, entered.toString())
        account?.let { outState.putParcelable(STATE_ACCOUNT, it) }
    }

    override fun onResume() {
        super.onResume()
        // The role can be granted in the setup screen while this screen
        // sits behind it: the note is re-read, never assumed.
        renderRoleNote()
    }

    /**
     * The "Setup" button and the role note.
     *
     * The dialer is the launcher Activity of the app, so the setup screen
     * has to be reachable from here. Both views are added in code, below
     * the keypad and above the footer, which leaves the existing keypad,
     * the SIM chooser and the "call via" rows untouched.
     */
    private fun installSetupControls() {
        val parent = findViewById<View>(R.id.textDialerFooter)?.parent as? LinearLayout ?: return
        if (buttonSetup != null) return
        val index = parent.indexOfChild(findViewById(R.id.textDialerFooter)).coerceAtLeast(0)

        val setup = Button(this).apply {
            id = View.generateViewId()
            text = getString(R.string.dialer_setup)
            isAllCaps = false
            setOnClickListener {
                startActivity(
                    Intent(this@DialerActivity, dev.smsrelay.ui.MainActivity::class.java)
                )
            }
        }
        val note = TextView(this).apply {
            id = View.generateViewId()
            setPadding(0, dp(8), 0, 0)
        }
        parent.addView(setup, index)
        parent.addView(note, index + 1)
        buttonSetup = setup
        textRoleNote = note
        renderRoleNote()
    }

    /** Says in one line whether the app is the phone's dialer yet. */
    private fun renderRoleNote() {
        val note = textRoleNote ?: return
        note.text = getString(
            if (RoleState.isDialerRoleHeld(this)) {
                R.string.dialer_footer
            } else {
                R.string.dialer_not_default_role
            }
        )
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    /**
     * The number of the launching intent, if any.
     *
     * `tel:` puts it in the scheme-specific part; `ACTION_DIAL` with no
     * data (a launcher tap) simply leaves the field empty.
     */
    private fun readIntentNumber(intent: Intent?) {
        val data = intent?.data ?: return
        val raw = data.schemeSpecificPart.orEmpty()
        if (raw.isBlank()) return
        val decoded = runCatching { Uri.decode(raw) }.getOrDefault(raw)
        entered = StringBuilder(
            PhoneNumbers.toAsciiDigits(decoded).filter { it.isDigit() || it == '+' || it == '*' || it == '#' }
        )
    }

    private fun bindKeypad() {
        val keys = mapOf(
            R.id.buttonKey0 to "0",
            R.id.buttonKey1 to "1",
            R.id.buttonKey2 to "2",
            R.id.buttonKey3 to "3",
            R.id.buttonKey4 to "4",
            R.id.buttonKey5 to "5",
            R.id.buttonKey6 to "6",
            R.id.buttonKey7 to "7",
            R.id.buttonKey8 to "8",
            R.id.buttonKey9 to "9",
            R.id.buttonKeyStar to "*",
            R.id.buttonKeyHash to "#",
            R.id.buttonKeyPlus to "+"
        )
        for ((id, value) in keys) {
            findViewById<Button>(id).setOnClickListener { append(value) }
        }
        findViewById<Button>(R.id.buttonKeyBackspace).setOnClickListener { backspace() }
        findViewById<Button>(R.id.buttonKeyBackspace).setOnLongClickListener {
            entered = StringBuilder()
            render()
            true
        }
        findViewById<Button>(R.id.buttonDial).setOnClickListener { placeCall() }
        findViewById<Button>(R.id.buttonDialerClose).setOnClickListener { finish() }
    }

    private fun append(value: String) {
        if (entered.length >= MAX_DIGITS) return
        entered.append(value)
        render()
    }

    private fun backspace() {
        if (entered.isEmpty()) return
        entered.deleteCharAt(entered.length - 1)
        render()
    }

    private fun render() {
        val text = entered.toString()
        textNumber.text = if (text.isEmpty()) {
            getString(R.string.dialer_number_empty)
        } else {
            format(text)
        }
        textHint.text = if (text.isEmpty()) {
            getString(R.string.dialer_hint_empty)
        } else if (isEmergency(text)) {
            getString(R.string.dialer_emergency_note)
        } else {
            getString(R.string.dialer_hint_ready)
        }
        // The "call via" rows carry the number, so they are rebuilt when
        // it changes.
        buildCallViaRows()
        renderSimState(text)
    }

    /**
     * The SIM line, and whether the chooser is offered.
     *
     * The chooser is hidden while an emergency number is on the screen:
     * an emergency call never goes out on a chosen card, so offering the
     * choice would promise something the dialer does not do.
     */
    private fun renderSimState(text: String) {
        val emergency = isEmergency(text)
        textSimState.text = when {
            accounts.isEmpty() -> getString(R.string.dialer_sim_none)
            emergency -> getString(R.string.dialer_sim_emergency)
            accounts.size == 1 -> getString(R.string.dialer_sim_single)
            else -> getString(R.string.dialer_sim_multiple)
        }
        spinnerSim.visibility =
            if (accounts.size > 1 && !emergency) View.VISIBLE else View.GONE
    }

    /**
     * Dialers show a number in readable groups. Only the display changes;
     * [entered] keeps exactly what the user typed, and that is what is
     * dialled.
     */
    private fun format(text: String): String {
        if (text.startsWith("*") || text.startsWith("#")) return text
        val builder = StringBuilder()
        var index = 0
        for (character in text) {
            if (index == 3 || (index > 3 && (index - 3) % 3 == 0 && text.length > 4)) {
                builder.append(' ')
            }
            builder.append(character)
            index += 1
        }
        return builder.toString()
    }

    /**
     * Which cards the phone offers for an outgoing call, and the chooser.
     *
     * The first card is pre-selected, the same rule the SMS sender uses:
     * an empty selection would let the platform ask on every call, which
     * is a dialog the user did not ask for. A phone with more than one
     * card gets a spinner, so the second card is reachable without
     * changing the system default; the choice lives for the session (it
     * survives a rotation, see [onSaveInstanceState]).
     *
     * The emergency path never looks at [account]: see [placeCall].
     */
    private fun loadAccounts() {
        val telecom = getSystemService(TelecomManager::class.java)
        accounts = runCatching {
            telecom?.callCapablePhoneAccounts.orEmpty()
        }.getOrDefault(emptyList())
        // A restored choice wins over the default card, as long as the
        // card is still in the phone; a card that was removed or disabled
        // falls back to the first one instead of naming a handle the
        // telecom stack no longer knows.
        val restored = account
        if (restored == null || accounts.none { it == restored }) {
            account = accounts.firstOrNull()
        }
        setupSimSelector()
    }

    /**
     * Fills the spinner with the cards of this phone.
     *
     * The listener is quiet while the list is rebuilt, so rebuilding it
     * never writes a card the user did not pick - the same rule
     * `MainActivity.setupSimSelector` follows. Visibility is decided by
     * [renderSimState], which also knows whether the field holds an
     * emergency number.
     */
    private fun setupSimSelector() {
        if (accounts.size < 2) return
        spinnerSim.setOnItemSelectedListener(null)
        spinnerSim.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            accounts.map { labelOf(it) }
        )
        val index = accounts.indexOf(account)
        spinnerSim.setSelection(if (index >= 0) index else 0)
        spinnerSim.setOnItemSelectedListener(object : AdapterView.OnItemSelectedListener {
            override fun onItemSelected(
                parent: AdapterView<*>,
                view: View?,
                position: Int,
                id: Long
            ) {
                account = accounts.getOrNull(position) ?: account
            }

            override fun onNothingSelected(parent: AdapterView<*>) = Unit
        })
    }

    /**
     * What the user calls a card: the label the system shows, then the
     * account id (already a readable "SIM 1"/"SIM 2" on most phones), and
     * finally the position, so a row is never blank.
     */
    private fun labelOf(handle: PhoneAccountHandle): String {
        val telecom = getSystemService(TelecomManager::class.java)
        val label = runCatching { telecom?.getPhoneAccount(handle)?.label }
            .getOrNull()
            ?.toString()
            ?.trim()
            .orEmpty()
        if (label.isNotEmpty()) return label
        val id = handle.id?.trim().orEmpty()
        if (id.isNotEmpty()) return id
        return getString(R.string.sim_label, accounts.indexOf(handle) + 1)
    }

    /**
     * Places the call.
     *
     * Rules, in order:
     *  1. an empty field does nothing;
     *  2. an emergency number goes to the platform with NO SIM choice,
     *     so the phone's own emergency handling is never bypassed;
     *  3. `CALL_PHONE` missing means the number is handed to the system
     *     dialer with `ACTION_DIAL`, which is still a working call path;
     *  4. otherwise the call is placed with the chosen card.
     */
    private fun placeCall() {
        val text = entered.toString()
        if (text.isEmpty()) {
            Toast.makeText(this, R.string.dialer_nothing_to_dial, Toast.LENGTH_SHORT).show()
            return
        }
        val uri = runCatching { Uri.fromParts("tel", text, null) }.getOrNull()
        if (uri == null) {
            Toast.makeText(this, R.string.dialer_number_invalid, Toast.LENGTH_SHORT).show()
            return
        }

        if (isEmergency(text)) {
            // No dialer selection, no backend, no extra handling: this is
            // exactly the call path the platform expects for 112.
            if (!callWithTelecom(uri, useCard = false)) {
                openSystemDialer(uri)
            }
            return
        }

        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CALL_PHONE) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            // The relay dialer works before the permission is granted:
            // the platform dialer finishes the call.
            if (!openSystemDialer(uri)) {
                Toast.makeText(this, R.string.dialer_permission_needed, Toast.LENGTH_LONG).show()
            }
            return
        }

        if (!callWithTelecom(uri, useCard = true)) {
            // A vendor telecom stack can refuse the handle; one retry
            // without it is better than a dead button.
            if (!callWithTelecom(uri, useCard = false)) {
                if (!openSystemDialer(uri)) {
                    Toast.makeText(this, R.string.dialer_call_failed, Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    /**
     * Hands the call to the platform.
     *
     * [useCard] is false for an emergency call: no `EXTRA_PHONE_ACCOUNT_HANDLE`
     * is put into the bundle at all, so the platform applies its own
     * emergency routing instead of the user's SIM choice.
     */
    private fun callWithTelecom(uri: Uri, useCard: Boolean): Boolean {
        val telecom = getSystemService(TelecomManager::class.java) ?: return false
        val extras = Bundle()
        val handle = if (useCard) account else null
        // The handle names the SIM. Without one the platform asks, or
        // uses the default card; both are correct answers.
        if (handle != null) {
            extras.putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, handle)
        }
        return runCatching { telecom.placeCall(uri, extras) }.isSuccess
    }

    /**
     * The last resort: hand the number to the platform dialer.
     *
     * This app also answers `ACTION_DIAL`, so the target package is named
     * explicitly. When the app itself IS the default dialer (a later
     * wave), there is no other dialer to hand the call to: the caller
     * shows a message instead of looping back here.
     */
    private fun openSystemDialer(uri: Uri): Boolean {
        val telecom = getSystemService(TelecomManager::class.java)
        val systemDialer = runCatching { telecom?.defaultDialerPackage }.getOrNull()
        if (systemDialer == packageName) return false
        val base = Intent(Intent.ACTION_DIAL, uri).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (!systemDialer.isNullOrBlank()) {
            base.setPackage(systemDialer)
            return runCatching { startActivity(base) }.isSuccess
        }
        // No default dialer is known (a fresh or unusual build): the
        // chooser is the only safe way left.
        return runCatching {
            startActivity(
                Intent.createChooser(base, getString(R.string.dialer_choose_app))
            )
        }.isSuccess
    }

    /**
     * The "call via" rows.
     *
     * Only entries that resolve on this phone are built, so a user who
     * has none of the messengers simply sees the hint instead of a grid
     * of dead buttons. https entries always make the list non-empty.
     */
    private fun buildCallViaRows() {
        val number = entered.toString()
        layoutCallVia.removeAllViews()
        if (callViaEntries.isEmpty()) {
            textCallViaEmpty.visibility = View.VISIBLE
            return
        }
        textCallViaEmpty.visibility = View.GONE
        for (entry in callViaEntries) {
            val button = Button(this)
            val label = entry.label(this)
            val note = entry.platformNote(this)
            button.text = if (note == null) {
                label
            } else {
                getString(R.string.dialer_entry_with_note, label, note)
            }
            button.isAllCaps = false
            button.setOnClickListener { openDeepLink(entry, number) }
            layoutCallVia.addView(button)
        }
    }

    private fun openDeepLink(entry: CallDeepLinks.Entry, number: String) {
        if (number.isBlank()) {
            Toast.makeText(this, R.string.dialer_nothing_to_dial, Toast.LENGTH_SHORT).show()
            return
        }
        if (!CallDeepLinks.open(this, entry, number)) {
            Toast.makeText(this, R.string.dialer_app_missing, Toast.LENGTH_SHORT).show()
        }
    }

    /** Emergency numbers are never handed to another app or the relay. */
    private fun isEmergency(text: String): Boolean = EmergencyNumbers.matches(text)

    companion object {
        /** Long enough for any real number, short enough to stay sane. */
        private const val MAX_DIGITS = 20

        private const val STATE_ENTERED = "entered"
        private const val STATE_ACCOUNT = "sim_account"

        /**
         * The emergency numbers of the regions this relay serves.
         *
         * One list for the whole app: [EmergencyNumbers] owns the check,
         * so the dialpad, the remote-action executor and the idle dial
         * poll can never disagree about a number.
         */
        val EMERGENCY_NUMBERS: Set<String> get() = EmergencyNumbers.NUMBERS
    }
}
