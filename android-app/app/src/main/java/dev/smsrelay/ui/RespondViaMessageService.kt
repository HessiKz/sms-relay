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

    /**
     * The phone starts this component when a call is answered with a
     * message. The relay does not write messages, so the service stops
     * right away instead of staying in the background.
     */
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        stopSelf(startId)
        return START_NOT_STICKY
    }
}
