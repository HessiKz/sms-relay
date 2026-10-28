package dev.smsrelay.sms

import android.content.Context
import android.telephony.SubscriptionManager
import dev.smsrelay.R

/**
 * The SIM cards of the phone.
 *
 * One place knows what a SIM is: which cards are active, in which slot
 * they sit and how the user reads their name. The sender, the picker in
 * the app and the "via SIM 1" note in the history all ask this list, so
 * they can never disagree about which card is which.
 *
 * The list is read from the system every time it is needed: the user can
 * take a card out or put one in while the app runs, and a card that is
 * gone must not stay in the settings as if it were still there.
 */
object SimCards {

    /** One active SIM card of the phone. */
    data class Card(
        /** The id the system gives to this card. */
        val subscriptionId: Int,
        /** 0 is the first slot (SIM 1), 1 the second (SIM 2). */
        val slotIndex: Int,
        /** The name the user gave the card; not a secret. */
        val displayName: String? = null
    )

    /**
     * The active cards of the phone, the one in the first slot first.
     */
    fun active(context: Context): List<Card> {
        val manager = context.getSystemService(SubscriptionManager::class.java)
            ?: return emptyList()
        val subscriptions = runCatching {
            manager.activeSubscriptionInfoList.orEmpty()
        }.getOrDefault(emptyList())
        return subscriptions
            .map {
                Card(
                    subscriptionId = it.subscriptionId,
                    slotIndex = it.simSlotIndex,
                    displayName = it.displayName?.toString()
                        ?.trim()
                        ?.takeIf { name -> name.isNotEmpty() }
                )
            }
            .sortedWith(compareBy({ it.slotIndex }, { it.subscriptionId }))
    }

    /** The card with this subscription id, or null when it is not active. */
    fun find(context: Context, subscriptionId: Int): Card? =
        active(context).firstOrNull { it.subscriptionId == subscriptionId }

    /** "SIM 1", "SIM 2" ... - the name the user knows the card by. */
    fun label(context: Context, card: Card): String =
        context.getString(R.string.sim_label, card.slotIndex + 1)
}
