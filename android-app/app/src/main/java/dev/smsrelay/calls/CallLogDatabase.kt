package dev.smsrelay.calls

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

/**
 * The call queue of the phone, kept apart from the SMS queue.
 *
 * A separate database is deliberate: the calls table is a fourth stream
 * next to messages, commands and contacts, and a problem in one stream
 * must never block the others. It also keeps the SMS database at its
 * current version instead of forcing a migration for a feature that
 * arrived later.
 *
 * Version 1 is the first schema. There is NO fallbackToDestructiveMigration
 * on purpose: this database is a queue, not a cache, and a destructive
 * migration would silently drop calls that were never uploaded. A future
 * schema change must come with a real [androidx.room.migration.Migration].
 */
@Database(
    entities = [CallLogEntity::class],
    version = 1,
    exportSchema = false
)
abstract class CallLogDatabase : RoomDatabase() {

    abstract fun callLogDao(): CallLogDao

    companion object {
        private const val NAME = "sms-relay-calls.db"

        @Volatile
        private var instance: CallLogDatabase? = null

        fun get(context: Context): CallLogDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    CallLogDatabase::class.java,
                    NAME
                )
                    .build()
                    .also { instance = it }
            }
    }
}
