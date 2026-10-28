package dev.smsrelay.ui

import android.app.Service
import android.content.Intent
import android.os.IBinder

/**
 * Stub for the `RESPOND_VIA_MESSAGE` contract of the default SMS app
 * role. The relay does not offer quick replies, so the service reports
 * that it cannot handle the intent and stops.
 */
class RespondViaMessageService : Service() {

    override fun onBind(intent: Intent?): IBinder? {
        stopSelf()
        return null
    }
}
