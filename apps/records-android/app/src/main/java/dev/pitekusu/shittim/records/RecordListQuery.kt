package dev.pitekusu.shittim.records

import java.text.Normalizer
import java.util.Locale

internal enum class RecordWinner(val nameInRecord: String?, val slot: String?) {
  All(null, null), Arona("アロナ", "participant-a"), Plana("プラナ", "participant-b"),
  Abe("安倍晋三AI", "participant-c"),
}

internal enum class RecordOrder { Newest, Oldest }

internal class RecordRequesterChoice(val displayName: String, val avatar: RecordAvatar)

/** Use all saved metadata, not the currently filtered or paged results. */
internal fun recordRequesterChoices(entries: List<RecordListEntry>): List<RecordRequesterChoice> =
  entries.groupBy { it.requesterName }.map { (name, records) ->
    val newest = records.sortedWith(compareByDescending<RecordListEntry> { it.completedAt }.thenBy { it.recordId })
    // An unfinished icon download must not replace an already saved Discord icon.
    val avatar = newest.firstOrNull { it.requesterAvatar.bytes != null }?.requesterAvatar
      ?: newest.first().requesterAvatar
    RecordRequesterChoice(name, avatar)
  }.sortedBy { it.displayName }

/** Screen-lifetime criteria only: never serialized into SavedState, Room, or worker input. */
internal data class RecordListQuery(
  val text: String = "",
  val winner: RecordWinner = RecordWinner.All,
  val order: RecordOrder = RecordOrder.Newest,
  val requesterName: String? = null,
) {
  private val terms = normalized(text).split(Regex("\\s+")).filter(String::isNotEmpty)
  val searchesText: Boolean get() = terms.isNotEmpty()
  val isDefault: Boolean get() = !searchesText && winner == RecordWinner.All &&
    order == RecordOrder.Newest && requesterName == null

  // The read contract exposes display names, not requester IDs. Match the saved name exactly;
  // do not guess identity from an icon or expose an internal Discord identifier.
  fun acceptsRequester(entry: RecordListEntry): Boolean =
    requesterName == null || requesterName == entry.requesterName

  fun acceptsWinner(entry: RecordListEntry): Boolean = when {
    winner == RecordWinner.All -> true
    entry.winnerSlot != null -> winner.slot == entry.winnerSlot
    else -> winner.nameInRecord == entry.winnerName
  }

  fun matches(entry: RecordListEntry, detail: RecordPreview? = null): Boolean {
    // This temporary text is discarded after comparing one record; there is no plaintext index.
    val content = normalized(buildString {
      appendLine(entry.questionPreview)
      appendLine(entry.requesterName)
      appendLine(entry.winnerName)
      detail?.let {
        appendLine(it.question)
        appendLine(it.decision)
        it.opinions.forEach { opinion ->
          appendLine(opinion.participantName)
          appendLine(opinion.summary)
          appendLine(opinion.initialProposal)
          appendLine(opinion.finalTitle)
          appendLine(opinion.finalProposal)
        }
        it.voting?.votes?.forEach { vote ->
          appendLine(vote.reason)
          vote.assessments?.forEach { assessment -> appendLine(assessment.reason) }
        }
        it.victoryMessage?.let(::appendLine)
        it.actions.forEach(::appendLine)
        it.caveats.forEach(::appendLine)
      }
    })
    return terms.all(content::contains)
  }

  fun sorted(entries: List<RecordListEntry>): List<RecordListEntry> {
    val byDate = if (order == RecordOrder.Newest) compareByDescending<RecordListEntry> { it.completedAt }
      else compareBy { it.completedAt }
    return entries.sortedWith(byDate.thenBy { it.recordId })
  }

  private fun normalized(value: String): String =
    Normalizer.normalize(value, Normalizer.Form.NFKC).lowercase(Locale.ROOT)
}
