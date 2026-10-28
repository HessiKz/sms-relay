package dev.smsrelay.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import dev.smsrelay.work.Schedule

/** Re-arms the upload schedule after a reboot or an app update. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        when (intent.action) {
            Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED -> {
                Schedule.schedulePeriodic(context)
                Schedule.enqueueUpload(context)
                Schedule.scheduleCommands(context)
                Schedule.enqueueCommands(context)
            }
        }
    }
}
