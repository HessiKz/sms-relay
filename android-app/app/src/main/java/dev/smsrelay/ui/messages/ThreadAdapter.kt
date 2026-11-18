package dev.smsrelay.ui.messages

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import dev.smsrelay.R
import dev.smsrelay.messages.SmsMessage

/**
 * One row of the thread list: a bubble or a day separator.
 *
 * A row carries everything its view needs, already formatted. That keeps
 * `ListAdapter` honest: two rows are equal exactly when they look equal,
 * so a message whose read state or SIM label changed is re-bound and a
 * message that did not change is left alone.
 */
sealed class ThreadRow {

    /** The label that starts the messages of one local day. */
    data class DayHeader(val label: String, val epochDay: Long) : ThreadRow() {
        /**
         * Negative ids, so a header can never collide with a message.
         * The `+ 2` keeps the value away from -1, which `RecyclerView`
         * reserves as [RecyclerView.NO_ID].
         */
        override fun stableId(): Long = -epochDay - 2
    }

    /**
     * One message. [note] is the second half of the quiet line under the
     * bubble: "Failed", "Draft", "Sending…" or a SIM label, already
     * chosen by the screen. Null means "the time alone".
     */
    data class Bubble(val message: SmsMessage, val note: String?) : ThreadRow() {
        override fun stableId(): Long = message.id
    }

    /** The id the adapter hands to `RecyclerView`. */
    abstract fun stableId(): Long
}

/**
 * Groups messages into rows: a [ThreadRow.DayHeader] whenever the local
 * day changes, then the bubbles of that day in order.
 *
 * The day is the phone's local day, computed by [MessageDates], so a
 * message from 23:59 and one from 00:01 land in different groups even
 * when they are a minute apart.
 */
object ThreadRows {

    /**
     * The rows for a list of messages, oldest first.
     *
     * [keyOf], [labelOf] and [noteOf] are passed in so this function has
     * no dependency on Android resources and can be reasoned about on its
     * own.
     */
    fun rows(
        messages: List<SmsMessage>,
        keyOf: (SmsMessage) -> Long,
        labelOf: (Long) -> String,
        noteOf: (SmsMessage) -> String?
    ): List<ThreadRow> {
        val rows = ArrayList<ThreadRow>(messages.size + 4)
        var lastKey: Long? = null
        for (message in messages) {
            val key = keyOf(message)
            if (lastKey == null || key != lastKey) {
                rows.add(ThreadRow.DayHeader(labelOf(key), key))
                lastKey = key
            }
            rows.add(ThreadRow.Bubble(message, noteOf(message)))
        }
        return rows
    }
}

/**
 * The bubbles of one thread.
 *
 * Incoming and outgoing messages use two view types with their own
 * layouts: the sides differ in alignment and in the theme colours, and
 * keeping them apart means a bubble is inflated once and never
 * reconfigured from left to right while the list scrolls.
 */
class ThreadAdapter :
    ListAdapter<ThreadRow, RecyclerView.ViewHolder>(DIFF) {

    init {
        setHasStableIds(true)
    }

    override fun getItemViewType(position: Int): Int = when (val row = getItem(position)) {
        is ThreadRow.DayHeader -> TYPE_DAY
        is ThreadRow.Bubble -> if (row.message.type.isOutgoing) TYPE_OUT else TYPE_IN
    }

    override fun getItemId(position: Int): Long = getItem(position).stableId()

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): RecyclerView.ViewHolder {
        val inflater = LayoutInflater.from(parent.context)
        return when (viewType) {
            TYPE_DAY -> DayHolder(inflater.inflate(R.layout.item_day_header, parent, false))
            TYPE_OUT -> BubbleHolder(inflater.inflate(R.layout.item_message_out, parent, false))
            else -> BubbleHolder(inflater.inflate(R.layout.item_message_in, parent, false))
        }
    }

    override fun onBindViewHolder(holder: RecyclerView.ViewHolder, position: Int) {
        when (val row = getItem(position)) {
            is ThreadRow.DayHeader -> (holder as DayHolder).bind(row.label)
            is ThreadRow.Bubble -> (holder as BubbleHolder).bind(row.message, row.note)
        }
    }

    /** The day pill between two groups of messages. */
    class DayHolder(view: View) : RecyclerView.ViewHolder(view) {
        private val label: TextView = view.findViewById(R.id.textDayHeader)

        fun bind(text: String) {
            label.text = text
            // A separator is a label, not a control: a screen reader
            // announces it instead of skipping it as decoration.
            itemView.contentDescription = text
        }
    }

    /** One message, on the side the inflated layout chose. */
    class BubbleHolder(view: View) : RecyclerView.ViewHolder(view) {

        private val body: TextView = view.findViewById(R.id.textBody)
        private val meta: TextView = view.findViewById(R.id.textMeta)

        fun bind(message: SmsMessage, note: String?) {
            body.text = message.body
            val time = MessageDates.time(message.dateMillis)
            meta.text = when {
                note.isNullOrEmpty() -> time
                time.isEmpty() -> note
                else -> itemView.context.getString(R.string.message_meta_with_note, time, note)
            }
            meta.visibility = if (meta.text.isNullOrEmpty()) View.GONE else View.VISIBLE
        }
    }

    private companion object {
        const val TYPE_DAY = 0
        const val TYPE_IN = 1
        const val TYPE_OUT = 2

        val DIFF = object : DiffUtil.ItemCallback<ThreadRow>() {
            override fun areItemsTheSame(oldItem: ThreadRow, newItem: ThreadRow): Boolean =
                oldItem.stableId() == newItem.stableId()

            override fun areContentsTheSame(oldItem: ThreadRow, newItem: ThreadRow): Boolean =
                oldItem == newItem
        }
    }
}
