package dev.pitekusu.shittim.records

internal enum class InsightsTab { Affection, Rankings, Costs }
internal enum class InsightsAffectionView { Participants, Requesters }

internal data class InsightsResource<T>(
  val data: T? = null,
  val loading: Boolean = false,
  val error: InsightsFailure? = null,
  val saved: Boolean = false,
  val revision: Long = 0L,
)

/** Decrypted data belongs to the authorized runtime; only display choices may be saveable. */
internal data class InsightsUiState(
  val tab: InsightsTab = InsightsTab.Affection,
  val view: InsightsAffectionView = InsightsAffectionView.Participants,
  val participant: String = "participant-a",
  val period: InsightsPeriod = InsightsPeriod.Week,
  val requesterPage: Int = 0,
  val online: Boolean = false,
  val rankings: InsightsResource<InsightsRankings> = InsightsResource(),
  val participants: InsightsResource<List<InsightsAffectionPage>> = InsightsResource(),
  val requesters: InsightsResource<List<InsightsRequesterPage>> = InsightsResource(),
  val costs: Map<InsightsPeriod, InsightsResource<InsightsCosts>> = emptyMap(),
)

internal sealed interface InsightsEvent {
  data class Tab(val value: InsightsTab) : InsightsEvent
  data class View(val value: InsightsAffectionView) : InsightsEvent
  data class Participant(val value: String) : InsightsEvent
  data class Period(val value: InsightsPeriod) : InsightsEvent
  data object PreviousRequester : InsightsEvent
  data object NextRequester : InsightsEvent
  data object MoreParticipants : InsightsEvent
  data object Refresh : InsightsEvent
  data object Back : InsightsEvent
}

/** Finite choices survive a fresh verification without retaining the old permit's decrypted data. */
internal data class InsightsSelection(
  val tab: InsightsTab = InsightsTab.Affection,
  val view: InsightsAffectionView = InsightsAffectionView.Participants,
  val participant: String = "participant-a",
  val period: InsightsPeriod = InsightsPeriod.Week,
  val requesterPage: Int = 0,
)
internal fun InsightsUiState.selection() = InsightsSelection(tab, view, participant, period, requesterPage)
