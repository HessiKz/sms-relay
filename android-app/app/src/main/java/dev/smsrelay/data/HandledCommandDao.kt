package dev.smsrelay.data

import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query

/**
 * A command that arrived from Telegram and was already handled by the app.
 *
 * The backend removes a command from the pending list once its result was
 * reported, but the report itself can fail (no network, wrong token). This
 * table is the local guarantee that a command is never executed twice: the
 * row is written BEFORE the SMS leaves the phone, so a lost report can only
 * cost a repeated status message, never a repeated SMS.
 */
@Entity(tableName = "handled_commands")
data class HandledCommandEntity(
    @PrimaryKey val id: String,
    /** handling | sent | delivered | failed | rejected. */
    val status: String,
    /** Short reason, never a message body. */
    val detail: String? = null,
    /** true once the backend acknowledged the result. */
    val reported: Boolean = false,
    val handledAt: Long = System.currentTimeMillis()
) {
    companion object {
        /** The radio took the message. */
        const val STATUS_SENT = "sent"

        /** The carrier confirmed delivery of every part. */
        const val STATUS_DELIVERED = "delivered"

        /** The message never left the phone. */
        const val STATUS_FAILED = "failed"

        /** The command was refused on purpose (sending is disabled). */
        const val STATUS_REJECTED = "rejected"

        /** The command was taken, the outcome is not known yet. */
        const val STATUS_HANDLING = "handling"
    }
}

@Dao
interface HandledCommandDao {

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsert(entry: HandledCommandEntity)

    /** The local guard: a known id is never sent again. */
    @Query("SELECT * FROM handled_commands WHERE id = :id LIMIT 1")
    fun findById(id: String): HandledCommandEntity?

    /** Results that the backend does not know about yet. */
    @Query(
        "SELECT * FROM handled_commands WHERE reported = 0" +
            " AND status IN ('sent', 'delivered', 'failed', 'rejected')" +
            " ORDER BY handledAt ASC LIMIT :limit"
    )
    fun unreported(limit: Int): List<HandledCommandEntity>

    /** Commands whose execution was interrupted (app killed mid send). */
    @Query(
        "SELECT * FROM handled_commands WHERE status = 'handling'" +
            " AND handledAt <= :before LIMIT :limit"
    )
    fun staleHandling(before: Long, limit: Int): List<HandledCommandEntity>

    /**
     * Outcome of the attempt, not reported yet.
     *
     * Only a row that is still "handling" is written: a radio or carrier
     * answer may have arrived while the send call was returning, and that
     * answer is newer than the attempt.
     */
    @Query(
        "UPDATE handled_commands SET status = :status, detail = :detail, reported = 0" +
            " WHERE id = :id AND status = 'handling'"
    )
    fun finishAttempt(id: String, status: String, detail: String?)

    /** Every part was delivered; no later sent answer may undo this. */
    @Query(
        "UPDATE handled_commands SET status = 'delivered', detail = NULL, reported = 0" +
            " WHERE id = :id AND status IN ('handling', 'sent')"
    )
    fun markDelivered(id: String): Int

    /** The radio or the carrier refused a part. */
    @Query(
        "UPDATE handled_commands SET status = 'failed', detail = :detail, reported = 0" +
            " WHERE id = :id AND status NOT IN ('delivered', 'failed', 'rejected')"
    )
    fun markFailed(id: String, detail: String?): Int

    /**
     * The backend knows the result now. The status is part of the match so
     * a newer outcome that arrived during the report is not lost.
     */
    @Query(
        "UPDATE handled_commands SET reported = 1" +
            " WHERE id = :id AND status = :status"
    )
    fun markReported(id: String, status: String)

    /**
     * Old rows are dropped: the backend forgets them as well. Rows that
     * were never reported are kept, because a late delivery report must
     * still reach the server.
     */
    @Query(
        "DELETE FROM handled_commands WHERE handledAt < :before AND reported = 1"
    )
    fun prune(before: Long)

    @Query("SELECT COUNT(*) FROM handled_commands")
    fun count(): Int
}
