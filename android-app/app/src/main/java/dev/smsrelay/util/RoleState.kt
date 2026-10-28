package dev.smsrelay.util

import android.Manifest
import android.app.role.RoleManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Telephony
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
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
        val roleManager = context.getSystemService(RoleManager::class.java) ?: return false
        return runCatching {
            roleManager.isRoleAvailable(RoleManager.ROLE_SMS) &&
                roleManager.isRoleHeld(RoleManager.ROLE_SMS)
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
}
