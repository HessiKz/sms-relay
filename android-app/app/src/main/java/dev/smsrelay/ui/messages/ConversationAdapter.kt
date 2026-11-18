package dev.smsrelay.ui.messages

import android.content.Context
import android.graphics.Typeface
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import dev.smsrelay.R
import dev.smsrelay.messages.SmsThread

/**
 * The conversation list, one row per thread of the phone's SMS store.
 *
 * A [ListAdapter] with a stable id per thread: a new message moves a row
 * to the top and the list animates the move instead of redrawing
 * everything, and `notifyDataSetChanged` is never needed.
 *
 * The row follows the messaging inbox: avatar with initials, the name
 * (bold when unread), a one line snippet, a relative date and, only when
 * there is more than one unread message, a counter. Every colour comes
 * from theme attributes, so dark mode needs nothing here.
 */
class ConversationAdapter(
    private val onClick: (SmsThread) -> Unit
) : ListAdapter<SmsThread, ConversationAdapter.Holder>(DIFF) {

    init {
        // Stable ids let a moved row keep its view and its ripple state.
        setHasStableIds(true)
    }

    override fun getItemId(position: Int): Long = getItem(position).threadId

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_conversation, parent, false)
        return Holder(view, onClick)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.bind(getItem(position))
    }

    class Holder(
        view: View,
        private val onClick: (SmsThread) -> Unit
    ) : RecyclerView.ViewHolder(view) {

        private val context: Context = view.context
        private val avatar: TextView = view.findViewById(R.id.textAvatar)
        private val unreadDot: View = view.findViewById(R.id.viewUnreadDot)
        private val name: TextView = view.findViewById(R.id.textName)
        private val snippet: TextView = view.findViewById(R.id.textSnippet)
        private val date: TextView = view.findViewById(R.id.textDate)
        private val unreadCount: TextView = view.findViewById(R.id.textUnreadCount)

        fun bind(thread: SmsThread) {
            val label = ThreadLabels.of(context, thread)

            avatar.text = AvatarDrawable.initials(label)
            avatar.background = AvatarDrawable.circle(thread.avatarKey)
            avatar.contentDescription = context.getString(R.string.messages_avatar_of, label)

            name.text = label
            name.setTypeface(null, if (thread.unread > 0) Typeface.BOLD else Typeface.NORMAL)

            snippet.text = thread.snippet.trim()
                .replace('\n', ' ')
                .ifEmpty { context.getString(R.string.messages_no_snippet) }
            date.text = MessageDates.rowLabel(context, thread.dateMillis)

            unreadDot.visibility = if (thread.unread > 0) View.VISIBLE else View.GONE
            if (thread.unread > 1) {
                unreadCount.visibility = View.VISIBLE
                unreadCount.text = context.getString(
                    R.string.messages_unread_count,
                    thread.unread
                )
            } else {
                unreadCount.visibility = View.GONE
            }

            // One description for the whole row: a screen reader does not
            // have to walk three separate labels, and the unread state is
            // part of the sentence instead of being a coloured dot.
            itemView.contentDescription = when {
                thread.unread > 1 -> context.getString(R.string.messages_unread_many, thread.unread) +
                    ", " + label
                thread.unread == 1 -> context.getString(R.string.messages_unread_one) + ", " + label
                else -> label
            }
            itemView.setOnClickListener { onClick(thread) }
        }
    }

    private companion object {
        val DIFF = object : DiffUtil.ItemCallback<SmsThread>() {
            override fun areItemsTheSame(oldItem: SmsThread, newItem: SmsThread): Boolean =
                oldItem.threadId == newItem.threadId

            override fun areContentsTheSame(oldItem: SmsThread, newItem: SmsThread): Boolean =
                oldItem == newItem
        }
    }
}
