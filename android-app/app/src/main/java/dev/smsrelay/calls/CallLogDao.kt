package dev.smsrelay.calls

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

/**
 * The local call queue.
 *
 * A row is written once, then moved from "queued" to "sent" or
 * "failed". The updates are targeted queries, so a stale in-memory
 * entity can never overwrite the upload state machine - the same rule
 * [dev.smsrelay.data.MessageDao] follows.
 */
@Dao
interface CallLogDao {

    /**
     * Writes a batch of new rows. A row that is already stored keeps its
     * state and its attempt counter: re-reading the log must not reset
     * the retry bookkeeping of a pending row.
     */
    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun insertAll(records: List<CallLogEntity>): List<Long>

    /** Replaces known rows (the rolling recent window updates a call). */
    @Insert(onConflict = OnConflictStrategy.REPLACE)
    fun upsertAll(records: List<CallLogEntity>)

    @Query(
        "SELECT * FROM call_log WHERE uploadState = 'queued'" +
            " ORDER BY startedAt ASC LIMIT :limit"
    )
    fun pending(limit: Int = 100): List<CallLogEntity>

    @Query(
        "UPDATE call_log SET uploadState = 'sent', attempts = attempts + 1," +
            " lastError = NULL WHERE id = :id"
    )
    fun markSent(id: String)

    @Query(
        "UPDATE call_log SET uploadState = 'failed', attempts = attempts + 1," +
            " lastError = :error WHERE id = :id"
    )
    fun markFailed(id: String, error: String?)

    /** How many call rows still wait, for the status line of the screen. */
    @Query("SELECT COUNT(*) FROM call_log WHERE uploadState = 'queued'")
    fun countQueued(): Int

    /** How many rows this phone has stored, queued or not. */
    @Query("SELECT COUNT(*) FROM call_log")
    fun countStored(): Int

    /**
     * The stored row of a provider row, or null when it is new.
     *
     * A call row is rewritten by the provider when the call ends or when
     * the user reads it, so the same `_ID` can arrive with another
     * content. The provider id is the identity; the content hash inside
     * the row only says whether something the server stores changed.
     */
    @Query("SELECT * FROM call_log WHERE providerId = :providerId LIMIT 1")
    fun findByProvider(providerId: Long): CallLogEntity?
}
