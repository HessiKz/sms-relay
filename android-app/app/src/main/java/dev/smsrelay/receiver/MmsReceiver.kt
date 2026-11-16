package dev.smsrelay.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * Contract receiver for the default-SMS-app role.
 *
 * Android and several OEM role pickers require a receiver for
 * `android.provider.Telephony.WAP_PUSH_DELIVER` before an app can be
 * selected as the default messaging app. This relay only forwards SMS,
 * so the MMS push is acknowledged and ignored; the receiver exists to
 * satisfy the role contract, not to implement an MMS client.
 */
class MmsReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        // No MMS support in this relay.
    }
}
