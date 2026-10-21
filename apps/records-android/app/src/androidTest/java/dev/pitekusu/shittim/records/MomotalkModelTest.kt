package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MomotalkModelTest {
  private val week = MomotalkWeek("2026-10-18", "2026-10-11T18:00:00+09:00",
    "2026-10-18T18:00:00+09:00", "2026-10-18T19:00:00+09:00")
  private val older = week.copy(weekId = "2026-10-11")
  private val room = MomotalkRoomSummary("r".repeat(43),
    MomotalkRequester("架空の依頼者", RecordAvatar(null, "cyan")), 3, "ready")
  private val weeks = listOf(MomotalkWeeksPage(weeks = listOf(week, older)))
  private fun rooms(value: MomotalkWeek = week) = listOf(MomotalkRoomsPage(week = value, rooms = listOf(room)))
  private fun detail() = MomotalkRoomDetail(week = week, room = room,
    participants = MOMOTALK_PARTICIPANTS.map { MomotalkParticipant(it, "架空の人格", RecordAvatar(null, "cyan")) },
    messages = listOf(MomotalkMessage(1, "participant-a", "架空の会話です。")), images = emptyList())
  private suspend fun await(condition: () -> Boolean) = withTimeout(5_000) { while (!condition()) delay(10) }
  private suspend fun dispose(model: MomotalkModel) = withContext(Dispatchers.Main) {
    ViewModelStore().apply { put("momotalk", model); clear() }
  }
  private fun model(active: () -> Boolean = { true },
    refresh: suspend (String) -> List<MomotalkRoomsPage> = { rooms() },
    readRoom: suspend (String, String) -> MomotalkRoomDetail = { _, _ -> detail() },
    authenticationRequired: () -> Unit = {}, calls: MutableList<String> = mutableListOf()) = MomotalkModel(active,
    cachedWeeks = { weeks }, refreshWeeks = { calls += "weeks"; weeks }, appendWeeks = { error("unexpected") },
    cachedRooms = { rooms(if (it == week.weekId) week else older) },
    refreshRooms = { calls += "rooms:$it"; refresh(it) }, appendRooms = { _, _ -> error("unexpected") },
    cachedRoom = { _, _ -> detail() }, refreshRoom = readRoom,
    authenticationRequired = authenticationRequired)

  @Test fun offlineVisitReadsSavedConversationWithoutRemoteAndKeepsTheWeekOnReturn() = runBlocking<Unit> {
    val calls = mutableListOf<String>()
    val model = model(calls = calls, readRoom = { _, _ -> error("remote offline") })
    try {
      withContext(Dispatchers.Main) { model.open(false) }
      await { model.state.value.rooms.saved && !model.state.value.rooms.loading }
      withContext(Dispatchers.Main) { model.open(false, week.weekId, room.roomId) }
      await { model.state.value.detail.saved && !model.state.value.detail.loading }
      assertEquals(detail().messages, model.state.value.detail.data?.messages)
      assertEquals(room.roomId, model.state.value.detail.data?.room?.roomId)
      withContext(Dispatchers.Main) { model.open(false) }
      assertNull(model.state.value.selectedRoom)
      assertEquals(week.weekId, model.state.value.selectedWeek)
      assertTrue(calls.isEmpty())
    } finally { dispose(model) }
  }

  @Test fun switchingWeekRejectsALateResponseFromThePreviousWeek() = runBlocking<Unit> {
    val started = CompletableDeferred<Unit>()
    val delayed = CompletableDeferred<List<MomotalkRoomsPage>>()
    val model = model(refresh = { value ->
      if (value == week.weekId) withContext(NonCancellable) { started.complete(Unit); delayed.await() }
      else rooms(older)
    })
    try {
      withContext(Dispatchers.Main) { model.open(true) }
      started.await()
      withContext(Dispatchers.Main) { model.event(MomotalkEvent.SelectWeek(older.weekId)) }
      await { model.state.value.rooms.data?.firstOrNull()?.week?.weekId == older.weekId && !model.state.value.rooms.loading }
      delayed.complete(rooms())
      delay(50)
      assertEquals(older.weekId, model.state.value.selectedWeek)
      assertEquals(older.weekId, model.state.value.rooms.data?.first()?.week?.weekId)
    } finally { delayed.complete(rooms()); dispose(model) }
  }

  @Test fun failedRefreshRetainsSavedMessagesAndAuthenticationFailureClearsThem() = runBlocking<Unit> {
    var failure = MomotalkFailure.UNAVAILABLE
    var authCalls = 0
    val model = model(readRoom = { _, _ -> throw MomotalkException(failure) }, authenticationRequired = { authCalls++ })
    try {
      withContext(Dispatchers.Main) { model.open(true, week.weekId, room.roomId) }
      await { model.state.value.detail.error == MomotalkFailure.UNAVAILABLE }
      assertEquals(detail().messages, model.state.value.detail.data?.messages)
      assertEquals(room.roomId, model.state.value.detail.data?.room?.roomId)
      failure = MomotalkFailure.AUTH_REQUIRED
      withContext(Dispatchers.Main) { model.event(MomotalkEvent.Refresh) }
      await { authCalls == 1 }
      assertNull(model.state.value.detail.data)
      assertNull(model.state.value.rooms.data)
      assertNull(model.state.value.weeks.data)
    } finally { dispose(model) }
  }

  @Test fun endedSessionCannotPublishADelayedConversationOrKeepDecryptedData() = runBlocking<Unit> {
    var active = true
    val response = CompletableDeferred<MomotalkRoomDetail>()
    val started = CompletableDeferred<Unit>()
    val model = model(active = { active }, readRoom = { _, _ ->
      withContext(NonCancellable) { started.complete(Unit); response.await() }
    })
    try {
      withContext(Dispatchers.Main) { model.open(true, week.weekId, room.roomId) }
      started.await()
      withContext(Dispatchers.Main) { active = false; model.endSession() }
      response.complete(detail())
      delay(50)
      assertEquals(MomotalkUiState(), model.state.value)
    } finally { response.complete(detail()); dispose(model) }
  }

  @Test fun anotherRoomStartsWhileThePreviousConversationIsStillLoading() = runBlocking<Unit> {
    val second = room.copy(roomId = "s".repeat(43))
    val started = CompletableDeferred<Unit>()
    val delayed = CompletableDeferred<MomotalkRoomDetail>()
    val model = model(refresh = { listOf(MomotalkRoomsPage(week = week, rooms = listOf(room, second))) },
      readRoom = { _, id ->
        if (id == room.roomId) withContext(NonCancellable) { started.complete(Unit); delayed.await() }
        else detail().copy(room = second)
      })
    try {
      withContext(Dispatchers.Main) { model.open(true) }
      await { model.state.value.rooms.data?.firstOrNull()?.rooms?.size == 2 }
      withContext(Dispatchers.Main) { model.event(MomotalkEvent.SelectRoom(room.roomId)) }
      started.await()
      withContext(Dispatchers.Main) {
        model.event(MomotalkEvent.Back)
        model.event(MomotalkEvent.SelectRoom(second.roomId))
      }
      await { model.state.value.detail.data?.room?.roomId == second.roomId && !model.state.value.detail.loading }
      delayed.complete(detail())
      delay(50)
      assertEquals(second.roomId, model.state.value.detail.data?.room?.roomId)
    } finally { delayed.complete(detail()); dispose(model) }
  }
}
