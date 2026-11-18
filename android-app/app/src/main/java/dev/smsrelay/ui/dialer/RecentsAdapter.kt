package dev.smsrelay.ui.dialer

import android.content.Context
import android.graphics.Typeface
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import dev.smsrelay.R
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.ui.messages.AvatarDrawable
import dev.smsrelay.ui.messages.MessageDates
import com.google.android.material.R as MaterialR

/**
 * The Recents tab: one row per call of the phone's log.
 *
 * The row is the four things a call log has to answer - who, what kind of
 * call, when, and how long - and a missed call is the only row that is
 * allowed a colour. Everything else is a theme attribute, so dark mode
 * needs nothing here.
 *
 * A row with no number (a private caller) still appears: hiding it would
 * hide a call the user received. It simply has no row actions beyond the
 * delete.
 */
class RecentsAdapter(
    private val onClick: (DialerRepository.RecentCall) -> Unit,
    private val onLongClick: (DialerRepository.RecentCall) -> Unit
) : ListAdapter<DialerRepository.RecentCall, RecentsAdapter.Holder>(DIFF) {

    init {
        // The provider id is stable: a deleted row animates out instead of
        // shifting every row below it.
        setHasStableIds(true)
    }

    override fun getItemId(position: Int): Long = getItem(position).id

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_recent_call, parent, false)
        return Holder(view, onClick, onLongClick)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.bind(getItem(position))
    }

    class Holder(
        view: View,
        private val onClick: (DialerRepository.RecentCall) -> Unit,
        private val onLongClick: (DialerRepository.RecentCall) -> Unit
    ) : RecyclerView.ViewHolder(view) {

        private val context: Context = view.context
        private val avatarFrame: View = view.findViewById(R.id.frameRecentAvatar)
        private val avatar: TextView = view.findViewById(R.id.textRecentAvatar)
        private val avatarIcon: ImageView = view.findViewById(R.id.imageRecentAvatar)
        private val name: TextView = view.findViewById(R.id.textRecentName)
        private val typeIcon: ImageView = view.findViewById(R.id.imageRecentType)
        private val meta: TextView = view.findViewById(R.id.textRecentMeta)
        private val date: TextView = view.findViewById(R.id.textRecentDate)

        fun bind(call: DialerRepository.RecentCall) {
            val known = call.cachedName?.trim().orEmpty()
            val label = known.ifEmpty { displayNumber(call.number) }
            // A private number is an empty string in the provider: the row
            // needs a sentence, not a blank line.
            val title = label.ifEmpty { context.getString(R.string.call_private_number) }

            name.text = title
            // An unread missed call is bold as well as red: that is the one
            // row a user is expected to act on.
            name.setTypeface(
                null,
                if (call.kind == DialerRepository.CallKind.MISSED && !call.isRead) {
                    Typeface.BOLD
                } else {
                    Typeface.NORMAL
                }
            )
            // A missed call is the one state worth a colour.
            name.setTextColor(
                if (call.kind == DialerRepository.CallKind.MISSED) {
                    AvatarDrawable.themeColor(
                        context,
                        MaterialR.attr.colorError,
                        0xFFB3261E.toInt()
                    )
                } else {
                    AvatarDrawable.themeColor(
                        context,
                        MaterialR.attr.colorOnSurface,
                        0xFF1D1B20.toInt()
                    )
                }
            )

            // The circle sits on the frame, not on the letter: a private
            // number keeps the same coloured disc with a person glyph in
            // it instead of a letter the phone never had.
            avatarFrame.background = AvatarDrawable.circle(avatarKey(call))
            // A number letters its last two digits, which is how a person
            // recognises an unknown caller among other unknown callers; a
            // private number has nothing to letter and gets the glyph.
            if (label.isNotEmpty()) {
                avatar.visibility = View.VISIBLE
                avatarIcon.visibility = View.GONE
                avatar.text = AvatarDrawable.initials(label)
                avatar.contentDescription = context.getString(R.string.messages_avatar_of, title)
            } else {
                avatar.visibility = View.GONE
                avatarIcon.visibility = View.VISIBLE
            }

            typeIcon.setImageResource(iconOf(call.kind))
            typeIcon.setColorFilter(
                if (call.kind == DialerRepository.CallKind.MISSED) {
                    AvatarDrawable.themeColor(
                        context,
                        MaterialR.attr.colorError,
                        0xFFB3261E.toInt()
                    )
                } else {
                    AvatarDrawable.themeColor(
                        context,
                        MaterialR.attr.colorOnSurfaceVariant,
                        0xFF49454F.toInt()
                    )
                }
            )

            // The second line: the kind of call, the number when the name
            // already took the first line, the duration and the SIM.
            val typeLabel = context.getString(typeLabelOf(call.kind))
            meta.text = DialerFormat.meta(
                listOf(
                    typeLabel,
                    if (known.isNotEmpty()) displayNumber(call.number) else null,
                    if (DialerFormat.hasDuration(call.kind, call.durationSeconds)) {
                        DialerFormat.duration(call.durationSeconds)
                    } else {
                        null
                    },
                    call.simLabel
                )
            )
            date.text = MessageDates.rowLabel(context, call.dateMillis)

            // One sentence for the whole row: a screen reader does not
            // walk four labels, and the kind of call is part of it.
            itemView.contentDescription = DialerFormat.meta(
                listOf(typeLabel, title, date.text.toString(), call.simLabel)
            )
            itemView.setOnClickListener { onClick(call) }
            itemView.setOnLongClickListener {
                onLongClick(call)
                true
            }
        }

        /** The stable key of the avatar: the number, or the name if none. */
        private fun avatarKey(call: DialerRepository.RecentCall): String =
            call.number.ifEmpty { call.cachedName.orEmpty() }

        private fun displayNumber(raw: String): String =
            SmsRepository.displayNumber(raw)

        private fun iconOf(kind: DialerRepository.CallKind): Int = when (kind) {
            DialerRepository.CallKind.OUTGOING -> R.drawable.ic_call_made
            DialerRepository.CallKind.MISSED,
            DialerRepository.CallKind.REJECTED,
            DialerRepository.CallKind.BLOCKED -> R.drawable.ic_call_missed
            DialerRepository.CallKind.VOICEMAIL -> R.drawable.ic_voicemail
            DialerRepository.CallKind.INCOMING,
            DialerRepository.CallKind.OTHER -> R.drawable.ic_call_received
        }

        private fun typeLabelOf(kind: DialerRepository.CallKind): Int = when (kind) {
            DialerRepository.CallKind.INCOMING -> R.string.dialer_call_type_incoming
            DialerRepository.CallKind.OUTGOING -> R.string.dialer_call_type_outgoing
            DialerRepository.CallKind.MISSED -> R.string.dialer_call_type_missed
            DialerRepository.CallKind.REJECTED -> R.string.dialer_call_type_rejected
            DialerRepository.CallKind.BLOCKED -> R.string.dialer_call_type_blocked
            DialerRepository.CallKind.VOICEMAIL -> R.string.dialer_call_type_voicemail
            DialerRepository.CallKind.OTHER -> R.string.dialer_call_type_other
        }
    }

    private companion object {
        val DIFF = object : DiffUtil.ItemCallback<DialerRepository.RecentCall>() {
            override fun areItemsTheSame(
                oldItem: DialerRepository.RecentCall,
                newItem: DialerRepository.RecentCall
            ): Boolean = oldItem.id == newItem.id

            override fun areContentsTheSame(
                oldItem: DialerRepository.RecentCall,
                newItem: DialerRepository.RecentCall
            ): Boolean = oldItem == newItem
        }
    }
}
