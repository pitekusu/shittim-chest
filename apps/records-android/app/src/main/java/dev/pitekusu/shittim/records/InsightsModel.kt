package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Screen-lifetime requests use the existing session and encrypted store through typed operations. */
internal class InsightsModel(
  private val active: () -> Boolean,
  private val cachedRankings: suspend () -> InsightsRankings?,
  private val cachedParticipants: suspend () -> List<InsightsAffectionPage>,
  private val cachedRequesters: suspend () -> List<InsightsRequesterPage>,
  private val cachedCosts: suspend (InsightsPeriod) -> InsightsCosts?,
  private val refreshRankings: suspend () -> InsightsRankings,
  private val refreshParticipants: suspend () -> List<InsightsAffectionPage>,
  private val refreshRequesters: suspend () -> List<InsightsRequesterPage>,
  private val refreshCosts: suspend (InsightsPeriod) -> InsightsCosts,
  private val appendParticipants: suspend (String) -> List<InsightsAffectionPage>,
  private val appendRequesters: suspend (String) -> List<InsightsRequesterPage>,
  private val authenticationRequired: () -> Unit = {},
  private val close: () -> Unit = {},
  private val warmRankings: suspend () -> InsightsRankings? = { null },
  private val warmParticipants: suspend () -> List<InsightsAffectionPage>? = { null },
  private val warmRequesters: suspend () -> List<InsightsRequesterPage>? = { null },
  initial: InsightsSelection = InsightsSelection(),
) : ViewModel() {
  private val mutableState = MutableStateFlow(InsightsUiState(tab = initial.tab, view = initial.view,
    participant = initial.participant, period = initial.period, requesterPage = initial.requesterPage))
  val state = mutableState.asStateFlow()
  private val jobs = mutableMapOf<String, Job>()
  private val visited = mutableSetOf<String>()
  private val requests = mutableMapOf<String, Long>()
  private var epoch = 0L

  fun beginVisit(online: Boolean) {
    visited.clear()
    open(online)
  }

  fun open(online: Boolean) {
    if (!active()) return lock()
    val reconnecting = online && !state.value.online
    mutableState.update { it.copy(online = online) }
    if (reconnecting) visited.clear()
    ensureCurrent()
  }

  fun lock() {
    epoch++
    jobs.values.forEach(Job::cancel)
    jobs.clear()
    visited.clear()
    mutableState.value = InsightsUiState()
  }

  fun event(event: InsightsEvent) {
    if (!active()) return lock()
    when (event) {
      is InsightsEvent.Tab -> mutableState.update { it.copy(tab = event.value) }
      is InsightsEvent.View -> mutableState.update { it.copy(view = event.value) }
      is InsightsEvent.Participant -> if (event.value in INSIGHTS_PARTICIPANTS)
        mutableState.update { it.copy(participant = event.value) }
      is InsightsEvent.Period -> mutableState.update { it.copy(period = event.value) }
      InsightsEvent.PreviousRequester -> mutableState.update {
        it.copy(requesterPage = (it.requesterPage - 1).coerceAtLeast(0))
      }
      InsightsEvent.NextRequester -> {
        val value = state.value
        if (value.requesterPage + 1 < value.requesters.data.orEmpty().size) {
          mutableState.update { it.copy(requesterPage = it.requesterPage + 1) }
        } else loadRequesters(more = true)
      }
      InsightsEvent.MoreParticipants -> loadParticipants(more = true)
      InsightsEvent.Refresh -> {
        if (state.value.online) loadCurrent(refresh = true)
        return
      }
      InsightsEvent.Back -> return
    }
    ensureCurrent()
  }

  private fun key(): String = when (state.value.tab) {
    InsightsTab.Rankings -> "rankings"
    InsightsTab.Affection -> if (state.value.view == InsightsAffectionView.Participants) "participants" else "requesters"
    InsightsTab.Costs -> "costs-${state.value.period.name}"
  }

  private fun ensureCurrent() {
    val key = key()
    if (visited.add(key)) loadCurrent(refresh = false)
  }

  private fun loadCurrent(refresh: Boolean) {
    when (state.value.tab) {
      InsightsTab.Rankings -> loadRankings(refresh)
      InsightsTab.Affection -> if (state.value.view == InsightsAffectionView.Participants)
        loadParticipants(refresh = refresh) else loadRequesters(refresh = refresh)
      InsightsTab.Costs -> loadCosts(state.value.period, refresh)
    }
  }

  private fun <T> load(
    key: String,
    refresh: Boolean,
    more: Boolean = false,
    current: () -> InsightsResource<T>,
    update: (InsightsResource<T>) -> Unit,
    cached: suspend () -> T?,
    remote: suspend () -> T,
    afterSuccess: () -> Unit = {},
    warm: suspend () -> T? = { null },
  ) {
    if (more && (!state.value.online || current().loading)) return
    if (jobs[key]?.isActive == true && !refresh && current().loading) return
    jobs[key]?.cancel()
    val requestEpoch = epoch
    val requestId = (requests[key] ?: 0L) + 1L
    requests[key] = requestId
    fun permitted() = active() && epoch == requestEpoch && requests[key] == requestId
    jobs[key] = viewModelScope.launch {
      if (!permitted()) return@launch
      update(current().copy(loading = true, error = null))
      try {
        if (!refresh && !more && current().data == null) {
          val value = cached()
          if (!permitted()) return@launch
          if (value != null) update(current().copy(data = value, saved = true))
        }
        if (!state.value.online) {
          if (permitted()) update(current().copy(loading = false, saved = current().data != null))
          return@launch
        }
        val value = remote()
        if (!permitted()) return@launch
        update(InsightsResource(data = value, saved = false,
          revision = current().revision + if (refresh) 1L else 0L))
        afterSuccess()
        // Optional avatars never delay the numeric result or replace it with an image error.
        val warmed = try { warm() }
        catch (error: CancellationException) { throw error }
        catch (_: Exception) { null }
        if (permitted() && warmed != null) update(current().copy(data = warmed))
      } catch (error: CancellationException) { throw error }
      catch (error: InsightsException) {
        if (!permitted()) return@launch
        update(current().copy(loading = false, error = error.failure))
        if (error.failure == InsightsFailure.AUTH_REQUIRED) { lock(); authenticationRequired() }
      } catch (_: Exception) {
        if (permitted()) update(current().copy(loading = false, error = InsightsFailure.UNAVAILABLE))
      }
    }
  }

  private fun loadRankings(refresh: Boolean) = load("rankings", refresh,
    current = { state.value.rankings }, update = { value -> mutableState.update { it.copy(rankings = value) } },
    cached = cachedRankings, remote = refreshRankings, warm = warmRankings)

  private fun loadParticipants(refresh: Boolean = false, more: Boolean = false) {
    val cursor = state.value.participants.data?.lastOrNull()?.nextCursor
    if (more && cursor == null) return
    load("participants", refresh, more,
      current = { state.value.participants }, update = { value -> mutableState.update { it.copy(participants = value) } },
      cached = { cachedParticipants().takeIf { it.isNotEmpty() } },
      remote = { if (more) appendParticipants(requireNotNull(cursor)) else refreshParticipants() }, warm = warmParticipants)
  }

  private fun loadRequesters(refresh: Boolean = false, more: Boolean = false) {
    val cursor = state.value.requesters.data?.lastOrNull()?.nextCursor
    if (more && cursor == null) return
    var previousGeneration: String? = null
    load("requesters", refresh, more,
      current = { state.value.requesters }, update = { value -> mutableState.update { it.copy(requesters = value) } },
      cached = { cachedRequesters().takeIf { it.isNotEmpty() } },
      remote = {
        previousGeneration = state.value.requesters.data?.firstOrNull()?.generatedAt
        if (more) appendRequesters(requireNotNull(cursor)) else refreshRequesters()
      },
      warm = warmRequesters,
      afterSuccess = {
        mutableState.update { it.copy(requesterPage = if (more)
          (it.requesterPage + 1).coerceAtMost(it.requesters.data.orEmpty().lastIndex.coerceAtLeast(0))
          else if (refresh || previousGeneration != it.requesters.data?.firstOrNull()?.generatedAt) 0
          else it.requesterPage.coerceAtMost(it.requesters.data.orEmpty().lastIndex.coerceAtLeast(0))) }
      })
  }

  private fun loadCosts(period: InsightsPeriod, refresh: Boolean) = load("costs-${period.name}", refresh,
    current = { state.value.costs[period] ?: InsightsResource() },
    update = { value -> mutableState.update { it.copy(costs = it.costs + (period to value)) } },
    cached = { cachedCosts(period) }, remote = { refreshCosts(period) })

  fun endSession() { lock(); close() }

  override fun onCleared() { endSession() }
}

