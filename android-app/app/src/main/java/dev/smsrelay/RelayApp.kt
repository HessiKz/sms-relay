package dev.smsrelay

import android.app.Application
import dev.smsrelay.contacts.ContactsSync
import dev.smsrelay.work.Schedule

class RelayApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Schedule.schedulePeriodic(this)
        Schedule.scheduleCommands(this)
        Schedule.enqueueCommands(this)
        // The contact list of the phone goes to the server in the
        // background, but at most every six hours. Nothing happens
        // without the permission of the user.
        ContactsSync.syncIfDue(this)
    }
}
