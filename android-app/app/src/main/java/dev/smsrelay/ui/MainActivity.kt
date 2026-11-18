package dev.smsrelay.ui

import android.Manifest
import android.app.Activity
import android.app.role.RoleManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.provider.Telephony
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.annotation.RequiresApi
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.constraintlayout.widget.ConstraintLayout
import androidx.constraintlayout.widget.ConstraintSet
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.work.WorkInfo
import androidx.work.WorkManager
import dev.smsrelay.R
import dev.smsrelay.SettingsStore
import dev.smsrelay.contacts.ContactsSync
import dev.smsrelay.contacts.ContactResolver
import dev.smsrelay.calls.CallLogDatabase
import dev.smsrelay.calls.CallLogWorker
import dev.smsrelay.calls.DialerActivity
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.net.RelayClient
import dev.smsrelay.pairing.PairingController
import dev.smsrelay.pairing.QrCode
import dev.smsrelay.sms.SimCards
import dev.smsrelay.sms.SmsSender
import dev.smsrelay.util.Diagnostics
import dev.smsrelay.util.RelativeTime
import dev.smsrelay.util.RoleState
import dev.smsrelay.work.ImportWorker
import dev.smsrelay.work.Schedule
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * The one screen of the app.
 *
 * It is written for the person who uses the phone: the first thing that
 * is shown is whether the app works, then the four things the user can
 * do. Everything technical lives in the last section.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var settings: SettingsStore

    private lateinit var inputBaseUri: EditText
    private lateinit var inputDeviceName: EditText
    private lateinit var inputPhoneB: EditText
    private lateinit var checkForward: CheckBox
    private lateinit var checkFallback: CheckBox
    private lateinit var checkSending: CheckBox
    private lateinit var spinnerSim: Spinner
    private lateinit var textConnection: TextView
    private lateinit var textQueue: TextView
    private lateinit var textPermissions: TextView
    private lateinit var textCapturePath: TextView
    private lateinit var textCapturePathTechnical: TextView
    private lateinit var textStatusHeadline: TextView
    private lateinit var textStatusSummary: TextView
    private lateinit var textContactsState: TextView
    private lateinit var buttonTest: Button
    private lateinit var buttonHistory: Button
    private lateinit var buttonAppSettings: Button
    private lateinit var buttonBattery: Button
    private lateinit var buttonAutostart: Button
    private lateinit var buttonDefaultSmsApp: Button
    private lateinit var buttonNotificationAccess: Button
    private lateinit var buttonSyncContacts: Button
    private lateinit var buttonImportSms: Button
    private lateinit var textImportState: TextView
    private lateinit var inputComposeNumber: EditText
    private lateinit var inputComposeBody: EditText
    private lateinit var buttonSendSms: Button
    private lateinit var textComposeStatus: TextView
    private lateinit var textCommandStatus: TextView

    // The call log card.
    private lateinit var textCallPermissions: TextView
    private lateinit var buttonCallPermissions: Button
    private lateinit var buttonSyncCalls: Button
    private lateinit var buttonOpenDialer: Button
    private lateinit var textCallSyncState: TextView

    /**
     * The dialer role controls of the Calls card.
     *
     * They are added in code instead of the layout file: the calls card
     * already exists and the role is a second question on the same card.
     * [installDialerRoleControls] inserts them under the "Open dialpad"
     * button and moves the sync line down, so nothing overlaps.
     */
    private var buttonDialerRole: Button? = null
    private var textDialerRoleState: TextView? = null

    /**
     * True while the role dialog has to be opened as soon as the
     * permission dialog is answered. It is not a gate: the answer is
     * ignored, and the role is requested either way.
     */
    private var roleRequestPending = false

    // The pairing of this phone with the server.
    private lateinit var buttonPair: Button
    private lateinit var buttonNewCode: Button
    private lateinit var textPairState: TextView
    private lateinit var textScanCode: TextView
    private lateinit var textPairCountdown: TextView
    private lateinit var imageQr: ImageView
    private lateinit var pairing: PairingController

    /** When the running pairing runs out (epoch seconds). */
    private var pairingExpiresAt = 0L

    /** The dialog that asks the user to confirm the pairing. */
    private var pairConfirmDialog: AlertDialog? = null

    private val mainHandler = Handler(Looper.getMainLooper())

    private val runtimePermissions = arrayOf(
        Manifest.permission.RECEIVE_SMS,
        Manifest.permission.READ_SMS,
        Manifest.permission.SEND_SMS,
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_CONTACTS
    )

    /**
     * The permissions of the call card. They are asked for separately:
     * the SMS group is hard restricted until the app is the default SMS
     * app, while the call log has a group of its own and can be granted
     * without any role.
     */
    private val callPermissions = arrayOf(
        Manifest.permission.READ_CALL_LOG,
        Manifest.permission.WRITE_CALL_LOG,
        Manifest.permission.CALL_PHONE,
        Manifest.permission.ANSWER_PHONE_CALLS
    )

    /**
     * The system dialog that asks for the default SMS app.
     *
     * The answer of the user decides what happens next: only a granted
     * role makes the SMS permissions possible. The launcher is a field
     * because the activity result API only accepts a registration before
     * the activity is started.
     */
    private val defaultSmsAppLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        handleDefaultSmsAppResult(result.resultCode)
    }

    /**
     * True while the user is away in the settings screen that was opened
     * as a fallback. The permissions are asked for when the user comes
     * back and the app is the default SMS app by then.
     */
    private var waitingForDefaultSmsChoice = false

    /**
     * The runtime dialog for the call permissions.
     *
     * A denial is a normal answer, not an error: the card is re-read and
     * says what is still missing. The app never crashes and never nags.
     */
    private val callPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { grants ->
        updateCallCard()
        // Only the call permissions count here: the notification
        // permission is not a reason to sync, and a dialog that granted
        // nothing but notifications must not look like a success.
        val grantedCall = grants.count { (permission, granted) ->
            granted && permission in callPermissions
        }
        if (grantedCall > 0) {
            // Something was allowed: a sync may be able to run now.
            if (hasCallLogPermission()) startCallSync()
        } else if (!hasAllCallPermissions()) {
            // Nothing was allowed and the card is still incomplete: say
            // so. A dialog that only granted notifications is not an
            // error and must not be reported as one.
            Toast.makeText(
                this,
                R.string.status_calls_no_permission,
                Toast.LENGTH_LONG
            ).show()
        }
        // The role was asked for while a call permission was missing and
        // Android refused it for that reason: now that something was
        // allowed, ask once more, so the user does not have to find the
        // button again. A granted role ends the wish.
        if (roleRequestPending) {
            if (RoleState.isDialerRoleHeld(this)) {
                roleRequestPending = false
            } else if (hasAllCallPermissions()) {
                roleRequestPending = false
                requestDialerRole()
            }
        }
    }

    /**
     * The system dialog that asks for the dialer role.
     *
     * The result code is ignored on purpose: a vendor build can answer
     * RESULT_OK and change nothing, or answer RESULT_CANCELED after the
     * user actually agreed. Only [RoleState.isDialerRoleHeld] decides,
     * and it is read in [onResume] as well.
     */
    private val dialerRoleLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { result ->
        if (RoleState.isDialerRoleHeld(this)) {
            Toast.makeText(this, R.string.action_dialer_role_granted, Toast.LENGTH_SHORT).show()
            roleRequestPending = false
        } else if (result.resultCode == Activity.RESULT_OK) {
            Toast.makeText(this, R.string.action_dialer_role_not_applied, Toast.LENGTH_LONG).show()
        } else {
            Toast.makeText(this, R.string.action_dialer_role_refused, Toast.LENGTH_LONG).show()
        }
        updateCallCard()
        // The permissions are a second, separate question, and only now:
        // a dialog opened on top of the role dialog would be dropped by
        // some builds. Android may have refused the role for exactly
        // these permissions, so the role is asked for once more after
        // they are granted, without the user having to find the button.
        if (!RoleState.isDialerRoleHeld(this) && !hasAllCallPermissions()) {
            roleRequestPending = true
            requestCallPermissions()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = SettingsStore(this)

        inputBaseUri = findViewById(R.id.inputBaseUri)
        inputDeviceName = findViewById(R.id.inputDeviceName)
        inputPhoneB = findViewById(R.id.inputPhoneB)
        checkForward = findViewById(R.id.checkForwardEnabled)
        checkFallback = findViewById(R.id.checkFallbackEnabled)
        checkSending = findViewById(R.id.checkSendingEnabled)
        spinnerSim = findViewById(R.id.spinnerSim)
        textConnection = findViewById(R.id.textConnectionState)
        textQueue = findViewById(R.id.textQueueState)
        textPermissions = findViewById(R.id.textPermissions)
        textCapturePath = findViewById(R.id.textCapturePath)
        textCapturePathTechnical = findViewById(R.id.textCapturePathTechnical)
        textStatusHeadline = findViewById(R.id.textStatusHeadline)
        textStatusSummary = findViewById(R.id.textStatusSummary)
        textContactsState = findViewById(R.id.textContactsState)
        buttonTest = findViewById(R.id.buttonTestConnection)
        buttonHistory = findViewById(R.id.buttonHistory)
        buttonAppSettings = findViewById(R.id.buttonAppSettings)
        buttonBattery = findViewById(R.id.buttonBatterySettings)
        buttonAutostart = findViewById(R.id.buttonAutostart)
        buttonDefaultSmsApp = findViewById(R.id.buttonDefaultSmsApp)
        buttonNotificationAccess = findViewById(R.id.buttonNotificationAccess)
        buttonSyncContacts = findViewById(R.id.buttonSyncContacts)
        buttonImportSms = findViewById(R.id.buttonImportSms)
        textImportState = findViewById(R.id.textImportState)
        inputComposeNumber = findViewById(R.id.inputComposeNumber)
        inputComposeBody = findViewById(R.id.inputComposeBody)
        buttonSendSms = findViewById(R.id.buttonSendSms)
        textComposeStatus = findViewById(R.id.textComposeStatus)
        textCommandStatus = findViewById(R.id.textCommandStatus)
        textCallPermissions = findViewById(R.id.textCallPermissions)
        buttonCallPermissions = findViewById(R.id.buttonCallPermissions)
        buttonSyncCalls = findViewById(R.id.buttonSyncCalls)
        buttonOpenDialer = findViewById(R.id.buttonOpenDialer)
        textCallSyncState = findViewById(R.id.textCallSyncState)
        buttonPair = findViewById(R.id.buttonPair)
        buttonNewCode = findViewById(R.id.buttonNewCode)
        textPairState = findViewById(R.id.textPairState)
        textScanCode = findViewById(R.id.textScanCode)
        textPairCountdown = findViewById(R.id.textPairCountdown)
        imageQr = findViewById(R.id.imageQr)

        pairing = PairingController(this, pairingListener)
        // The device can be removed on the server while the app is open:
        // the screen says so at once.
        deviceRemovedCallback = { onDeviceRemoved() }
        SettingsStore.onDeviceRemoved = deviceRemovedCallback

        inputBaseUri.setText(settings.baseUri)
        inputDeviceName.setText(settings.deviceName.ifBlank { defaultDeviceName() })
        // The backup number is pre-filled so the app is ready to use.
        inputPhoneB.setText(settings.phoneB.ifBlank { SettingsStore.DEFAULT_PHONE_B })
        checkForward.isChecked = settings.forwardEnabled
        checkFallback.isChecked = settings.fallbackEnabled
        checkSending.isChecked = settings.sendingEnabled
        updateSendingState()
        updateContactsState()

        requestMissingPermissions()
        setupSimSelector()

        buttonTest.setOnClickListener { testConnection() }
        buttonHistory.setOnClickListener {
            startActivity(Intent(this, HistoryActivity::class.java))
        }
        buttonAppSettings.setOnClickListener { start(Diagnostics.appDetailsIntent(this)) }
        buttonBattery.setOnClickListener {
            val request = Diagnostics.batterySettingsIntent(this)
            if (!start(request)) {
                start(Diagnostics.batteryListIntent())
            }
        }
        buttonAutostart.setOnClickListener {
            Diagnostics.autostartIntent(this)?.let { start(it) }
        }
        buttonDefaultSmsApp.setOnClickListener { requestDefaultSmsAppRole() }
        buttonNotificationAccess.setOnClickListener {
            val intent = Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)
            if (!start(intent)) {
                Toast.makeText(this, R.string.status_failed, Toast.LENGTH_SHORT).show()
            }
        }
        checkSending.setOnClickListener {
            settings.sendingEnabled = checkSending.isChecked
            updateSendingState()
            if (settings.sendingEnabled) {
                // A command may be waiting for the very permission the user
                // just gave: pick it up now instead of at the next tick.
                dev.smsrelay.work.Schedule.enqueueCommands(this)
            }
        }
        buttonSendSms.setOnClickListener { sendComposedSms() }
        buttonSyncContacts.setOnClickListener { syncContactsNow() }
        buttonImportSms.setOnClickListener { confirmImport() }
        buttonCallPermissions.setOnClickListener { requestCallPermissions() }
        buttonSyncCalls.setOnClickListener { startCallSync() }
        buttonOpenDialer.setOnClickListener {
            startActivity(Intent(this, DialerActivity::class.java))
        }
        buttonPair.setOnClickListener { startPairing() }
        buttonNewCode.setOnClickListener { confirmNewPairingCode() }
        installDialerRoleControls()
        observeImport()
        observeCallSync()
        updateCallCard()
        refreshPairingUi()
    }

    override fun onResume() {
        super.onResume()
        // The cards of the phone are only known once the permission for
        // them is granted: the picker is refreshed on every resume.
        setupSimSelector()
        refreshStatus()
        refreshPairingUi()
        updateCallCard()
        // The role dialog can be answered while this screen is away, and
        // a vendor build can change the answer later: the role is re-read
        // here, never remembered from the last tap.
        if (roleRequestPending && RoleState.isDialerRoleHeld(this)) {
            roleRequestPending = false
            Toast.makeText(this, R.string.action_dialer_role_granted, Toast.LENGTH_SHORT).show()
        }
        // A pairing that was running when the app was closed goes on.
        pairing.resume()
        if (waitingForDefaultSmsChoice) {
            // The user was in the default apps settings. If the app is
            // the default SMS app now, the permissions can be asked for.
            waitingForDefaultSmsChoice = false
            if (RoleState.isDefaultSmsApp(this)) {
                requestMissingPermissions()
            }
        }
        // Pick up a command that arrived while the app was closed, now
        // and not in five minutes.
        dev.smsrelay.work.Schedule.enqueueCommands(this)
        // The contact list of the phone goes to the server in the
        // background, at most every six hours.
        syncContactsIfDue()
    }

    override fun onPause() {
        super.onPause()
        save()
    }

    override fun onDestroy() {
        // A dialog that is still open is not an answer.
        //
        // When the screen is really going away, closing it answers "no",
        // so the pairing is cancelled instead of a token being stored
        // that nobody agreed to. After a rotation only the window is
        // gone: the new screen asks again.
        dismissPairConfirmDialog(cancelPairing = !isChangingConfigurations)
        // The waiting for the admin stops with the screen. The pairing
        // itself is not thrown away: it goes on the next time the app is
        // opened.
        if (::pairing.isInitialized) pairing.stop()
        mainHandler.removeCallbacks(countdownTick)
        // Only this screen clears the callback: another screen may have
        // taken it over already.
        if (SettingsStore.onDeviceRemoved === deviceRemovedCallback) {
            SettingsStore.onDeviceRemoved = null
        }
        deviceRemovedCallback = null
        super.onDestroy()
    }

    /** The callback this screen put into the store. */
    private var deviceRemovedCallback: (() -> Unit)? = null

    /**
     * The device was removed on the server: the token is gone and the
     * queue of the phone waits for the next pairing.
     */
    private fun onDeviceRemoved() {
        if (isFinishing || isDestroyed) return
        refreshPairingUi()
        Toast.makeText(this, R.string.pair_state_removed, Toast.LENGTH_LONG).show()
        refreshStatus()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_PERMISSIONS) {
            // The SMS permissions are only granted when the app is the
            // default SMS app, so the screen is read again.
            refreshStatus()
            return
        }
        if (requestCode == REQUEST_READ_CONTACTS) {
            if (ContactsSync.hasPermission(this)) {
                syncContactsNow()
            } else {
                updateContactsState()
            }
        }
    }

    private fun save() {
        // The server URL is written first: a new URL drops the token of
        // the old server, because a token belongs to one server only.
        settings.baseUri = inputBaseUri.text.toString()
        settings.deviceName = inputDeviceName.text.toString()
        settings.phoneB = inputPhoneB.text.toString()
        settings.forwardEnabled = checkForward.isChecked
        settings.fallbackEnabled = checkFallback.isChecked
        settings.sendingEnabled = checkSending.isChecked
        saveSimSelection()
        // Flush the queue right after a configuration change instead of
        // waiting for the next incoming SMS.
        dev.smsrelay.work.Schedule.enqueueUpload(this)
    }

    /**
     * The name this phone shows in the admin app.
     */
    private fun defaultDeviceName(): String {
        val manufacturer = Build.MANUFACTURER?.trim().orEmpty()
        val model = Build.MODEL?.trim().orEmpty()
        return listOf(manufacturer, model)
            .filter { it.isNotEmpty() }
            .joinToString(" ")
            .ifBlank { getString(R.string.app_name) }
    }

    /**
     * Asks the server for a code and shows it. The admin approves the
     * code in the mini app, the phone picks the token up afterwards.
     *
     * If a code is already live, the same pairing is resumed instead of
     * silently creating another one. The Mini App may still be showing
     * the old QR; two live codes would let the admin approve a code the
     * phone is no longer polling.
     */
    private fun startPairing() {
        if (hasActivePairing()) {
            showPairMessage(getString(R.string.pair_state_waiting))
            pairing.resume()
            return
        }
        startNewPairing()
    }

    /** True when a code exists and has not passed its expiry. */
    private fun hasActivePairing(): Boolean {
        if (settings.pairingState != SettingsStore.STATE_PAIRING) return false
        if (settings.pairingId.isBlank()) return false
        val expiresAt = settings.pairingExpiresAt
        return expiresAt <= 0L ||
            expiresAt > System.currentTimeMillis() / 1000L
    }

    /** Replaces the live code, after warning that the old QR dies. */
    private fun confirmNewPairingCode() {
        if (!hasActivePairing()) {
            startNewPairing()
            return
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.pair_replace_title)
            .setMessage(R.string.pair_replace_message)
            .setPositiveButton(R.string.action_new_code) { _, _ ->
                startNewPairing()
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    /** Starts a brand-new pairing; an older code is superseded. */
    private fun startNewPairing() {
        val name = inputDeviceName.text.toString().trim()
        if (name.isBlank()) {
            showPairMessage(getString(R.string.pair_error_name))
            return
        }
        val url = inputBaseUri.text.toString().trim()
        if (url.isBlank()) {
            showPairMessage(getString(R.string.pair_error_url))
            return
        }
        // A confirmation dialog of the old pairing is not an answer for
        // the new one: it is closed without answering, so the old pairing
        // is simply superseded instead of being reported as cancelled.
        dismissPairConfirmDialog(cancelPairing = false)
        // The URL and the name are written before the pairing starts.
        settings.baseUri = url
        settings.deviceName = name
        showPairMessage(getString(R.string.pair_state_starting))
        hidePairingCode()
        pairing.start(name)
    }

    /** What the pairing looks like right now. */
    private fun refreshPairingUi() {
        if (settings.hasToken()) {
            val name = settings.pairedDeviceName.ifBlank { settings.deviceName }
            showPairMessage(getString(R.string.pair_state_paired, name))
        } else if (settings.pairingState == SettingsStore.STATE_REMOVED) {
            showPairMessage(getString(R.string.pair_state_removed))
        } else if (settings.pairingState == SettingsStore.STATE_SERVER_CHANGED) {
            showPairMessage(getString(R.string.pair_state_server_changed))
        } else if (settings.pairingState == SettingsStore.STATE_PAIRING) {
            showPairMessage(getString(R.string.pair_state_waiting))
        } else {
            showPairMessage(getString(R.string.pair_state_unpaired))
        }
        hidePairingCode()
        buttonPair.isEnabled = true
    }

    /** A message of the pairing, in plain words. */
    private fun showPairMessage(message: String) {
        textPairState.text = message
    }

    /**
     * What the confirmation dialog says: the server, this phone and who
     * approved the pairing. Never the token or the claim secret.
     */
    private fun confirmationMessage(
        confirmation: PairingController.Confirmation
    ): String {
        val approver = when {
            confirmation.approverName.isNotBlank() &&
                confirmation.approverUsername.isNotBlank() ->
                getString(
                    R.string.pair_confirm_approver_username,
                    confirmation.approverName,
                    confirmation.approverUsername
                )

            confirmation.approverName.isNotBlank() ->
                getString(R.string.pair_confirm_approver, confirmation.approverName)

            confirmation.approverUsername.isNotBlank() ->
                getString(
                    R.string.pair_confirm_approver,
                    "@" + confirmation.approverUsername
                )

            else -> getString(R.string.pair_confirm_approver_unknown)
        }
        val device = confirmation.deviceName.ifBlank { settings.deviceName }
        return getString(
            R.string.pair_confirm_message,
            confirmation.serverUrl,
            device,
            approver
        )
    }

    /**
     * Closes the confirmation dialog.
     *
     * With [cancelPairing] the dialog answers "no" on the way out, so the
     * pairing is cancelled and no token is stored - the answer is never
     * assumed. Without it the listener is detached first and the pairing
     * stays pending on the server, so the next screen asks again.
     */
    private fun dismissPairConfirmDialog(cancelPairing: Boolean) {
        val dialog = pairConfirmDialog ?: return
        pairConfirmDialog = null
        if (!cancelPairing) dialog.setOnDismissListener(null)
        dialog.dismiss()
    }

    /** The code and the QR code are only shown while the admin waits. */
    private fun hidePairingCode() {
        imageQr.visibility = View.GONE
        textScanCode.visibility = View.GONE
        textPairCountdown.visibility = View.GONE
        buttonNewCode.visibility = View.GONE
        mainHandler.removeCallbacks(countdownTick)
    }

    /** Shows the short code, the QR code and the time that is left. */
    private fun showPairingCode(state: PairingController.State) {
        textScanCode.text = state.scanCode
        textScanCode.visibility = View.VISIBLE
        buttonNewCode.visibility = View.VISIBLE
        val bitmap = QrCode.render(state.qrPayload, qrSizePx())
        if (bitmap == null) {
            // The payload does not fit into a QR code: ask for a new one.
            imageQr.visibility = View.GONE
            showPairMessage(getString(R.string.pair_error_qr))
        } else {
            imageQr.setImageBitmap(bitmap)
            imageQr.visibility = View.VISIBLE
        }
        pairingExpiresAt = state.expiresAt
        mainHandler.removeCallbacks(countdownTick)
        countdownTick.run()
    }

    /** How long the code is still valid. */
    private val countdownTick = object : Runnable {
        override fun run() {
            val remaining = pairingExpiresAt - System.currentTimeMillis() / 1000L
            textPairCountdown.visibility = View.VISIBLE
            textPairCountdown.text = if (remaining > 0) {
                getString(R.string.pair_countdown, remaining)
            } else {
                getString(R.string.pair_expired)
            }
            if (remaining > 0) mainHandler.postDelayed(this, 1000L)
        }
    }

    /** The size of the QR code in pixels. */
    private fun qrSizePx(): Int {
        val density = resources.displayMetrics.density
        return (QR_SIZE_DP * density).toInt().coerceIn(400, 1600)
    }

    /**
     * What the pairing controller has to say.
     */
    private val pairingListener = object : PairingController.Listener {
        override fun onPairingStarted(state: PairingController.State) {
            if (isFinishing || isDestroyed) return
            showPairMessage(getString(R.string.pair_state_waiting))
            buttonPair.isEnabled = false
            showPairingCode(state)
        }

        /**
         * The admin approved the pairing. The user has to confirm it on
         * this phone before the token is handed out and stored.
         *
         * The dialog is not cancelable: back or a tap outside is not an
         * answer, and an unanswered dialog must not pair the phone.
         */
        override fun onPairingConfirmationRequired(
            confirmation: PairingController.Confirmation,
            respond: (Boolean) -> Unit
        ) {
            if (isFinishing || isDestroyed) {
                // No screen, no confirmation: the pairing is cancelled
                // instead of storing a token nobody agreed to.
                respond(false)
                return
            }
            hidePairingCode()
            buttonPair.isEnabled = false
            showPairMessage(getString(R.string.pair_state_waiting))
            var answered = false
            val dialog = AlertDialog.Builder(this@MainActivity)
                .setTitle(R.string.pair_confirm_title)
                .setMessage(confirmationMessage(confirmation))
                .setPositiveButton(R.string.pair_confirm_positive) { _, _ ->
                    answered = true
                    respond(true)
                }
                .setNegativeButton(android.R.string.cancel) { _, _ ->
                    answered = true
                    respond(false)
                }
                .create()
            dialog.setCancelable(false)
            dialog.setCanceledOnTouchOutside(false)
            dialog.setOnDismissListener {
                if (pairConfirmDialog === dialog) pairConfirmDialog = null
                if (!answered) {
                    // Dismissed without an answer: do not pair.
                    answered = true
                    respond(false)
                }
            }
            val shown = runCatching { dialog.show() }.isSuccess
            if (shown) {
                pairConfirmDialog = dialog
            } else if (!answered) {
                // The screen went away between the check and the show:
                // a pairing is cancelled, never confirmed silently.
                answered = true
                respond(false)
            }
        }

        override fun onPaired(deviceName: String) {
            if (isFinishing || isDestroyed) return
            showPairMessage(getString(R.string.pair_state_paired, deviceName))
            hidePairingCode()
            buttonPair.isEnabled = true
            // The phone may talk to the server now: the workers start.
            Schedule.enqueueUpload(this@MainActivity)
            Schedule.restartCommandChain(this@MainActivity)
            Toast.makeText(
                this@MainActivity,
                getString(R.string.pair_state_paired, deviceName),
                Toast.LENGTH_SHORT
            ).show()
            refreshStatus()
        }

        override fun onPairingFailed(reason: PairingController.Reason) {
            if (isFinishing || isDestroyed) return
            hidePairingCode()
            buttonPair.isEnabled = true
            showPairMessage(
                getString(
                    when (reason) {
                        PairingController.Reason.Network -> R.string.pair_error_network
                        PairingController.Reason.Server -> R.string.pair_error_server
                        PairingController.Reason.AlreadyClaimed ->
                            R.string.pair_error_already_claimed

                        PairingController.Reason.Expired -> R.string.pair_error_expired
                        PairingController.Reason.Invalid -> R.string.pair_error_invalid
                        PairingController.Reason.Cancelled -> R.string.pair_error_cancelled
                    }
                )
            )
        }
    }

    /**
     * The app is read-only by default: while sending is off, no SMS leaves
     * the phone - not a composed message, not a command from Telegram and
     * not the cellular fallback.
     */
    private fun updateSendingState() {
        val enabled = settings.sendingEnabled
        buttonSendSms.isEnabled = enabled
        inputComposeNumber.isEnabled = enabled
        inputComposeBody.isEnabled = enabled
        checkFallback.isEnabled = enabled
        // The fields explain themselves while they are disabled.
        inputComposeNumber.hint = getString(
            if (enabled) R.string.hint_compose_number else R.string.hint_sending_disabled
        )
        inputComposeBody.hint = getString(
            if (enabled) R.string.hint_compose_body else R.string.hint_sending_disabled
        )
        textComposeStatus.text = if (enabled) {
            getString(R.string.status_sending_ready)
        } else {
            getString(R.string.hint_sending_disabled)
        }
    }

    /** Sends the composed message with the phone's own SIM. */
    private fun sendComposedSms() {
        if (!settings.sendingEnabled) {
            Toast.makeText(this, R.string.hint_sending_disabled, Toast.LENGTH_SHORT).show()
            return
        }
        // The card the user picked must be known before the radio is asked:
        // the message leaves with that card.
        saveSimSelection()
        val target = inputComposeNumber.text.toString().trim()
        val body = inputComposeBody.text.toString()
        if (target.isEmpty()) {
            Toast.makeText(this, R.string.status_invalid_number, Toast.LENGTH_SHORT).show()
            return
        }
        if (body.isBlank()) {
            Toast.makeText(this, R.string.status_empty_body, Toast.LENGTH_SHORT).show()
            return
        }
        // The radio call must not block the UI. Looking up a contact name
        // reads the contacts database, so that happens here as well.
        Thread {
            val destination = ContactResolver.resolve(this, target)
            if (destination == null || !SmsSender.isValidTarget(destination)) {
                runOnUiThread {
                    val reason = getString(
                        if (target.any { it.isLetter() }) R.string.status_unknown_contact
                        else R.string.status_invalid_number
                    )
                    textComposeStatus.text = reason
                    Toast.makeText(this, reason, Toast.LENGTH_LONG).show()
                }
                return@Thread
            }
            val outcome = SmsSender.send(this, settings, destination, body)
            runOnUiThread {
                when (outcome) {
                    SmsSender.Outcome.Sent -> {
                        textComposeStatus.text = getString(R.string.status_sms_sent)
                        Toast.makeText(this, R.string.status_sms_sent, Toast.LENGTH_SHORT).show()
                    }

                    is SmsSender.Outcome.Failed -> {
                        val reason = getString(R.string.status_sms_failed, sendFailure(outcome.reason))
                        textComposeStatus.text = reason
                        Toast.makeText(this, reason, Toast.LENGTH_LONG).show()
                    }
                }
            }
        }.start()
    }

    /** The short codes of the sender are shown as plain words. */
    private fun sendFailure(reason: String): String = getString(
        when (reason) {
            SmsSender.REASON_NO_MANAGER -> R.string.reason_no_sms_service
            SmsSender.REASON_SIM_UNAVAILABLE -> R.string.reason_sim_unavailable
            SmsSender.REASON_PERMISSION -> R.string.reason_permission
            SmsSender.REASON_INVALID_TARGET -> R.string.reason_invalid_target
            SmsSender.REASON_EMPTY_BODY -> R.string.reason_empty_body
            else -> R.string.reason_send_error
        }
    )

    private fun setupSimSelector() {
        val cards = SimCards.active(this)
        val ids = cards.map { it.subscriptionId }
        if (ids == simIds) return
        // The listener is quiet while the list is rebuilt: a rebuild must
        // never write a card that the user did not pick.
        spinnerSim.setOnItemSelectedListener(null)
        simIds.clear()
        simIds.addAll(ids)
        val labels = cards.map { SimCards.label(this, it) }
        spinnerSim.adapter = ArrayAdapter(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            labels
        )
        // The card the user picked stays picked; a card that is gone falls
        // back to the first one, the same rule the sender uses.
        val current = settings.simSubscriptionId
        val index = if (current == null) -1 else simIds.indexOf(current)
        if (labels.isNotEmpty()) {
            spinnerSim.setSelection(if (index >= 0) index else 0)
        }
        spinnerSim.setOnItemSelectedListener(simListener)
    }

    /** The choice is stored as soon as the user makes it. */
    private val simListener = object : AdapterView.OnItemSelectedListener {
        override fun onItemSelected(
            parent: AdapterView<*>,
            view: View?,
            position: Int,
            id: Long
        ) {
            saveSimSelection()
        }

        override fun onNothingSelected(parent: AdapterView<*>) = Unit
    }

    private val simIds = mutableListOf<Int>()

    /**
     * Writes the chosen card into the settings.
     *
     * This is the ONE place the choice is stored: the composed message,
     * the commands from Telegram and the cellular fallback all read it
     * from there. When the cards of the phone cannot be read (the
     * permission is missing) nothing is written, so a choice that was
     * made earlier is not lost.
     */
    private fun saveSimSelection() {
        if (simIds.isEmpty()) return
        val position = spinnerSim.selectedItemPosition
        if (position < 0 || position >= simIds.size) return
        settings.simSubscriptionId = simIds[position]
    }

    private fun testConnection() {
        save()
        val client = RelayClient(settings.baseUri, settings.token)
        Thread {
            val result = client.probe()
            runOnUiThread {
                result.onSuccess { latency ->
                    settings.lastProbeOk = true
                    textConnection.text = getString(R.string.toast_probe_ok, latency)
                    Toast.makeText(this, getString(R.string.toast_probe_ok, latency), Toast.LENGTH_SHORT).show()
                    updateStatusHeadline(true)
                }.onFailure { error ->
                    settings.lastProbeOk = false
                    val reason = error.message ?: error.javaClass.simpleName
                    textConnection.text = getString(R.string.toast_probe_failed, reason)
                    Toast.makeText(this, getString(R.string.toast_probe_failed, reason), Toast.LENGTH_LONG).show()
                    updateStatusHeadline(false)
                }
            }
        }.start()
    }

    private fun refreshStatus() {
        Thread {
            val dao = AppDatabase.get(this).messageDao()
            val queued = dao.countByState(MessageRecord.STATE_QUEUED)
            val sent = dao.countByState(MessageRecord.STATE_SENT)
            val lastMessage = dao.latest()
            val lastSmsAt = (lastMessage?.receivedAt ?: 0L) * 1000L
            val lastUpload = settings.lastUploadAt
            val stamp = if (lastUpload > 0) {
                SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(Date(lastUpload))
            } else {
                getString(R.string.status_never)
            }
            // Is the relay reachable right now? The answer belongs in
            // the first line of the screen.
            val client = RelayClient(settings.baseUri, settings.token)
            val reachable = client.probe().isSuccess
            val permissionText = plainPermissionText()
            val captureText = plainCaptureText()
            val captureTechnical = getString(R.string.capture_path, RoleState.statusText(this@MainActivity))
            val queueText = getString(R.string.label_queue) + ": " + queued + " / " + sent +
                "\n" + getString(R.string.label_last_upload) + ": " + stamp
            val summary = buildString {
                if (lastSmsAt > 0L) {
                    append(
                        getString(
                            R.string.status_last_sms,
                            RelativeTime.describe(this@MainActivity, lastSmsAt)
                        )
                    )
                } else {
                    append(getString(R.string.status_no_sms_yet))
                }
                append('\n')
                append(getString(R.string.status_waiting_to_send, queued))
            }
            val commandStatus = commandStatusText()
            runOnUiThread {
                settings.lastProbeOk = reachable
                updateStatusHeadline(reachable)
                textStatusSummary.text = summary
                textPermissions.text = permissionText
                textCapturePath.text = captureText
                textCapturePathTechnical.text = captureTechnical
                textQueue.text = queueText
                textCommandStatus.text = commandStatus
            }
        }.start()
    }

    private fun updateStatusHeadline(reachable: Boolean) {
        textStatusHeadline.text = getString(
            if (reachable) R.string.status_working else R.string.status_not_connected
        )
    }

    /**
     * What the app may do, in words a person understands.
     */
    private fun plainPermissionText(): String {
        val canRead = RoleState.canReceiveSmsBroadcast(this) ||
            RoleState.hasNotificationAccess(this)
        val canSend = ContextCompat.checkSelfPermission(this, Manifest.permission.SEND_SMS) ==
            PackageManager.PERMISSION_GRANTED
        val batteryOk = Diagnostics.isIgnoringBatteryOptimizations(this)
        return buildString {
            append(
                getString(
                    if (canRead) R.string.perm_plain_receive_yes else R.string.perm_plain_receive_no
                )
            )
            if (!canRead) {
                append('\n')
                append(getString(R.string.perm_plain_receive_hint))
            }
            append('\n')
            append(
                getString(
                    if (canSend) R.string.perm_plain_send_yes else R.string.perm_plain_send_no
                )
            )
            append('\n')
            append(
                getString(
                    if (batteryOk) R.string.perm_plain_battery_ok
                    else R.string.perm_plain_battery_blocked
                )
            )
        }
    }

    /** How the app gets to see the messages of the phone. */
    private fun plainCaptureText(): String = getString(
        when {
            RoleState.isDefaultSmsApp(this) -> R.string.capture_default_app
            RoleState.hasNotificationAccess(this) -> R.string.capture_notifications
            else -> R.string.capture_none
        }
    )

    /** Short status of the last command that arrived from Telegram. */
    private fun commandStatusText(): String {
        val status = settings.lastCommandStatus
        if (status.isBlank()) {
            return getString(R.string.label_last_command) + ": " +
                getString(R.string.status_command_none)
        }
        val stamp = settings.lastCommandAt
        val whenText = if (stamp > 0) {
            SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US).format(Date(stamp))
        } else {
            ""
        }
        val suffix = if (whenText.isEmpty()) "" else " (" + whenText + ")"
        return getString(R.string.label_last_command) + ": " + status + suffix
    }

    /**
     * Uploads the contact list of the phone, on a background thread.
     */
    private fun syncContactsNow() {
        if (!ContactsSync.hasPermission(this)) {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.READ_CONTACTS),
                REQUEST_READ_CONTACTS
            )
            return
        }
        textContactsState.text = getString(R.string.status_contacts_uploading)
        ContactsSync.sync(this) { outcome ->
            runOnUiThread {
                when (outcome) {
                    ContactsSync.Outcome.Uploaded -> {
                        updateContactsState()
                        Toast.makeText(
                            this,
                            R.string.status_contacts_uploaded,
                            Toast.LENGTH_SHORT
                        ).show()
                    }

                    ContactsSync.Outcome.Failed -> {
                        updateContactsState()
                        Toast.makeText(
                            this,
                            R.string.status_contacts_failed,
                            Toast.LENGTH_LONG
                        ).show()
                    }

                    ContactsSync.Outcome.NoPermission -> updateContactsState()

                    ContactsSync.Outcome.NothingToDo -> updateContactsState()
                }
            }
        }
    }

    /** Uploads the contact list when it is time for it. */
    private fun syncContactsIfDue() {
        if (!ContactsSync.isDue(this)) {
            updateContactsState()
            return
        }
        ContactsSync.syncIfDue(this) { outcome ->
            runOnUiThread {
                if (outcome == ContactsSync.Outcome.Uploaded) {
                    updateContactsState()
                }
            }
        }
    }

    /** What the server knows about the contacts of this phone. */
    private fun updateContactsState() {
        if (!ContactsSync.hasPermission(this)) {
            textContactsState.text = getString(R.string.status_contacts_no_permission)
            return
        }
        val count = settings.contactsCount
        val stamp = settings.lastContactsSyncAt
        textContactsState.text = if (count <= 0 || stamp <= 0L) {
            getString(R.string.status_contacts_none)
        } else {
            getString(
                R.string.status_contacts_synced,
                count,
                RelativeTime.describe(this, stamp)
            )
        }
    }

    /**
     * Copying the whole SMS inbox is opt-in and needs the default-SMS
     * role: reading the inbox is hard restricted without it on modern
     * Android. The dialog spells out that the history becomes visible in
     * the web inbox (and only there).
     */
    private fun confirmImport() {
        if (!RoleState.isDefaultSmsApp(this)) {
            Toast.makeText(
                this,
                R.string.status_import_needs_default_app,
                Toast.LENGTH_LONG
            ).show()
            requestDefaultSmsAppRole()
            return
        }
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.READ_SMS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            Toast.makeText(
                this,
                R.string.status_import_needs_permission,
                Toast.LENGTH_LONG
            ).show()
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.READ_SMS),
                REQUEST_READ_SMS
            )
            return
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.import_confirm_title)
            .setMessage(R.string.import_confirm_message)
            .setPositiveButton(R.string.import_confirm_positive) { _, _ -> startImport() }
            .setNegativeButton(R.string.import_confirm_negative, null)
            .show()
    }

    private fun startImport() {
        buttonImportSms.isEnabled = false
        textImportState.text = getString(R.string.status_import_waiting)
        Schedule.enqueueImport(this)
    }

    /** Progress of the one import job, straight from WorkManager. */
    private fun observeImport() {
        WorkManager.getInstance(this)
            .getWorkInfosForUniqueWorkLiveData(Schedule.UNIQUE_IMPORT)
            .observe(this) { infos -> renderImportState(infos.firstOrNull()) }
    }

    private fun renderImportState(info: WorkInfo?) {
        if (info == null) {
            buttonImportSms.isEnabled = true
            textImportState.text = getString(R.string.status_import_none)
            return
        }
        when (info.state) {
            WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED -> {
                buttonImportSms.isEnabled = false
                textImportState.text = getString(R.string.status_import_waiting)
            }

            WorkInfo.State.RUNNING -> {
                buttonImportSms.isEnabled = false
                val progress = info.progress
                textImportState.text = getString(
                    R.string.status_import_running,
                    progress.getInt(ImportWorker.KEY_IMPORTED, 0),
                    progress.getInt(ImportWorker.KEY_DUPLICATES, 0)
                )
            }

            WorkInfo.State.SUCCEEDED -> {
                buttonImportSms.isEnabled = true
                val result = info.outputData
                textImportState.text = getString(
                    R.string.status_import_done,
                    result.getInt(ImportWorker.KEY_IMPORTED, 0),
                    result.getInt(ImportWorker.KEY_DUPLICATES, 0),
                    result.getInt(ImportWorker.KEY_SKIPPED, 0)
                )
            }

            WorkInfo.State.FAILED -> {
                buttonImportSms.isEnabled = true
                val reason = info.outputData.getString(ImportWorker.KEY_ERROR).orEmpty()
                textImportState.text = getString(
                    R.string.status_import_failed,
                    importError(reason)
                )
            }

            else -> {
                buttonImportSms.isEnabled = true
                textImportState.text = getString(R.string.status_import_none)
            }
        }
    }

    private fun importError(reason: String): String = getString(
        when (reason) {
            ImportWorker.ERROR_NO_ROLE -> R.string.import_error_no_role
            ImportWorker.ERROR_NO_PERMISSION -> R.string.import_error_no_permission
            ImportWorker.ERROR_AUTH -> R.string.import_error_auth
            ImportWorker.ERROR_NOT_PAIRED -> R.string.import_error_not_paired
            ImportWorker.ERROR_REJECTED -> R.string.import_error_rejected
            ImportWorker.ERROR_SERVER -> R.string.import_error_server
            ImportWorker.ERROR_READ -> R.string.import_error_read
            else -> R.string.import_error_unknown
        }
    )

    // ------------------------------------------------------------------
    // Calls
    // ------------------------------------------------------------------

    /** True while the call-sync job owns the status line. */
    private var callSyncRunning = false

    /**
     * Adds the dialer-role button and its state line to the Calls card.
     *
     * The card is a `ConstraintLayout`; the two views are inserted under
     * the "Open dialpad" button and the sync line is pushed below them,
     * so the card grows without overlapping anything. The ids are
     * generated, which is why the fields above hold the references.
     */
    private fun installDialerRoleControls() {
        val parent = buttonOpenDialer.parent as? ConstraintLayout ?: return
        if (buttonDialerRole != null) return

        val button = Button(this).apply {
            id = View.generateViewId()
            text = getString(R.string.action_request_dialer_role)
            isAllCaps = false
            setOnClickListener { requestDialerRole() }
        }
        val state = TextView(this).apply {
            id = View.generateViewId()
            setText(R.string.calls_role_explanation)
        }

        parent.addView(button, ConstraintLayout.LayoutParams(
            ConstraintLayout.LayoutParams.WRAP_CONTENT,
            ConstraintLayout.LayoutParams.WRAP_CONTENT
        ))
        parent.addView(state, ConstraintLayout.LayoutParams(
            ConstraintLayout.LayoutParams.MATCH_CONSTRAINT,
            ConstraintLayout.LayoutParams.WRAP_CONTENT
        ))

        val set = ConstraintSet()
        set.clone(parent)
        set.connect(
            button.id, ConstraintSet.TOP,
            buttonOpenDialer.id, ConstraintSet.BOTTOM, 0
        )
        set.connect(button.id, ConstraintSet.START, ConstraintSet.PARENT_ID, ConstraintSet.START, 0)
        set.connect(
            state.id, ConstraintSet.TOP,
            button.id, ConstraintSet.BOTTOM, 0
        )
        set.connect(state.id, ConstraintSet.START, ConstraintSet.PARENT_ID, ConstraintSet.START, 0)
        set.connect(state.id, ConstraintSet.END, ConstraintSet.PARENT_ID, ConstraintSet.END, 0)
        // The sync line used to sit under the dialpad button; it moves
        // under the role state so both lines stay readable.
        set.connect(
            textCallSyncState.id, ConstraintSet.TOP,
            state.id, ConstraintSet.BOTTOM, 0
        )
        set.applyTo(parent)

        buttonDialerRole = button
        textDialerRoleState = state
    }

    /**
     * The dialer-role question of the Calls card.
     *
     * Three answers are possible, and each one says what to do:
     *  - the app holds the role: nothing to ask, and the card says so;
     *  - the phone offers the role: a button asks for it;
     *  - the phone does not offer it (no telephony, a vendor build that
     *    removed it): the button opens the default-apps settings and the
     *    line explains that the phone has no role.
     */
    private fun updateDialerRoleCard() {
        val button = buttonDialerRole ?: return
        val state = textDialerRoleState ?: return
        val held = RoleState.isDialerRoleHeld(this)
        val available = held || RoleState.isDialerRoleAvailable(this)
        button.isEnabled = !held
        button.text = getString(
            if (held) R.string.calls_role_held else R.string.action_request_dialer_role
        )
        state.text = getString(
            when {
                held -> R.string.calls_role_held_hint
                available -> R.string.calls_role_explanation
                else -> R.string.calls_role_unavailable
            }
        )
    }

    /**
     * Ask for the dialer role.
     *
     * The role request is the first thing that happens: gating it behind
     * the call permissions was a dead end, because a single "no" (for
     * example to ANSWER_PHONE_CALLS) hid the role dialog for good and the
     * app could never become the dialer. Android does check the
     * permissions itself before it grants the role, so the dialog
     * decides; when it fails for lack of a permission the card is still
     * there, and the permission button asks for the missing ones.
     *
     * The permissions are asked for afterwards, separately, so the user
     * sees both questions instead of only the one that blocked the other.
     */
    private fun requestDialerRole() {
        if (RoleState.isDialerRoleHeld(this)) {
            Toast.makeText(this, R.string.calls_role_held, Toast.LENGTH_SHORT).show()
            updateCallCard()
            return
        }
        val intent = RoleState.dialerRoleIntent(this)
        if (intent == null) {
            // No role on this phone: the default-apps settings are the
            // only place the choice can be made.
            if (!openDefaultAppsSettings()) {
                Toast.makeText(this, R.string.calls_role_unavailable, Toast.LENGTH_LONG).show()
            }
            askCallPermissionsAfterRole()
            return
        }
        val launched = runCatching { dialerRoleLauncher.launch(intent) }.isSuccess
        if (!launched) {
            if (!openDefaultAppsSettings()) {
                Toast.makeText(this, R.string.calls_role_unavailable, Toast.LENGTH_LONG).show()
            }
            // The dialog never opened, so nothing will ask later.
            askCallPermissionsAfterRole()
        }
    }

    /**
     * The missing call permissions, asked after the role, never before.
     *
     * A dialog opened on top of the role screen is dropped by some
     * builds, so the role answer comes first: [dialerRoleLauncher] calls
     * this when it returns, and [requestDialerRole] calls it only when
     * the role dialog could not be shown at all.
     */
    private fun askCallPermissionsAfterRole() {
        if (hasAllCallPermissions()) return
        roleRequestPending = true
        requestCallPermissions()
    }

    /**
     * The default-apps screen, where the dialer can be chosen by hand.
     *
     * The first screen the phone can open wins; the app details page is
     * always there as the last resort.
     */
    private fun openDefaultAppsSettings(): Boolean {
        if (tryStart(Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS))) {
            Toast.makeText(this, R.string.action_dialer_role_settings, Toast.LENGTH_LONG).show()
            return true
        }
        if (tryStart(Diagnostics.appDetailsIntent(this))) {
            Toast.makeText(this, R.string.calls_role_unavailable, Toast.LENGTH_LONG).show()
            return true
        }
        return false
    }

    /**
     * The call card: what is allowed, how the queue looks and whether the
     * dialpad is reachable.
     *
     * Only cheap reads happen here; the row counts come from Room, which
     * refuses a query on the main thread, so [refreshCallCount] does them
     * on a short-lived thread - the same pattern [refreshStatus] uses for
     * the SMS queue.
     */
    private fun updateCallCard() {
        textCallPermissions.text = callPermissionText()
        // The notification permission is asked for by the same button, so
        // a phone whose call permissions are complete can still have a
        // reason to tap it.
        buttonCallPermissions.isEnabled =
            !hasAllCallPermissions() || notificationPermissionMissing()
        // Wave 2: the app can hold the role, so the card offers it and
        // reports it in plain words instead of a content description.
        buttonOpenDialer.contentDescription = getString(
            if (RoleState.isDialerRoleHeld(this)) {
                R.string.calls_role_held
            } else {
                R.string.calls_role_not_yet
            }
        )
        updateDialerRoleCard()
        if (!hasCallLogPermission()) {
            textCallSyncState.text = getString(R.string.status_calls_no_permission)
        } else if (!callSyncRunning) {
            refreshCallCount()
        }
    }

    /** Reads the queue counts and writes the idle state line. */
    private fun refreshCallCount() {
        val lastSync = settings.lastCallSyncAt
        Thread {
            val dao = runCatching {
                CallLogDatabase.get(this).callLogDao()
            }.getOrNull()
            val stored = runCatching { dao?.countStored() ?: 0 }.getOrDefault(0)
            val queued = runCatching { dao?.countQueued() ?: 0 }.getOrDefault(0)
            runOnUiThread {
                if (isFinishing || isDestroyed) return@runOnUiThread
                // A running job owns the line until it reports back.
                if (callSyncRunning) return@runOnUiThread
                textCallSyncState.text = idleCallSyncText(stored, queued, lastSync)
            }
        }.start()
    }

    /** What the card says while no job is running. */
    private fun idleCallSyncText(stored: Int, queued: Int, lastSync: Long): String =
        buildString {
            if (stored <= 0 && lastSync <= 0L) {
                append(getString(R.string.status_calls_none))
            } else {
                val whenText = if (lastSync > 0L) {
                    RelativeTime.describe(this@MainActivity, lastSync)
                } else {
                    getString(R.string.status_never)
                }
                append(getString(R.string.status_calls_synced, stored, whenText))
            }
            if (queued > 0) {
                append('\n')
                append(getString(R.string.status_calls_queued, queued))
            }
        }

    /** Each permission of the call group in one line, in plain words. */
    private fun callPermissionText(): String {
        val granted = { permission: String ->
            ContextCompat.checkSelfPermission(this, permission) ==
                PackageManager.PERMISSION_GRANTED
        }
        val lines = listOf(
            if (granted(Manifest.permission.READ_CALL_LOG)) {
                R.string.call_perm_read_yes
            } else {
                R.string.call_perm_read_no
            },
            if (granted(Manifest.permission.WRITE_CALL_LOG)) {
                R.string.call_perm_write_yes
            } else {
                R.string.call_perm_write_no
            },
            if (granted(Manifest.permission.CALL_PHONE)) {
                R.string.call_perm_call_yes
            } else {
                R.string.call_perm_call_no
            },
            if (granted(Manifest.permission.ANSWER_PHONE_CALLS)) {
                R.string.call_perm_answer_yes
            } else {
                R.string.call_perm_answer_no
            }
        )
        return buildString {
            for (line in lines) {
                if (isNotEmpty()) append('\n')
                append(getString(line))
            }
            if (notificationPermissionMissing()) {
                append('\n')
                append(getString(R.string.call_perm_notify_no))
            }
            if (!hasAllCallPermissions()) {
                append('\n')
                append(getString(R.string.call_perm_hint))
            }
        }
    }

    private fun hasCallLogPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.READ_CALL_LOG) ==
            PackageManager.PERMISSION_GRANTED

    private fun hasAllCallPermissions(): Boolean = callPermissions.all {
        ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
    }

    /**
     * Asks for the missing permissions of the call group.
     *
     * A denied permission is remembered by Android: asking again would
     * show nothing. In that case the app details screen is the only way
     * left, and the user is sent there instead of to a silent dialog.
     */
    private fun requestCallPermissions() {
        val missing = callPermissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        // Android 13+ asks for the in-call notification separately, so it
        // is requested here but deliberately kept out of
        // [hasAllCallPermissions]: a "no" to notifications must never
        // stand between the user and the dialer role.
        val wanted = if (notificationPermissionMissing()) {
            missing + Manifest.permission.POST_NOTIFICATIONS
        } else {
            missing
        }
        if (wanted.isEmpty()) {
            Toast.makeText(
                this,
                R.string.action_call_permissions_granted,
                Toast.LENGTH_SHORT
            ).show()
            updateCallCard()
            return
        }
        val permanentlyDenied = missing.any {
            !ActivityCompat.shouldShowRequestPermissionRationale(this, it)
        }
        // Only READ_CALL_LOG is needed to sync; the others are optional
        // and are asked for anyway, because a call the user answers from
        // the relay needs them.
        if (missing.isNotEmpty() && permanentlyDenied && missing.size == callPermissions.size) {
            Toast.makeText(this, R.string.call_perm_hint, Toast.LENGTH_LONG).show()
            start(Diagnostics.appDetailsIntent(this))
            return
        }
        callPermissionLauncher.launch(wanted.toTypedArray())
    }

    /**
     * True when the in-call notification would be dropped silently.
     *
     * Android 13 introduced the runtime permission; before it, posting a
     * notification needs no grant at all.
     */
    private fun notificationPermissionMissing(): Boolean =
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED

    /** Runs the call sync now; the worker itself is resumable. */
    private fun startCallSync() {
        if (!hasCallLogPermission()) {
            Toast.makeText(
                this,
                R.string.status_calls_no_permission,
                Toast.LENGTH_LONG
            ).show()
            requestCallPermissions()
            return
        }
        if (!settings.hasToken()) {
            Toast.makeText(
                this,
                R.string.import_error_not_paired,
                Toast.LENGTH_LONG
            ).show()
            return
        }
        Schedule.enqueueCallLog(this)
    }

    /** Progress of the one call-sync job, straight from WorkManager. */
    private fun observeCallSync() {
        WorkManager.getInstance(this)
            .getWorkInfosForUniqueWorkLiveData(Schedule.UNIQUE_CALL_LOG)
            .observe(this) { infos -> renderCallSyncState(infos.firstOrNull()) }
    }

    private fun renderCallSyncState(info: WorkInfo?) {
        if (info == null) {
            callSyncRunning = false
            buttonSyncCalls.isEnabled = true
            updateCallCard()
            return
        }
        when (info.state) {
            WorkInfo.State.ENQUEUED, WorkInfo.State.BLOCKED -> {
                callSyncRunning = true
                buttonSyncCalls.isEnabled = false
                textCallSyncState.text = getString(R.string.status_calls_waiting)
            }

            WorkInfo.State.RUNNING -> {
                callSyncRunning = true
                buttonSyncCalls.isEnabled = false
                val progress = info.progress
                textCallSyncState.text = getString(
                    R.string.status_calls_running,
                    progress.getInt(CallLogWorker.KEY_PROCESSED, 0)
                )
            }

            WorkInfo.State.SUCCEEDED -> {
                callSyncRunning = false
                buttonSyncCalls.isEnabled = true
                val result = info.outputData
                textCallSyncState.text = getString(
                    R.string.status_calls_done,
                    result.getInt(CallLogWorker.KEY_INSERTED, 0),
                    result.getInt(CallLogWorker.KEY_UPDATED, 0),
                    result.getInt(CallLogWorker.KEY_UNCHANGED, 0),
                    result.getInt(CallLogWorker.KEY_FAILED, 0)
                )
            }

            WorkInfo.State.FAILED -> {
                callSyncRunning = false
                buttonSyncCalls.isEnabled = true
                val reason = info.outputData.getString(CallLogWorker.KEY_ERROR).orEmpty()
                textCallSyncState.text = getString(
                    R.string.status_calls_failed,
                    callSyncError(reason)
                )
            }

            else -> {
                callSyncRunning = false
                buttonSyncCalls.isEnabled = true
                updateCallCard()
            }
        }
    }

    private fun callSyncError(reason: String): String = getString(
        when (reason) {
            CallLogWorker.ERROR_NO_PERMISSION -> R.string.calls_error_no_permission
            CallLogWorker.ERROR_NOT_PAIRED -> R.string.calls_error_not_paired
            CallLogWorker.ERROR_SERVER -> R.string.calls_error_server
            CallLogWorker.ERROR_READ -> R.string.calls_error_read
            else -> R.string.calls_error_unknown
        }
    )

    /**
     * Asks the system to make this app the default SMS app.
     *
     * From Android 10 on the SMS permission group is hard restricted: a
     * sideloaded app only receives it when the user makes it the default
     * SMS app. Without the role the app never sees an incoming SMS and
     * cannot send the cellular fallback.
     *
     * The button always answers the user. When the system refuses the
     * request, the default apps settings are opened instead, where the
     * same choice can be made by hand.
     */
    private fun requestDefaultSmsAppRole() {
        if (RoleState.isDefaultSmsApp(this)) {
            // Nothing to ask for: say so instead of doing nothing.
            Toast.makeText(this, R.string.default_sms_already, Toast.LENGTH_SHORT).show()
            refreshStatus()
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            requestSmsRoleApi29()
        } else {
            requestSmsRoleApi26()
        }
    }

    /**
     * Android 10 and newer: the role manager owns the choice. The system
     * dialog is started with the launcher, so the answer of the user is
     * known in [handleDefaultSmsAppResult].
     */
    @RequiresApi(Build.VERSION_CODES.Q)
    private fun requestSmsRoleApi29() {
        if (!RoleState.isRoleAvailable(this)) {
            // The phone has no SMS role at all. The settings screen is
            // the last chance to set the default SMS app.
            Toast.makeText(this, R.string.default_sms_unavailable, Toast.LENGTH_LONG).show()
            openDefaultSmsSettings()
            return
        }
        val roleManager = getSystemService(RoleManager::class.java)
        val intent = roleManager?.let { manager ->
            runCatching {
                manager.createRequestRoleIntent(RoleManager.ROLE_SMS)
            }.getOrNull()
        }
        if (intent == null) {
            openDefaultSmsSettings()
            return
        }
        runCatching { defaultSmsAppLauncher.launch(intent) }
            .onFailure { openDefaultSmsSettings() }
    }

    /**
     * Android 8 and 9: there is no role manager yet. The change of the
     * default SMS app is asked for with the platform intent, which shows
     * the system dialog of the phone.
     */
    private fun requestSmsRoleApi26() {
        val intent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
            .putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName)
        runCatching { defaultSmsAppLauncher.launch(intent) }
            .onFailure { openDefaultSmsSettings() }
    }

    /**
     * The system dialog is closed. The state of the app is read again
     * instead of trusting the result code: only the role itself tells
     * whether the SMS permissions are possible now.
     */
    private fun handleDefaultSmsAppResult(resultCode: Int) {
        if (RoleState.isDefaultSmsApp(this)) {
            // The role is granted: the SMS permissions can be asked for.
            requestMissingPermissions()
            Toast.makeText(this, R.string.default_sms_granted, Toast.LENGTH_SHORT).show()
        } else if (resultCode == Activity.RESULT_OK) {
            // The system said yes but the role was not granted: a vendor
            // build refused the change without a word.
            Toast.makeText(this, R.string.default_sms_not_applied, Toast.LENGTH_LONG).show()
        } else {
            Toast.makeText(this, R.string.default_sms_refused, Toast.LENGTH_LONG).show()
        }
        refreshStatus()
    }

    /**
     * The last resort: the settings screen where the default SMS app is
     * chosen by hand. The first candidate the system can open wins; when
     * none of them works the user gets a hint.
     */
    private fun openDefaultSmsSettings() {
        waitingForDefaultSmsChoice = true
        val candidates = listOf(
            // The screen of the default apps (Android 7 and newer).
            Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS) to
                R.string.default_sms_settings_opened,
            // The SMS screen of the phone (Android 8 and 9).
            Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
                .putExtra(Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, packageName) to
                R.string.default_sms_settings_opened,
            // The page of this app, which every phone has.
            Diagnostics.appDetailsIntent(this) to R.string.default_sms_manual
        )
        for ((candidate, message) in candidates) {
            if (tryStart(candidate)) {
                Toast.makeText(this, message, Toast.LENGTH_LONG).show()
                return
            }
        }
        waitingForDefaultSmsChoice = false
        Toast.makeText(this, R.string.default_sms_manual, Toast.LENGTH_LONG).show()
    }

    /** Starts an intent and reports whether the system could open it. */
    private fun tryStart(intent: Intent): Boolean =
        runCatching { startActivity(intent) }.isSuccess

    private fun requestMissingPermissions() {
        val missing = runtimePermissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), REQUEST_PERMISSIONS)
        }
    }

    private fun start(intent: Intent): Boolean =
        runCatching { startActivity(intent) }
            .onFailure { Toast.makeText(this, it.javaClass.simpleName, Toast.LENGTH_SHORT).show() }
            .isSuccess

    companion object {
        private const val REQUEST_PERMISSIONS = 1001
        private const val REQUEST_READ_CONTACTS = 1002
        private const val REQUEST_READ_SMS = 1003

        /** The size of the QR code on the screen. */
        private const val QR_SIZE_DP = 220
    }
}
