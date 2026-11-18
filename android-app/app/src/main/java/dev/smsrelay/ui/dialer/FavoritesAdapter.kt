package dev.smsrelay.ui.dialer

import android.content.Context
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.DiffUtil
import androidx.recyclerview.widget.ListAdapter
import androidx.recyclerview.widget.RecyclerView
import dev.smsrelay.R
import dev.smsrelay.messages.SmsRepository
import dev.smsrelay.ui.messages.AvatarDrawable

/**
 * The Favorites tab: the starred contacts of the phone.
 *
 * A favorite is one number of one contact ([DialerRepository] keeps the
 * first), so the row is a name, that number and the star that explains
 * why it is here. Tapping dials; a long press offers the same sheet as
 * the Contacts tab.
 */
class FavoritesAdapter(
    private val onClick: (DialerRepository.FavoriteContact) -> Unit,
    private val onLongClick: (DialerRepository.FavoriteContact) -> Unit
) : ListAdapter<DialerRepository.FavoriteContact, FavoritesAdapter.Holder>(DIFF) {

    init {
        setHasStableIds(true)
    }

    override fun getItemId(position: Int): Long = getItem(position).contactId

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
        val view = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_dialer_favorite, parent, false)
        return Holder(view, onClick, onLongClick)
    }

    override fun onBindViewHolder(holder: Holder, position: Int) {
        holder.bind(getItem(position))
    }

    class Holder(
        view: View,
        private val onClick: (DialerRepository.FavoriteContact) -> Unit,
        private val onLongClick: (DialerRepository.FavoriteContact) -> Unit
    ) : RecyclerView.ViewHolder(view) {

        private val context: Context = view.context
        private val avatar: TextView = view.findViewById(R.id.textFavoriteAvatar)
        private val name: TextView = view.findViewById(R.id.textFavoriteName)
        private val number: TextView = view.findViewById(R.id.textFavoriteNumber)

        fun bind(favorite: DialerRepository.FavoriteContact) {
            avatar.text = AvatarDrawable.initials(favorite.name)
            avatar.background = AvatarDrawable.circle(favorite.name)
            avatar.contentDescription = context.getString(
                R.string.messages_avatar_of,
                favorite.name
            )
            name.text = favorite.name
            number.text = SmsRepository.displayNumber(favorite.number)
            itemView.contentDescription = DialerFormat.meta(
                listOf(favorite.name, number.text.toString())
            )
            itemView.setOnClickListener { onClick(favorite) }
            itemView.setOnLongClickListener {
                onLongClick(favorite)
                true
            }
        }
    }

    private companion object {
        val DIFF = object : DiffUtil.ItemCallback<DialerRepository.FavoriteContact>() {
            override fun areItemsTheSame(
                oldItem: DialerRepository.FavoriteContact,
                newItem: DialerRepository.FavoriteContact
            ): Boolean = oldItem.contactId == newItem.contactId

            override fun areContentsTheSame(
                oldItem: DialerRepository.FavoriteContact,
                newItem: DialerRepository.FavoriteContact
            ): Boolean = oldItem == newItem
        }
    }
}
