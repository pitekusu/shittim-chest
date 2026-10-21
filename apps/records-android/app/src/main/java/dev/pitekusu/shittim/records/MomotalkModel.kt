package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/** Only the current verified account may publish a request or decrypt a saved conversation. */
internal class MomotalkModel(
  private val active: () -> Boolean,
  private val cachedWeeks: suspend () -> List<MomotalkWeeksPage>,
  private val refreshWeeks: suspend () -> List<MomotalkWeeksPage>,
  private val appendWeeks: suspend (String) -> List<MomotalkWeeksPage>,
  private val cachedRooms: suspend (String) -> List<MomotalkRoomsPage>,
  private val refreshRooms: suspend (String) -> List<MomotalkRoomsPage>,
  private val appendRooms: suspend (String, String) -> List<MomotalkRoomsPage>,
  private val cachedRoom: suspend (String, String) -> MomotalkRoomDetail?,
  private val refreshRoom: suspend (String, String) -> MomotalkRoomDetail,
  private val warmRooms: suspend (String) -> List<MomotalkRoomsPage>? = { null },
  private val warmRoom: suspend (String, String) -> MomotalkRoomDetail? = { _, _ -> null },
  private val authenticationRequired: () -> Unit = {},
  private val close: () -> Unit = {},
  initial: MomotalkSelection = MomotalkSelection(),
) : ViewModel() {
  private val mutableState = MutableStateFlow(MomotalkUiState(selectedWeek = initial.selectedWeek))
  val state = mutableState.asStateFlow()
  private val jobs = mutableMapOf<String, Job>()
  private val visited = mutableSetOf<String>()
  private var epoch = 0L
  private var requestId = 0L
  private val requests = mutableMapOf<String, Long>()

  fun open(online: Boolean, week: String? = null, room: String? = null) {
    if (!active()) return lock()
    if (online && !state.value.online) visited.clear()
    if (week != null && week != state.value.selectedWeek) selectWeek(week)
    selectRoom(room)
    mutableState.update { it.copy(online = online) }
    ensureCurrent()
  }

  fun event(event: MomotalkEvent) {
    if (!active()) return lock()
    when (event) {
      is MomotalkEvent.SelectWeek -> {
        if (state.value.weeks.data.orEmpty().flatMap { it.weeks }.none { it.weekId == event.value }) return
        selectWeek(event.value)
        ensureCurrent()
      }
      is MomotalkEvent.SelectRoom -> {
        if (state.value.rooms.data.orEmpty().flatMap { it.rooms }.none { it.roomId == event.value && it.state == "ready" }) return
        selectRoom(event.value)
        ensureCurrent()
      }
      MomotalkEvent.MoreWeeks -> loadWeeks(more = true)
      MomotalkEvent.MoreRooms -> state.value.selectedWeek?.let { loadRooms(it, more = true) }
      MomotalkEvent.Refresh -> if (state.value.online) {
        val value = state.value
        when {
          value.selectedRoom != null && value.selectedWeek != null -> loadRoom(value.selectedWeek, value.selectedRoom, refresh = true)
          value.selectedWeek != null -> { loadWeeks(refresh = true); loadRooms(value.selectedWeek, refresh = true) }
          else -> loadWeeks(refresh = true)
        }
      }
      MomotalkEvent.Back -> selectRoom(null)
    }
  }

  private fun selectWeek(week: String) {
    if (week == state.value.selectedWeek) return
    jobs.remove("rooms")?.cancel()
    jobs.remove("detail")?.cancel()
    requests.remove("rooms"); requests.remove("detail")
    mutableState.update { it.copy(selectedWeek = week, selectedRoom = null,
      rooms = MomotalkResource(), detail = MomotalkResource()) }
  }

  private fun selectRoom(room: String?) {
    if (room == state.value.selectedRoom) return
    jobs.remove("detail")?.cancel()
    requests.remove("detail")
    if (room != null) visited.remove("room-${state.value.selectedWeek}-$room")
    mutableState.update { it.copy(selectedRoom = room,
      detail = if (room == null || it.detail.data?.room?.roomId == room) it.detail else MomotalkResource()) }
  }

  private fun ensureCurrent() {
    if (visited.add("weeks")) loadWeeks()
    val value = state.value
    val week = value.selectedWeek ?: return
    if (value.selectedRoom != null) {
      if (visited.add("room-$week-${value.selectedRoom}") || value.detail.data?.room?.roomId != value.selectedRoom)
        loadRoom(week, value.selectedRoom)
    } else if (visited.add("rooms-$week") || value.rooms.data == null) loadRooms(week)
  }

  private fun <T> load(key: String, refresh: Boolean, more: Boolean = false,
    current: () -> MomotalkResource<T>, update: (MomotalkResource<T>) -> Unit,
    cached: suspend () -> T?, remote: suspend () -> T,
    selected: () -> Boolean = { true }, after: () -> Unit = {}, warm: suspend () -> T? = { null }) {
    if (more && (!state.value.online || current().loading)) return
    if (jobs[key]?.isActive == true && !refresh) return
    jobs[key]?.cancel()
    val expectedEpoch = epoch
    val expectedRequest = ++requestId
    requests[key] = expectedRequest
    fun permitted() = active() && epoch == expectedEpoch && requests[key] == expectedRequest && selected()
    jobs[key] = viewModelScope.launch {
      if (!permitted()) return@launch
      update(current().copy(loading = true, error = null))
      try {
        if (!refresh && !more && current().data == null) {
          val value = cached()
          if (!permitted()) return@launch
          if (value != null) { update(current().copy(data = value, saved = true)); after() }
        }
        if (!state.value.online) {
          if (permitted()) update(current().copy(loading = false, saved = current().data != null))
          return@launch
        }
        val value = remote()
        if (!permitted()) return@launch
        update(MomotalkResource(data = value, revision = current().revision + if (refresh) 1L else 0L))
        after()
        // Optional media must never hold back a readable conversation.
        val warmed = try { warm() } catch (error: CancellationException) { throw error } catch (_: Exception) { null }
        if (permitted() && warmed != null) update(current().copy(data = warmed))
      } catch (error: CancellationException) { throw error }
      catch (error: MomotalkException) {
        if (!permitted()) return@launch
        if (error.failure == MomotalkFailure.AUTH_REQUIRED) { lock(); authenticationRequired() }
        else update(current().copy(loading = false, error = error.failure))
      } catch (_: Exception) {
        if (permitted()) update(current().copy(loading = false, error = MomotalkFailure.UNAVAILABLE))
      }
    }
  }

  private fun loadWeeks(refresh: Boolean = false, more: Boolean = false) {
    val cursor = state.value.weeks.data?.lastOrNull()?.nextCursor
    if (more && cursor == null) return
    load("weeks", refresh, more, current = { state.value.weeks },
      update = { value -> mutableState.update { it.copy(weeks = value) } },
      cached = { cachedWeeks().takeIf { it.isNotEmpty() } },
      remote = { if (more) appendWeeks(requireNotNull(cursor)) else refreshWeeks() },
      after = {
        if (state.value.selectedWeek == null) {
          state.value.weeks.data?.firstOrNull()?.weeks?.firstOrNull()?.weekId?.let {
            selectWeek(it); ensureCurrent()
          }
        }
      })
  }

  private fun loadRooms(week: String, refresh: Boolean = false, more: Boolean = false) {
    val cursor = state.value.rooms.data?.lastOrNull()?.nextCursor
    if (more && cursor == null) return
    load("rooms", refresh, more, current = { state.value.rooms },
      update = { value -> mutableState.update { it.copy(rooms = value) } },
      cached = { cachedRooms(week).takeIf { it.isNotEmpty() } },
      remote = { if (more) appendRooms(week, requireNotNull(cursor)) else refreshRooms(week) },
      selected = { state.value.selectedWeek == week }, warm = { warmRooms(week) })
  }

  private fun loadRoom(week: String, room: String, refresh: Boolean = false) = load("detail", refresh,
    current = { state.value.detail }, update = { value -> mutableState.update { it.copy(detail = value) } },
    cached = { cachedRoom(week, room) }, remote = { refreshRoom(week, room) },
    selected = { state.value.selectedWeek == week && state.value.selectedRoom == room },
    warm = { warmRoom(week, room) })

  fun lock() {
    epoch++
    jobs.values.forEach(Job::cancel); jobs.clear(); requests.clear(); visited.clear()
    mutableState.value = MomotalkUiState()
  }
  fun endSession() { lock(); close() }
  override fun onCleared() { endSession() }
}
