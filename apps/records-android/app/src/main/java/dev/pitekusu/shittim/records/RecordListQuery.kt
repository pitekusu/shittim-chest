package dev.pitekusu.shittim.records

import java.text.Normalizer
import java.util.Locale

internal enum class RecordWinner(val nameInRecord: String?) {
  All(null), Arona("アロナ"), Plana("プラナ"), Abe("安倍晋三AI"),
}

internal enum class RecordOrder { Newest, Oldest }

/** Screen-lifetime criteria only: never serialized into SavedState, Room, or worker input. */
internal data class RecordListQuery(
  val text: String = "",
  val winner: RecordWinner = RecordWinner.All,
  val order: RecordOrder = RecordOrder.Newest,
) {
  private val terms = normalized(text).split(Regex("\\s+")).filter(String::isNotEmpty)
  val searchesText: Boolean get() = terms.isNotEmpty()
  val isDefault: Boolean get() = !searchesText && winner == RecordWinner.All && order == RecordOrder.Newest

  fun acceptsWinner(entry: RecordListEntry): Boolean =
    winner.nameInRecord?.let { it == entry.winnerName } ?: true

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
