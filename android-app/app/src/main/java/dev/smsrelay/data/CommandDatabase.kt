package dev.smsrelay.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * Local bookkeeping of the commands that were executed for Telegram.
 *
 * A separate database on purpose: the message database must not be touched
 * by a schema change, because the queued messages are the whole point of
 * the relay and a destructive migration would drop them.
 */
@Database(
    entities = [HandledCommandEntity::class],
    version = 1,
    exportSchema = false
)
abstract class CommandDatabase : RoomDatabase() {

    abstract fun handledCommandDao(): HandledCommandDao

    companion object {
        @Volatile
        private var instance: CommandDatabase? = null

        fun get(context: Context): CommandDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    CommandDatabase::class.java,
                    "sms-relay-commands.db"
                ).build().also { instance = it }
            }
    }
}
