package dev.smsrelay.util

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings

/**
 * Diagnostics helpers: permissions, battery optimisation exemption and
 * the vendor-specific autostart screen.
 *
 * Every intent is best effort -- every OEM ships a different menu and
 * none of them is guaranteed. The deep links are started explicitly
 * (component name) and failures are reported by the caller, because on
 * API 30+ `resolveActivity()` cannot see vendor packages unless they are
 * declared in <queries>.
 */
object Diagnostics {

    fun isIgnoringBatteryOptimizations(context: Context): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true
        val power = context.getSystemService(PowerManager::class.java) ?: return false
        return power.isIgnoringBatteryOptimizations(context.packageName)
    }

    /**
     * The per-app "ignore battery optimisations" dialog. It needs the
     * REQUEST_IGNORE_BATTERY_OPTIMIZATIONS permission; if the platform
     * refuses it, the user is sent to the app's detail page instead.
     */
    fun batterySettingsIntent(context: Context): Intent {
        val request = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            .setData(Uri.fromParts("package", context.packageName, null))
        return request
    }

    /** Fallback: the list of all apps, which needs no permission. */
    fun batteryListIntent(): Intent =
        Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)

    /** Xiaomi autostart screen; null on other devices. */
    fun autostartIntent(context: Context): Intent? {
        val candidates = listOf(
            Intent().setComponent(
                android.content.ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.permcenter.autostart.AutoStartManagementActivity"
                )
            ),
            Intent().setComponent(
                android.content.ComponentName(
                    "com.miui.securitycenter",
                    "com.miui.powerkeeper.ui.HiddenAppsConfigActivity"
                )
            )
        )
        for (candidate in candidates) {
            val exists = runCatching {
                context.packageManager.resolveActivity(candidate, 0)
            }.getOrNull()
            if (exists != null) return candidate
        }
        return null
    }

    fun appDetailsIntent(context: Context): Intent =
        Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            .setData(Uri.fromParts("package", context.packageName, null))

    fun isXiaomi(): Boolean =
        Build.MANUFACTURER.equals("xiaomi", ignoreCase = true)

    fun isSamsung(): Boolean =
        Build.MANUFACTURER.equals("samsung", ignoreCase = true)
}
