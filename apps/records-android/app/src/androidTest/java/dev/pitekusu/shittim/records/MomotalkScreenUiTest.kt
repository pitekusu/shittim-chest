package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class MomotalkScreenUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun weekAndRequesterDrillDownExposeTypedNavigationAndNativeConversation() {
    val state = mutableStateOf(momotalkSampleState())
    val events = mutableListOf<MomotalkEvent>()
    val detail = requireNotNull(state.value.detail.data)
    show(state, events::add)
    compose.onNodeWithTag("momotalk-week-${detail.week.weekId}").performClick()
    compose.runOnIdle { state.value = state.value.copy(selectedWeek = detail.week.weekId) }
    compose.onNodeWithTag("momotalk-week-picker").assertIsDisplayed().performClick()
    compose.onNodeWithTag("momotalk-week-choice-${detail.week.weekId}").performClick()
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-room-${detail.room.roomId}"))
    compose.onNodeWithTag("momotalk-room-${detail.room.roomId}").performClick()
    compose.runOnIdle { state.value = state.value.copy(selectedRoom = detail.room.roomId) }
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasText(detail.messages.first().text))
    compose.onNodeWithText(detail.messages.first().text).assertIsDisplayed()
    compose.onNodeWithTag("momotalk-refresh").performClick()
    compose.onNodeWithTag("momotalk-back").performClick()
    compose.runOnIdle { assertEquals(listOf(MomotalkEvent.SelectWeek(detail.week.weekId),
      MomotalkEvent.SelectWeek(detail.week.weekId), MomotalkEvent.SelectRoom(detail.room.roomId),
      MomotalkEvent.Refresh, MomotalkEvent.Back), events) }
  }

  @Test fun narrowDarkConversationAtDoubleTextKeepsMessagesAndSelfiesReachable() {
    val state = mutableStateOf(momotalkSampleState(openRoom = true))
    val detail = requireNotNull(state.value.detail.data)
    show(state, width = 320, fontScale = 2f, dark = true)
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasText(detail.messages.last().text))
    compose.onNodeWithText(detail.messages.last().text).assertIsDisplayed()
    capture("momotalk-conversation-320dp-2x-dark")
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
    compose.onNodeWithTag("momotalk-selfie-0").assertIsDisplayed().performClick()
    compose.onNodeWithTag("momotalk-selfie-viewer").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-selfie-close").assertIsDisplayed().performClick()
    compose.onNodeWithTag("momotalk-selfie-viewer").assertDoesNotExist()
  }

  @Test fun offlineCachedRoomsRemainReadableWhileUnsavedPagesAndPreparingRoomsAreDisabled() {
    val sample = momotalkSampleState()
    val page = requireNotNull(sample.rooms.data).first()
    val unavailableRoom = page.rooms.first().copy(roomId = "fictional-pending-room".padEnd(43, '0'), state = "preparing")
    val state = mutableStateOf(sample.copy(selectedWeek = page.week.weekId, online = false,
      weeks = sample.weeks.copy(data = sample.weeks.data?.map { it.copy(nextCursor = "2026-10-05") }),
      rooms = MomotalkResource(listOf(page.copy(rooms = page.rooms + unavailableRoom,
        nextCursor = "fictional-next-room".padEnd(43, '0'))), saved = true)))
    val events = mutableListOf<MomotalkEvent>()
    show(state, events::add)
    compose.onNodeWithTag("momotalk-saved").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-refresh").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-week-picker").performClick()
    compose.onNodeWithTag("momotalk-more-weeks").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-week-choice-${page.week.weekId}").performClick()
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-room-${unavailableRoom.roomId}"))
    compose.onNodeWithTag("momotalk-room-${unavailableRoom.roomId}").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-more-rooms"))
    compose.onNodeWithTag("momotalk-more-rooms").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-room-${page.rooms.first().roomId}"))
    compose.onNodeWithTag("momotalk-room-${page.rooms.first().roomId}").assertIsEnabled().performClick()
    compose.runOnIdle { assertEquals(listOf(MomotalkEvent.SelectWeek(page.week.weekId),
      MomotalkEvent.SelectRoom(page.rooms.first().roomId)), events) }
  }

  @Test fun failedReadOffersRetryAndAnEmptyResultNeverLooksLikeLoading() {
    val state = mutableStateOf(MomotalkUiState(online = true,
      weeks = MomotalkResource(error = MomotalkFailure.INVALID_RESPONSE)))
    val events = mutableListOf<MomotalkEvent>()
    show(state, events::add)
    compose.onNodeWithTag("momotalk-retry").assertIsEnabled().performClick()
    compose.runOnIdle { state.value = state.value.copy(weeks = MomotalkResource(loading = true)) }
    compose.onNodeWithTag("momotalk-loading").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-refresh").assertIsNotEnabled()
    compose.runOnIdle { state.value = state.value.copy(weeks = MomotalkResource(data = emptyList())) }
    compose.onNodeWithText(compose.activity.getString(R.string.momotalk_empty_weeks)).assertIsDisplayed()
    compose.onNodeWithTag("momotalk-loading").assertDoesNotExist()
    compose.runOnIdle { assertEquals(listOf(MomotalkEvent.Refresh), events) }
  }

  @Test fun wideConversationShowsRoomListAndBothPhotosCanBeBrowsedInViewer() {
    val sample = momotalkSampleState(openRoom = true)
    val detail = requireNotNull(sample.detail.data)
    val second = detail.images.first().copy(participant = "participant-b", mood = "unhappy",
      subjects = listOf(MomotalkImageSubject("participant-b", "unhappy")))
    val state = mutableStateOf(sample.copy(detail = MomotalkResource(detail.copy(images = detail.images + second))))
    show(state, width = 1000)
    compose.onNodeWithTag("momotalk-rooms-pane").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-content").assertIsDisplayed()
    capture("momotalk-wide")
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
    compose.onNodeWithTag("momotalk-selfie-0").performClick()
    compose.onNodeWithTag("momotalk-selfie-previous").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-selfie-next").assertIsEnabled().performClick()
    compose.onNodeWithTag("momotalk-selfie-next").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-selfie-previous").assertIsEnabled()
    compose.onNodeWithTag("momotalk-selfie-close").performClick()
  }

  /** Screenshots are opt-in evidence; normal CI does not compare pixels. */
  @Test fun captureNativeMomotalkLayouts() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureMomotalk") != "true") return
    data class Layout(val name: String, val width: Int, val font: Float, val dark: Boolean)
    val layouts = listOf(Layout("light", 360, 1f, false), Layout("dark", 360, 1f, true),
      Layout("320dp-2x", 320, 2f, false), Layout("wide", 1000, 1f, false))
    for (layout in layouts) {
      val sample = momotalkSampleState()
      val detail = requireNotNull(sample.detail.data)
      val state = mutableStateOf(sample.copy(selectedWeek = detail.week.weekId))
      show(state, width = layout.width, fontScale = layout.font, dark = layout.dark)
      capture("momotalk-${layout.name}-rooms")
      compose.runOnIdle { state.value = state.value.copy(selectedRoom = detail.room.roomId) }
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasText(detail.messages.first().text))
      capture("momotalk-${layout.name}-conversation")
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
      capture("momotalk-${layout.name}-selfie")
    }
  }

  private fun show(state: MutableState<MomotalkUiState>, onEvent: (MomotalkEvent) -> Unit = {},
    width: Int = 360, fontScale: Float = 1f, dark: Boolean = false) {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        val viewportDensity = density.density * minOf(1f, LocalConfiguration.current.screenWidthDp.toFloat() / width)
        CompositionLocalProvider(LocalDensity provides Density(viewportDensity, fontScale)) {
          ShittimTheme(dark) { Box(Modifier.width(width.dp).fillMaxHeight()) { MomotalkScreen(state.value, onEvent) } }
        }
      }
    }
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureMomotalk") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(requireNotNull(compose.activity.getExternalFilesDir(null)), "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
