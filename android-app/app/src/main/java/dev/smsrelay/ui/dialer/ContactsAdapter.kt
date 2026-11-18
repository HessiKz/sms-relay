package dev.smsrelay.ui.dialer

import android.content.Context
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

/**
 * The Contacts tab: one row per contact number.
 *
 * The list is already deduplicated and sorted by [DialerRepository], so
 * this adapter only paints. A starred contact gets the small star at the
 * end of the name line; the star is a marker, so the row still dials.
 */
class ContactsAdapter(
    private val onClick: (DialerRepository.DialerContact) -> Unit,
    private val onLongClick: (DialerRepository.DialerContact) -> Unit
) : ListAdapter<DialerRepository.DialerContact, ContactsAdapter.Holder>(DIFF) {

    init {
        setHasStableIds(true)
    }

    /**
     * The number is the identity: the repository already collapsed two
     * spellings of one number, so two rows can never share it.
     */
    override fun getItemId(position: Int): Long {
        val item = getItem(position)
        return (item.contactId * 31 + item.number.hashCode()).toLong()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_dialer_contact, parent, false)
        return Holder(view, onClick, onLongClick)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.bind(getItem(position))
    }

    class Holder(
        view: View,
        private val onClick: (DialerRepository.DialerContact) -> Unit,
        private val onLongClick: (DialerRepository.DialerContact) -> Unit
    ) : RecyclerView.ViewHolder(view) {

        private val context: Context = view.context
        private val avatar: TextView = view.findViewById(R.id.textContactAvatar)
        private val name: TextView = view.findViewById(R.id.textContactName)
        private val number: TextView = view.findViewById(R.id.textContactNumber)
        private val star: ImageView = view.findViewById(R.id.imageContactStar)

        fun bind(contact: DialerRepository.DialerContact) {
            avatar.text = AvatarDrawable.initials(contact.name)
            avatar.background = AvatarDrawable.circle(contact.name)
            avatar.contentDescription = context.getString(
                R.string.messages_avatar_of,
                contact.name
            )

            name.text = contact.name
            number.text = SmsRepository.displayNumber(contact.number)
            star.visibility = if (contact.starred) View.VISIBLE else View.GONE

            // The star has its own description only when it is drawn: an
            // invisible view is not read, so the row sentence is honest
            // either way.
            itemView.contentDescription = DialerFormat.meta(
                listOf(contact.name, number.text.toString())
            )
            itemView.setOnClickListener { onClick(contact) }
            itemView.setOnLongClickListener {
                onLongClick(contact)
                true
            }
        }
    }

    private companion object {
        val DIFF = object : DiffUtil.ItemCallback<DialerRepository.DialerContact>() {
            override fun areItemsTheSame(
                oldItem: DialerRepository.DialerContact,
                newItem: DialerRepository.DialerContact
            ): Boolean =
                oldItem.contactId == newItem.contactId && oldItem.number == newItem.number

            override fun areContentsTheSame(
                oldItem: DialerRepository.DialerContact,
                newItem: DialerRepository.DialerContact
            ): Boolean = oldItem == newItem
        }
    }
}
