package dev.smsrelay.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase

@Database(
    entities = [MessageRecord::class, FallbackLogEntity::class],
    version = 4,
    exportSchema = false
)
abstract class AppDatabase : RoomDatabase() {

    abstract fun messageDao(): MessageDao
    abstract fun fallbackLogDao(): FallbackLogDao

    companion object {
        /**
         * Adds the normalised sender column. The existing rows keep their
         * history (a destructive migration would throw the queue away).
         */
        val MIGRATION_1_2 = object : androidx.room.migration.Migration(1, 2) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE messages ADD COLUMN senderKey TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "UPDATE messages SET senderKey = sender WHERE senderKey = ''"
                )
            }
        }

        /**
         * Adds the column that names the SIM card a message came in on.
         *
         * The old rows keep their history: they simply do not know the
         * card (0 = unknown). The column is added HERE and not to the
         * 1 -> 2 migration, because a database that came through
         * 1 -> 2 would then have the column twice.
         */
        val MIGRATION_2_3 = object : androidx.room.migration.Migration(2, 3) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE messages ADD COLUMN subId INTEGER NOT NULL DEFAULT 0"
                )
            }
        }

        /**
         * Adds the capture path. Two messages with the same sender and
         * body are only duplicates when they came in through different
         * paths (broadcast vs notification); two genuine repeats from the
         * same path keep their own rows.
         */
        val MIGRATION_3_4 = object : androidx.room.migration.Migration(3, 4) {
            override fun migrate(db: androidx.sqlite.db.SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE messages ADD COLUMN source TEXT NOT NULL DEFAULT 'sms'"
                )
            }
        }

        @Volatile
        private var instance: AppDatabase? = null

        fun get(context: Context): AppDatabase =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext,
                    AppDatabase::class.java,
                    "sms-relay.db"
                )
                    .addMigrations(MIGRATION_1_2, MIGRATION_2_3, MIGRATION_3_4)
                    .build()
                    .also { instance = it }
            }
    }
}
