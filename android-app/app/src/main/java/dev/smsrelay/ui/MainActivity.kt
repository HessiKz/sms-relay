package dev.smsrelay.ui

import android.Manifest
import android.app.role.RoleManager
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.work.WorkInfo
import androidx.work.WorkManager
import dev.smsrelay.R
import dev.smsrelay.SettingsStore
import dev.smsrelay.contacts.ContactsSync
import dev.smsrelay.contacts.ContactResolver
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.net.RelayClient
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
    private lateinit var inputToken: EditText
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

    private val runtimePermissions = arrayOf(
        Manifest.permission.RECEIVE_SMS,
        Manifest.permission.READ_SMS,
        Manifest.permission.SEND_SMS,
        Manifest.permission.READ_PHONE_STATE,
        Manifest.permission.READ_CONTACTS
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        settings = SettingsStore(this)

        inputBaseUri = findViewById(R.id.inputBaseUri)
        inputToken = findViewById(R.id.inputToken)
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

        inputBaseUri.setText(settings.baseUri)
        inputToken.setText(settings.token)
        // The backup number is pre-filled so the app is ready to use.
        inputPhoneB.setText(settings.phoneB.ifBlank { SettingsStore.DEFAULT_PHONE_B })
        checkForward.isChecked = settings.forwardEnabled
        checkFallback.isChecked = settings.fallbackEnabled
        checkSending.isChecked = settings.sendingEnabled
        updateSendingState()
        updateContactsState()

        requestMissingPermissions()
        requestDefaultSmsAppRole()
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
        observeImport()
    }

    override fun onResume() {
        super.onResume()
        // The cards of the phone are only known once the permission for
        // them is granted: the picker is refreshed on every resume.
        setupSimSelector()
        refreshStatus()
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

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_READ_CONTACTS) {
            if (ContactsSync.hasPermission(this)) {
                syncContactsNow()
            } else {
                updateContactsState()
            }
        }
    }

    private fun save() {
        settings.baseUri = inputBaseUri.text.toString()
        settings.token = inputToken.text.toString()
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
            ImportWorker.ERROR_REJECTED -> R.string.import_error_rejected
            ImportWorker.ERROR_SERVER -> R.string.import_error_server
            ImportWorker.ERROR_READ -> R.string.import_error_read
            else -> R.string.import_error_unknown
        }
    )

    /**
     * On Android 10+ the SMS permission group is hard restricted: a
     * sideloaded app only receives it when the user makes it the default
     * SMS app. Without the role the app never sees an incoming SMS and
     * cannot send the cellular fallback.
     */
    private fun requestDefaultSmsAppRole() {
        if (android.os.Build.VERSION.SDK_INT < android.os.Build.VERSION_CODES.Q) return
        val roleManager = getSystemService(RoleManager::class.java) ?: return
        if (roleManager.isRoleAvailable(RoleManager.ROLE_SMS) &&
            roleManager.isRoleHeld(RoleManager.ROLE_SMS)
        ) {
            return
        }
        runCatching {
            startActivity(roleManager.createRequestRoleIntent(RoleManager.ROLE_SMS))
        }.onFailure {
            Toast.makeText(this, R.string.status_failed, Toast.LENGTH_SHORT).show()
        }
    }

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
    }
}
