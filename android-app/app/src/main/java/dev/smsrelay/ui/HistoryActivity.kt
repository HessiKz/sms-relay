package dev.smsrelay.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import dev.smsrelay.R
import dev.smsrelay.data.AppDatabase
import dev.smsrelay.data.MessageRecord
import dev.smsrelay.sms.SimCards
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

class HistoryActivity : AppCompatActivity() {

    private lateinit var adapter: Adapter
    private lateinit var list: RecyclerView
    private lateinit var empty: TextView
    private val pageSize = 50
    private var offset = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_history)
        list = findViewById<RecyclerView>(R.id.listHistory)
        empty = findViewById<TextView>(R.id.textEmpty)
        list.layoutManager = LinearLayoutManager(this)
        adapter = Adapter()
        list.adapter = adapter
        list.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(recyclerView: RecyclerView, dx: Int, dy: Int) {
                val manager = recyclerView.layoutManager as LinearLayoutManager
                if (manager.findLastVisibleItemPosition() >= adapter.itemCount - 5) {
                    loadNextPage()
                }
            }
        })

        loadNextPage()
    }

    private var loading = false

    private fun loadNextPage() {
        if (loading) return
        loading = true
        Thread {
            val dao = AppDatabase.get(this).messageDao()
            val items = dao.historyPage(pageSize, offset)
            val newOffset = offset + items.size
            // The names of the cards are read once for the whole page: the
            // list of the cards of the phone does not change while the
            // history is on the screen.
            val labels = SimCards.active(this).associate {
                it.subscriptionId to SimCards.label(this, it)
            }
            runOnUiThread {
                adapter.append(items, labels)
                empty.visibility = if (adapter.itemCount == 0) View.VISIBLE else View.GONE
                offset = newOffset
                loading = false
            }
        }.start()
    }

    private inner class Adapter : RecyclerView.Adapter<Adapter.Holder>() {
        private val items = mutableListOf<MessageRecord>()
        private var cardLabels: Map<Int, String> = emptyMap()

        fun submit(newItems: List<MessageRecord>) {
            items.clear()
            items.addAll(newItems)
            notifyDataSetChanged()
        }

        fun append(newItems: List<MessageRecord>, labels: Map<Int, String>) {
            cardLabels = labels
            if (newItems.isEmpty()) return
            val start = items.size
            items.addAll(newItems)
            notifyItemRangeInserted(start, newItems.size)
        }

        override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): Holder {
            val view = LayoutInflater.from(parent.context)
                .inflate(R.layout.item_history, parent, false)
            return Holder(view)
        }

        override fun getItemCount(): Int = items.size

        override fun onBindViewHolder(holder: Holder, position: Int) {
            holder.bind(items[position])
        }

        inner class Holder(view: View) : RecyclerView.ViewHolder(view) {
            private val sender: TextView = view.findViewById(R.id.textSender)
            private val body: TextView = view.findViewById(R.id.textBody)
            private val meta: TextView = view.findViewById(R.id.textMeta)

            fun bind(record: MessageRecord) {
                sender.text = record.sender
                body.text = record.body
                val stamp = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)
                    .format(Date(record.receivedAt * 1000L))
                // The card the message came in on is a quiet note next to
                // the time: it is missing when the card could not be found.
                val card = cardLabels[record.subId]
                val viaCard = card?.let {
                    " · " + itemView.context.getString(R.string.history_via_sim, it)
                } ?: ""
                meta.text = "$stamp$viaCard · ${record.uploadState}" +
                    (record.fallbackState.takeIf { it != MessageRecord.FALLBACK_NONE }
                        ?.let { " · fallback:$it" } ?: "") +
                    (record.lastError?.let { " · $it" } ?: "")
            }
        }
    }
}
