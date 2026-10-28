package dev.smsrelay.ui

import android.os.Bundle
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import dev.smsrelay.R

/**
 * Minimal handler for `ACTION_SENDTO` with the sms:/smsto:/mms:/mmsto:
 * schemes.
 *
 * Android only grants the SMS permission group to the app the user
 * chooses as the default SMS app, and becoming the default SMS app
 * requires exactly this component. It is not meant to be a full
 * messenger: the relay forwards everything to Telegram, so this screen
 * only explains that and offers to open the phone's own dialer.
 */
class ComposeSmsActivity : AppCompatActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_compose_sms)
        findViewById<TextView>(R.id.textComposeInfo).text = getString(
            R.string.compose_sms_info
        )
        findViewById<TextView>(R.id.textComposeAddress).text =
            intent.data?.schemeSpecificPart.orEmpty()
    }

    override fun onResume() {
        super.onResume()
        // Nothing to compose here: the relay app only observes and
        // forwards. Returning immediately keeps the default-SMS-app role
        // usable without pretending to be a messenger.
        finish()
    }
}
