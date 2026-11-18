package dev.smsrelay.util

import android.Manifest
import android.app.role.RoleManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Telephony
import android.telecom.TelecomManager
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import dev.smsrelay.BuildConfig
import dev.smsrelay.R

/**
 * Which capture paths are currently available.
 *
 * On Android 10+ the SMS permission group is hard restricted, so the app
 * only receives messages when it is the default SMS app. When it is not,
 * the notification listener path can still pick the message up from the
 * messaging app's notification (if the user granted notification access
 * and the messaging app shows a preview).
 */
object RoleState {

    fun isDefaultSmsApp(context: Context): Boolean {
        // Before Android 10 there is no role manager; the default SMS app
        // is still tracked by the platform, and lying about it would hide
        // the broadcast path from the rest of the app.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return Telephony.Sms.getDefaultSmsPackage(context) == context.packageName
        }
        return isRoleAvailable(context) && isRoleHeld(context)
    }

    /**
     * True when this phone can be asked to make this app the default SMS
     * app, in either era - the same distinction as
     * [isDialerRoleAvailable], and for the same reason.
     *
     * Use this for reporting and for deciding whether a request is worth
     * starting. [isRoleAvailable] stays a strict role-manager primitive:
     * it answers false on Android 8 and 9 by design, and a caller that
     * reads it as "this phone cannot be the default SMS app" is wrong
     * there. The screens already branch on the SDK and never consult it
     * below Android 10.
     */
    fun isSmsRoleAvailable(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) return isRoleAvailable(context)
        // Android 8 and 9: no role manager, but `ACTION_CHANGE_DEFAULT` is
        // how the platform has always asked, and it is what the request
        // path uses.
        val intent = Intent(Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
        return runCatching { intent.resolveActivity(context.packageManager) != null }
            .getOrDefault(false)
    }

    /**
     * True when the phone knows the default-SMS-app role at all.
     *
     * A phone without telephony, or a vendor build that took the role
     * away, has no role to ask for: the default apps settings are the
     * only way left.
     *
     * This is the role-manager answer only: it is false on Android 8 and
     * 9, where the platform has no roles. Ask [isSmsRoleAvailable] when
     * the question is "can this app be the default SMS app at all".
     */
    fun isRoleAvailable(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
        val roleManager = context.getSystemService(RoleManager::class.java) ?: return false
        return runCatching {
            roleManager.isRoleAvailable(RoleManager.ROLE_SMS)
        }.getOrDefault(false)
    }

    /** True when this app holds the default-SMS-app role (Android 10+). */
    fun isRoleHeld(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return false
        val roleManager = context.getSystemService(RoleManager::class.java) ?: return false
        return runCatching {
            roleManager.isRoleHeld(RoleManager.ROLE_SMS)
        }.getOrDefault(false)
    }

    /**
     * True when this phone can be asked to make this app the dialer.
     *
     * There are two eras here, and conflating them hid the dialer action
     * on Android 8 and 9:
     *
     *  - Android 10 and newer: the role manager owns the choice, so the
     *    answer is `isRoleAvailable(ROLE_DIALER)`. A phone whose vendor
     *    removed the role has no dialog to show, and the default apps
     *    settings are then the only way left.
     *  - Android 8 and 9: there is no role manager at all, but the role
     *    still exists - the telecom service tracks the default dialer
     *    package and accepts `ACTION_CHANGE_DEFAULT_DIALER`. Reporting
     *    "unavailable" there was simply wrong, and it is what made the
     *    card hide its action on those releases.
     *
     * A `true` answer never traps the user: [dialerRoleIntent] returns
     * null when neither path can be resolved, and every caller falls back
     * to the default apps settings in that case.
     */
    fun isDialerRoleAvailable(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = context.getSystemService(RoleManager::class.java) ?: return false
            return runCatching {
                roleManager.isRoleAvailable(RoleManager.ROLE_DIALER)
            }.getOrDefault(false)
        }
        // Android 8 and 9: the role exists, it is just not modelled by the
        // role manager. The telecom service is what owns the default
        // dialer, and its presence is the honest availability answer.
        if (context.getSystemService(TelecomManager::class.java) != null) return true
        // No telecom service (a build without telephony): the intent is
        // the last thing worth checking before the settings fallback.
        return dialerRoleIntent(context) != null
    }

    /**
     * The system intent that asks the user to make this app the dialer.
     *
     * Android 10+ owns the choice in the role manager. Android 8 and 9
     * have no role yet: there the platform still accepts
     * `TelecomManager.ACTION_CHANGE_DEFAULT_DIALER` with the package name
     * of the candidate, which shows the same system dialog.
     *
     * Returns null when the phone offers neither path; the caller then
     * sends the user to the default-apps settings instead of doing
     * nothing. The answer is never trusted: the role is re-read.
     */
    fun dialerRoleIntent(context: Context): Intent? {
        val packageName = context.packageName
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = context.getSystemService(RoleManager::class.java)
                ?: return null
            if (!runCatching { roleManager.isRoleAvailable(RoleManager.ROLE_DIALER) }
                    .getOrDefault(false)
            ) {
                return null
            }
            return runCatching {
                roleManager.createRequestRoleIntent(RoleManager.ROLE_DIALER)
            }.getOrNull()
        }
        // Android 8 and 9: the telecom service shows the dialog. The
        // extra is the one the platform reads for this action.
        val intent = Intent(TelecomManager.ACTION_CHANGE_DEFAULT_DIALER)
            .putExtra(TelecomManager.EXTRA_CHANGE_DEFAULT_DIALER_PACKAGE_NAME, packageName)
        return if (intent.resolveActivity(context.packageManager) != null) intent else null
    }

    /**
     * True when this app is the phone's dialer.
     *
     * Android 10+ owns the choice in the role manager. Android 8 and 9
     * have no role yet; there the platform tracks the default dialer
     * package in the telecom service, which is the same thing the role
     * manager reports on newer releases.
     */
    fun isDialerRoleHeld(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val roleManager = context.getSystemService(RoleManager::class.java)
                ?: return false
            return runCatching {
                roleManager.isRoleHeld(RoleManager.ROLE_DIALER)
            }.getOrDefault(false)
        }
        val telecom = context.getSystemService(TelecomManager::class.java)
            ?: return false
        return runCatching {
            telecom.defaultDialerPackage == context.packageName
        }.getOrDefault(false)
    }

    fun hasNotificationAccess(context: Context): Boolean {
        return try {
            NotificationManagerCompat.getEnabledListenerPackages(context)
                .contains(context.packageName)
        } catch (e: Exception) {
            false
        }
    }

    /**
     * True when incoming messages reach the app through the normal SMS
     * broadcast: before Android 10 the runtime permissions are enough,
     * from Android 10 on only the default SMS app gets the broadcast.
     */
    fun canReceiveSmsBroadcast(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) {
            return ContextCompat.checkSelfPermission(
                context, Manifest.permission.RECEIVE_SMS
            ) == PackageManager.PERMISSION_GRANTED
        }
        return isDefaultSmsApp(context)
    }

    /** True when the app cannot receive anything at all. */
    fun isDeaf(context: Context): Boolean =
        !canReceiveSmsBroadcast(context) && !hasNotificationAccess(context)

    fun statusText(context: Context): String {
        val defaultApp = isDefaultSmsApp(context)
        val notifications = hasNotificationAccess(context)
        return when {
            defaultApp -> context.getString(R.string.capture_state_default_app)
            notifications -> context.getString(R.string.capture_state_notifications)
            else -> context.getString(R.string.capture_state_none)
        }
    }

    /**
     * The facts an operator needs to say why a role is or is not held.
     *
     * It is built here, from the same helpers the screens use, so the
     * report cannot disagree with the card next to it. It only holds
     * settings and permissions: no number, name or message is ever in it.
     */
    fun diagnosticsReport(context: Context): String {
        val smsPackage = runCatching {
            Telephony.Sms.getDefaultSmsPackage(context)
        }.getOrNull()
        val dialerPackage = runCatching {
            context.getSystemService(TelecomManager::class.java)?.defaultDialerPackage
        }.getOrNull()
        val lines = ArrayList<String>(14)
        // The version first: it is the one line that says which build
        // produced the rest of the report, and an operator comparing two
        // APKs needs it before any role state.
        lines.add(
            context.getString(
                R.string.diagnostics_version,
                BuildConfig.VERSION_NAME,
                BuildConfig.VERSION_CODE
            )
        )
        lines.add(context.getString(R.string.diagnostics_android, Build.VERSION.SDK_INT))
        // Both SMS lines go through the era-aware helpers. Reporting
        // `isRoleAvailable`/`isRoleHeld` directly said "no" to both on
        // Android 8 and 9 even with this app already the default SMS app,
        // which is the fastest way to send the next investigation after a
        // fault that does not exist.
        lines.add(
            context.getString(
                R.string.diagnostics_role_sms_available,
                yesNo(context, isSmsRoleAvailable(context))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_role_sms_held,
                yesNo(context, isDefaultSmsApp(context))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_role_dialer_available,
                yesNo(context, isDialerRoleAvailable(context))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_role_dialer_held,
                yesNo(context, isDialerRoleHeld(context))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_sms_package,
                smsPackage ?: context.getString(R.string.diagnostics_none)
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_dialer_package,
                dialerPackage ?: context.getString(R.string.diagnostics_none)
            )
        )
        lines.add(context.getString(R.string.diagnostics_package, context.packageName))
        lines.add(
            context.getString(
                R.string.diagnostics_permission,
                "READ_SMS",
                yesNo(context, hasPermission(context, Manifest.permission.READ_SMS))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_permission,
                "READ_CALL_LOG",
                yesNo(context, hasPermission(context, Manifest.permission.READ_CALL_LOG))
            )
        )
        lines.add(
            context.getString(
                R.string.diagnostics_permission,
                "READ_CONTACTS",
                yesNo(context, hasPermission(context, Manifest.permission.READ_CONTACTS))
            )
        )
        return lines.joinToString("\n")
    }

    private fun hasPermission(context: Context, permission: String): Boolean =
        runCatching {
            ContextCompat.checkSelfPermission(context, permission) ==
                PackageManager.PERMISSION_GRANTED
        }.getOrDefault(false)

    private fun yesNo(context: Context, value: Boolean): String = context.getString(
        if (value) R.string.diagnostics_yes else R.string.diagnostics_no
    )
}
