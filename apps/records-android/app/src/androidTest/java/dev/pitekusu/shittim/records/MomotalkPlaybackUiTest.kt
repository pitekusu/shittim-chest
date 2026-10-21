package dev.pitekusu.shittim.records

import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.MotionDurationScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Authored conversations exercise playback without API requests or real account data. */
@RunWith(AndroidJUnit4::class)
@ScreenTest
class MomotalkPlaybackUiTest {
  // The CI device disables system animations. Compose's standard test context
  // gives these explicit playback tests a deterministic clock without changing device settings.
  @get:Rule val compose = createAndroidComposeRule<MainActivity>(effectContext = object : MotionDurationScale {
    override val scaleFactor = 1f
  })

  @After fun restoreClock() { compose.mainClock.autoAdvance = true }

  @Test fun skipShowsTheWholeConversationAndPhotosImmediatelyAndReplayStartsAgain() {
    val state = mutableStateOf(shortConversation())
    val detail = requireNotNull(state.value.detail.data)
    val events = mutableListOf<MomotalkEvent>()
    show(state, onEvent = events::add)
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-follow").assertDoesNotExist()
    compose.onNodeWithText(detail.messages.last().text).assertDoesNotExist()
    compose.onNodeWithTag("momotalk-selfie-0").assertDoesNotExist()

    skip()
    compose.onNodeWithTag("momotalk-playback-replay").assertIsDisplayed()
    scrollWhileStatic {
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasText(detail.messages.last().text))
      compose.onNodeWithText(detail.messages.last().text).assertIsDisplayed()
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
      compose.onNodeWithTag("momotalk-selfie-0").assertIsDisplayed()
    }
    compose.onNodeWithTag("momotalk-playback-replay").assertIsDisplayed().performClick()
    frame()
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
    compose.onNodeWithText(detail.messages.last().text).assertDoesNotExist()
    compose.onNodeWithTag("momotalk-selfie-0").assertDoesNotExist()
    compose.runOnIdle { assertTrue("Playback controls must not refresh the API", events.isEmpty()) }
  }

  @Test fun warmingCachedImagesAndRefreshingTheSameRoomDoNotReplayTheConversation() {
    val initial = shortConversation()
    val detail = requireNotNull(initial.detail.data)
    val state = mutableStateOf(initial.copy(detail = initial.detail.copy(saved = true,
      data = detail.copy(images = detail.images.map { it.copy(bytes = null) }))))
    show(state)
    skip()
    compose.runOnIdle {
      state.value = state.value.copy(detail = state.value.detail.copy(data = detail.copy(
        images = detail.images.map { it.copy(bytes = it.bytes?.copyOf()) }), saved = false, revision = 1))
    }
    frame()
    compose.onNodeWithTag("momotalk-playback-replay").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-skip").assertDoesNotExist()
    scrollWhileStatic {
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
      compose.onNodeWithTag("momotalk-selfie-0").assertIsDisplayed()
    }
  }

  @Test fun switchingRoomsAndWeeksStartsTheirOwnPlayback() {
    val state = mutableStateOf(shortConversation())
    val first = requireNotNull(state.value.detail.data)
    show(state)
    skip()
    val otherRoom = first.room.copy(roomId = "fictional-other-room".padEnd(43, '0'))
    compose.runOnIdle {
      state.value = state.value.copy(selectedRoom = otherRoom.roomId,
        detail = MomotalkResource(first.copy(room = otherRoom,
          messages = first.messages.map { it.copy(text = "別の先生：${it.text}") })))
    }
    frame()
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-replay").assertDoesNotExist()
    compose.onNodeWithText(first.messages.last().text).assertDoesNotExist()
    skip()
    compose.runOnIdle {
      val current = requireNotNull(state.value.detail.data)
      val olderWeek = current.week.copy(weekId = "2026-10-05")
      state.value = state.value.copy(selectedWeek = olderWeek.weekId,
        detail = MomotalkResource(current.copy(week = olderWeek)))
    }
    frame()
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-replay").assertDoesNotExist()
  }

  @Test fun leavingForegroundPausesAndResumingContinuesTheRemainingTurn() {
    lateinit var owner: Owner
    compose.runOnUiThread { owner = Owner().apply { lifecycle.currentState = Lifecycle.State.RESUMED } }
    val state = mutableStateOf(shortConversation())
    val detail = requireNotNull(state.value.detail.data)
    show(state, owner = owner)
    compose.mainClock.advanceTimeBy(1_150)
    compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.STARTED }
    frame()
    compose.mainClock.advanceTimeBy(5_000)
    compose.onNodeWithText(detail.messages.first().text).assertDoesNotExist()
    compose.onNodeWithText(detail.messages.last().text).assertDoesNotExist()
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
    compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.RESUMED }
    frame()
    // Each short turn lasts about 1.4 s: 0.5 s finishes the remainder, not a restarted turn.
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithText(detail.messages.first().text).assertExists()
    compose.onNodeWithText(detail.messages.last().text).assertDoesNotExist()
  }

  @Test fun disabledMotionShowsSavedOfflineMessagesAndPhotosWithoutAPlaybackWait() {
    val initial = shortConversation()
    val state = mutableStateOf(initial.copy(online = false, detail = initial.detail.copy(saved = true)))
    show(state, animationsEnabled = false)
    compose.onNodeWithTag("momotalk-refresh").assertIsNotEnabled()
    compose.onNodeWithTag("momotalk-playback-skip").assertDoesNotExist()
    val last = requireNotNull(state.value.detail.data).messages.last().text
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasText(last))
    compose.onNodeWithText(last).assertIsDisplayed()
    compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
    compose.onNodeWithTag("momotalk-selfie-0").assertIsDisplayed()
    compose.mainClock.advanceTimeBy(5_000)
    compose.onNodeWithTag("momotalk-selfie-0").assertIsDisplayed()
  }

  @Test fun wideAndLargeTextLayoutsKeepThePlaybackActionReachableAfterSkipping() {
    val state = mutableStateOf(shortConversation())
    val width = mutableStateOf(1000)
    val fontScale = mutableStateOf(1f)
    show(state, width = width, fontScale = fontScale)
    compose.onNodeWithTag("momotalk-rooms-pane").assertIsDisplayed()
    skip()
    scrollWhileStatic {
      compose.onNodeWithTag("momotalk-content").performScrollToNode(hasTestTag("momotalk-selfie-0"))
    }
    compose.onNodeWithTag("momotalk-playback-replay").assertIsDisplayed()
    compose.runOnIdle { width.value = 320; fontScale.value = 2f }
    frame()
    compose.onNodeWithTag("momotalk-rooms-pane").assertDoesNotExist()
    compose.onNodeWithTag("momotalk-playback-replay").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-replay").performClick()
    frame()
    compose.onNodeWithTag("momotalk-playback-skip").assertIsDisplayed()
  }

  @Test fun typingBoundariesKeepCombiningMarksModifiersJoinedEmojiAndFlagsWhole() {
    val characters = listOf("か\u3099", "e\u0301", "👍🏽", "👨‍👩‍👧‍👦", "🇯🇵")
    val text = characters.joinToString("")
    assertEquals(characters.runningFold(0) { end, character -> end + character.length }.drop(1),
      momotalkGraphemeEnds(text))
    assertTrue(momotalkGraphemeEnds("").isEmpty())
  }

  @Test fun restorationRetainsNumericProgressAndTheReadersFollowChoice() {
    val messages = requireNotNull(shortConversation().detail.data).messages
    val active = mutableStateOf(true)
    lateinit var playback: MomotalkPlayback
    val restoration = StateRestorationTester(compose)
    compose.mainClock.autoAdvance = false
    // MainActivity has its own content; the standard restoration helper needs an empty host.
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent {
      val current = rememberMomotalkPlayback(messages, active.value, motionAllowed = true, replay = 0)
      SideEffect { playback = current }
    }
    frame()
    compose.mainClock.advanceTimeBy(1_150)
    compose.runOnIdle { active.value = false; playback.state.following = false }
    frame()
    lateinit var before: MomotalkPlaybackState
    var progress = 0f
    compose.runOnIdle {
      before = playback.state
      progress = before.turn.value
      assertEquals(0, before.completed)
      assertTrue(progress > 0f && progress < 1f)
    }
    // The helper removes and restores composition in separate frames; allow both while paused.
    compose.mainClock.autoAdvance = true
    try {
      restoration.emulateSavedInstanceStateRestore()
      compose.waitForIdle()
    } finally { compose.mainClock.autoAdvance = false }
    frame()
    compose.runOnIdle {
      assertNotSame(before, playback.state)
      assertEquals(0, playback.state.completed)
      assertEquals(progress, playback.state.turn.value, .0001f)
      assertFalse(playback.state.skipped)
      assertFalse(playback.state.following)
      active.value = true
    }
    frame()
    compose.mainClock.advanceTimeBy(500)
    compose.runOnIdle { assertEquals(1, playback.state.completed) }
  }

  @Test fun readingEarlierLinesStopsFollowingAndTheFixedActionReturnsToTheNewText() {
    val sample = shortConversation()
    val detail = requireNotNull(sample.detail.data)
    val state = mutableStateOf(sample.copy(detail = MomotalkResource(detail.copy(messages = listOf(
      MomotalkMessage(1, "participant-a", "架空の会話を一緒に読みます。".repeat(80)))))))
    val width = mutableStateOf(320)
    val fontScale = mutableStateOf(2f)
    show(state, width = width, fontScale = fontScale)
    compose.mainClock.advanceTimeBy(5_000)
    frame()
    compose.onNodeWithTag("momotalk-playback-follow").assertDoesNotExist()
    val tail = readingPosition()
    assertTrue("The synthetic long bubble must have scrolled", tail > 0f)

    compose.onNodeWithTag("momotalk-content").performTouchInput {
      swipe(center, center.copy(y = center.y + 140f), durationMillis = 1_000)
    }
    compose.mainClock.advanceTimeBy(500)
    frame()
    compose.onNodeWithTag("momotalk-playback-follow").assertIsDisplayed()
    val reading = readingPosition()
    assertTrue("Reading earlier lines must move away from the playback tail", reading < tail)
    compose.mainClock.advanceTimeBy(2_000)
    frame()
    assertEquals("New letters must not move the reader's position", reading, readingPosition(), .01f)

    compose.runOnIdle { width.value = 1000; fontScale.value = 1f }
    frame()
    compose.onNodeWithTag("momotalk-rooms-pane").assertIsDisplayed()
    compose.onNodeWithTag("momotalk-playback-follow").assertIsDisplayed()
    compose.runOnIdle { width.value = 320; fontScale.value = 2f }
    frame()
    compose.onNodeWithTag("momotalk-rooms-pane").assertDoesNotExist()
    compose.onNodeWithTag("momotalk-playback-follow").assertIsDisplayed()

    compose.onNodeWithTag("momotalk-playback-follow").assertIsDisplayed().performClick()
    frame()
    compose.onNodeWithTag("momotalk-playback-follow").assertDoesNotExist()
    val range = compose.onNodeWithTag("momotalk-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange]
    assertEquals("Following returns to the currently revealed line", range.maxValue(), range.value(), .01f)
  }

  private fun shortConversation(): MomotalkUiState {
    val sample = momotalkSampleState(openRoom = true)
    val detail = requireNotNull(sample.detail.data)
    return sample.copy(detail = MomotalkResource(detail.copy(messages = listOf(
      MomotalkMessage(1, "participant-a", "一つ目。"), MomotalkMessage(2, "participant-b", "二つ目。")))))
  }

  private fun skip() {
    compose.onNodeWithTag("momotalk-playback-skip").performClick()
    frame()
  }

  private fun frame() { repeat(3) { compose.mainClock.advanceTimeByFrame() } }

  /** Scroll-to-node searches LazyColumn layouts, so its static frames must be allowed to run. */
  private fun scrollWhileStatic(block: () -> Unit) {
    val previous = compose.mainClock.autoAdvance
    compose.mainClock.autoAdvance = true
    try { block(); compose.waitForIdle() } finally { compose.mainClock.autoAdvance = previous }
  }

  private fun readingPosition(): Float = compose.onNodeWithTag("momotalk-content").fetchSemanticsNode()
    .config[SemanticsProperties.VerticalScrollAxisRange].value()

  private fun show(state: MutableState<MomotalkUiState>, animationsEnabled: Boolean = true,
    owner: LifecycleOwner? = null, width: MutableState<Int> = mutableStateOf(420),
    fontScale: MutableState<Float> = mutableStateOf(1f), onEvent: (MomotalkEvent) -> Unit = {}) {
    compose.mainClock.autoAdvance = !animationsEnabled
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      val density = LocalDensity.current
      val viewportDensity = density.density * minOf(1f, LocalConfiguration.current.screenWidthDp.toFloat() / width.value)
      CompositionLocalProvider(LocalDensity provides Density(viewportDensity, fontScale.value),
        LocalLifecycleOwner provides (owner ?: LocalLifecycleOwner.current)) {
        ShittimTheme { Box(Modifier.width(width.value.dp).fillMaxHeight()) {
          MomotalkScreen(state.value, onEvent, animationsEnabled = animationsEnabled)
        } }
      }
    } }
    frame()
  }

  private class Owner : LifecycleOwner { override val lifecycle = LifecycleRegistry(this) }
}
