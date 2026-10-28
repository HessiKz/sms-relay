package dev.smsrelay.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Update

@Dao
interface MessageDao {

    @Insert(onConflict = OnConflictStrategy.IGNORE)
    fun insert(record: MessageRecord): Long

    @Update
    fun update(record: MessageRecord)

    @Query("SELECT * FROM messages WHERE id = :id")
    fun findById(id: String): MessageRecord?

    @Query("SELECT * FROM messages WHERE uploadState = :state ORDER BY createdAt ASC LIMIT :limit")
    fun pendingUploads(state: String = MessageRecord.STATE_QUEUED, limit: Int = 20): List<MessageRecord>

    @Query("SELECT * FROM messages ORDER BY createdAt DESC LIMIT :limit OFFSET :offset")
    fun historyPage(limit: Int, offset: Int): List<MessageRecord>

    /** The newest message the phone saw, or null when there is none. */
    @Query("SELECT * FROM messages ORDER BY createdAt DESC LIMIT 1")
    fun latest(): MessageRecord?

    @Query("SELECT COUNT(*) FROM messages")
    fun count(): Int

    @Query("SELECT COUNT(*) FROM messages WHERE uploadState = :state")
    fun countByState(state: String): Int

    @Query("SELECT * FROM messages WHERE uploadState = :state ORDER BY createdAt ASC LIMIT :limit")
    fun byState(state: String, limit: Int): List<MessageRecord>

    /** Messages the fallback sweep may still have to look at. */
    @Query(
        "SELECT * FROM messages WHERE uploadState IN (:states)" +
            " AND fallbackState != :fallbackSent" +
            " AND receivedAt >= :since" +
            " ORDER BY receivedAt DESC LIMIT :limit"
    )
    fun fallbackCandidates(
        states: List<String>,
        fallbackSent: String,
        since: Long,
        limit: Int
    ): List<MessageRecord>

    /** Targeted update: only touches the fallback columns, so a stale
     *  in-memory record can never overwrite the upload state machine. */
    @Query("UPDATE messages SET fallbackState = :state WHERE id = :id")
    fun markFallbackState(id: String, state: String)

    /** Targeted update of the upload columns only. */
    @Query(
        "UPDATE messages SET uploadState = :uploadState, uploadAttempts = :attempts," +
            " lastAttemptAt = :attemptAt, lastError = :error, backendState = :backendState" +
            " WHERE id = :id"
    )
    fun markUploadState(
        id: String,
        uploadState: String,
        attempts: Int,
        attemptAt: Long,
        error: String?,
        backendState: String?
    )

    /** Targeted update of the state the backend reported for this message. */
    @Query(
        "UPDATE messages SET uploadState = :uploadState, backendState = :backendState" +
            " WHERE id = :id"
    )
    fun markBackendOutcome(id: String, uploadState: String, backendState: String)

    @Query("SELECT MIN(receivedAt) FROM messages WHERE uploadState = :state")
    fun oldestQueuedAt(state: String = MessageRecord.STATE_QUEUED): Long?

    /** Count messages with the same sender and body that arrived through
     *  another capture path in the time window. */
    @Query(
        "SELECT COUNT(*) FROM messages WHERE sender = :sender AND body = :body" +
            " AND receivedAt >= :since AND source != :source"
    )
    fun countRecentFromOtherSource(
        sender: String,
        body: String,
        since: Long,
        source: String
    ): Int

    /** Same, but on the normalised sender: two spellings of one number
     *  are the same sender. */
    @Query(
        "SELECT COUNT(*) FROM messages WHERE senderKey = :senderKey" +
            " AND body = :body AND receivedAt >= :since AND source != :source"
    )
    fun countRecentByKeyFromOtherSource(
        senderKey: String,
        body: String,
        since: Long,
        source: String
    ): Int

    @Query("DELETE FROM messages")
    fun deleteAll()
}
