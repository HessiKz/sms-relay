package dev.smsrelay.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.PrimaryKey
import androidx.room.Query

@Entity(tableName = "fallback_log")
data class FallbackLogEntity(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    val messageId: String,
    val sentAt: Long,
    /** true only after the radio confirmed the SMS. */
    val ok: Boolean,
    val error: String? = null
)

@Dao
interface FallbackLogDao {

    @Insert
    fun insert(entry: FallbackLogEntity): Long

    @Query("UPDATE fallback_log SET ok = :ok, error = :error WHERE id = :id")
    fun markResultById(id: Long, ok: Boolean, error: String?)

    /** Radio confirmed the SMS: only these count towards the caps. */
    @Query("SELECT COUNT(*) FROM fallback_log WHERE sentAt >= :since AND ok = 1")
    fun countConfirmedSince(since: Long): Int

    /** Radio confirmed at least one SMS for this message. */
    @Query("SELECT COUNT(*) FROM fallback_log WHERE messageId = :messageId AND ok = 1")
    fun countConfirmedForMessage(messageId: String): Int

    @Query("SELECT MAX(sentAt) FROM fallback_log WHERE ok = 1")
    fun lastConfirmedAt(): Long?

    @Query("SELECT MAX(sentAt) FROM fallback_log")
    fun lastAttemptAt(): Long?

    @Query(
        "UPDATE fallback_log SET ok = :ok, error = :error" +
            " WHERE id = (SELECT MAX(id) FROM fallback_log WHERE messageId = :messageId)"
    )
    fun markResult(messageId: String, ok: Boolean, error: String?)

    @Query("SELECT COUNT(*) FROM fallback_log WHERE messageId = :messageId")
    fun countAttemptsForMessage(messageId: String): Int
}
