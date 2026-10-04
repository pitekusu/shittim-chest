package dev.pitekusu.shittim.records

import android.Manifest
import android.animation.ValueAnimator
import android.graphics.Bitmap
import android.provider.Settings
import android.view.KeyEvent
import androidx.activity.BackEventCompat
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
import kotlin.math.abs
import kotlin.math.roundToInt
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Real animation frames, not only the final route or a branch skipped when system motion is off. */
@RunWith(AndroidJUnit4::class)
@ScreenTest
class Nav3SceneMotionUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val animationScaleSettings = listOf(Settings.Global.WINDOW_ANIMATION_SCALE,
    Settings.Global.TRANSITION_ANIMATION_SCALE, Settings.Global.ANIMATOR_DURATION_SCALE)
  private var originalAnimationScales: Map<String, String?>? = null
  private val entries = (1..8).map { index ->
    RecordListEntry(index.toString().padStart(43, 'a'), "遷移確認の架空の相談 $index",
      "画面試験用", RecordAvatar(null, "cyan"), Instant.parse("2026-10-01T00:00:00Z"), "アロナ")
  }
  private val session = SessionState.SignedIn(
    MobileSessionUser("画面試験用", MobileAvatar("placeholder", "確認用", "cyan")),
    "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"))
  private val question = "遷移確認の架空の議題"
  private val records = RecordListState.Ready.fromSaved(entries)
  private val preview = RecordPreviewState.Ready(RecordPreview(question, "架空の結論", "アロナ"), saved = true)

  @Before fun enableMotionForThisTest() {
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    val resolver = instrumentation.targetContext.contentResolver
    // The standard API enables all three together; preserve their separate original values.
    originalAnimationScales = animationScaleSettings.associateWith { Settings.Global.getString(resolver, it) }
    instrumentation.uiAutomation.setAnimationScale(1f)
    instrumentation.waitForIdleSync()
    compose.waitUntil(5_000) {
      ValueAnimator.areAnimatorsEnabled() && ValueAnimator.getDurationScale() == 1f
    }
    compose.runOnIdle { assertTrue(ValueAnimator.areAnimatorsEnabled()) }
  }

  @After fun restoreOriginalAnimationScales() {
    val original = originalAnimationScales ?: return
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    val resolver = instrumentation.targetContext.contentResolver
    val automation = instrumentation.uiAutomation
    // Restoring distinct or unset values needs Settings; do not hold shell identity in the test.
    automation.adoptShellPermissionIdentity(Manifest.permission.WRITE_SECURE_SETTINGS)
    try {
      original.forEach { (setting, value) ->
        assertTrue("Restore $setting", Settings.Global.putString(resolver, setting, value))
      }
    } finally {
      automation.dropShellPermissionIdentity()
    }
    instrumentation.waitForIdleSync()
    val originalDuration = original[Settings.Global.ANIMATOR_DURATION_SCALE]?.toFloatOrNull() ?: 1f
    compose.waitUntil(5_000) {
      original.all { (setting, value) -> Settings.Global.getString(resolver, setting) == value } &&
        ValueAnimator.getDurationScale() == originalDuration
    }
  }

  private inner class Host {
    val backStack = NavBackStack<NavKey>(RecordsList)
    val authorized = mutableStateOf(true)
    val events = mutableListOf<BootstrapScreen.Event>()

    fun state(): BootstrapScreen.State {
      if (!authorized.value) return BootstrapScreen.State(ThemeChoice.Dark) {}
      val selected = (backStack.lastOrNull() as? RecordDetail)?.recordId
      return BootstrapScreen.State(ThemeChoice.Dark, session,
        records = records, backStack = backStack.toList(),
        selectedRecordId = selected,
        // Match the presenter: the live preview disappears immediately on pop.
        record = if (selected == null) RecordPreviewState.Idle else preview,
        eventSink = { event ->
          events += event
          when (event) {
            is BootstrapScreen.Event.OpenRecord -> backStack.openRecord(event.recordId)
            BootstrapScreen.Event.CloseRecord -> backStack.closeRecord()
            else -> Unit
          }
        })
    }
  }

  private fun show(host: Host) {
    assertTrue("Run motion tests with system animator_duration_scale=1", ValueAnimator.areAnimatorsEnabled())
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 850.dp))) {
        BootstrapUi(host.state())
      }
    } }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(entries.last().questionPreview))
    compose.waitForIdle()
  }

  @Test fun compactPushAndButtonPopSlideWholeOpaqueEntriesAndKeepListPosition() {
    val host = Host()
    show(host)
    val position = listPosition()
    val list = compose.onNodeWithTag("records-list-pane").fetchSemanticsNode()
    val paneWidth = list.size.width.toFloat()
    val paneTop = list.positionInRoot.y
    val paneHeight = list.size.height.toFloat()
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { host.backStack.openRecord(entries.last().recordId) }
      compose.mainClock.advanceTimeBy(128)
      val incoming = compose.onNodeWithTag("records-detail-pane").fetchSemanticsNode()
      assertTrue("The complete detail entry must slide in from the right",
        incoming.positionInRoot.x > 1f && incoming.positionInRoot.x < paneWidth - 1f)
      assertTrue("The complete list entry must slide left under the detail",
        compose.onNodeWithTag("records-list-pane").fetchSemanticsNode().positionInRoot.x < -1f)
      val incomingLeft = incoming.positionInRoot.x
      val incomingFrame = compose.onRoot().captureToImage().asAndroidBitmap()
      compose.mainClock.advanceTimeBy(192)
      finishOutgoingEntry("records-list-pane")
      compose.onNodeWithText(question).assertIsDisplayed()
      compose.onNodeWithTag("records-list-pane").assertDoesNotExist()
      val settledFrame = compose.onRoot().captureToImage().asAndroidBitmap()
      val settledDetailLeft = compose.onNodeWithTag("records-detail-pane")
        .fetchSemanticsNode().positionInRoot.x
      // Exercise the actual three-button/key event path, not only the dispatcher's callback.
      InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
      compose.mainClock.advanceTimeBy(96)
      assertTrue("Button Back must slide the retained detail to the right",
        compose.onNodeWithTag("records-detail-pane").fetchSemanticsNode().positionInRoot.x > settledDetailLeft + 1f)
      assertTrue("The list must slide in from the left",
        compose.onNodeWithTag("records-list-pane").fetchSemanticsNode().positionInRoot.x < -1f)
      compose.onNodeWithText(question, useUnmergedTree = true).assertExists()
      compose.onNodeWithText(compose.activity.getString(R.string.record_loading)).assertDoesNotExist()
      compose.mainClock.advanceTimeBy(240)
      finishOutgoingEntry("records-detail-pane")
      compose.onNodeWithTag("records-detail-pane").assertDoesNotExist()
      compose.onNodeWithText(entries.last().questionPreview).assertIsDisplayed()
      assertEquals(position, listPosition(), .01f)
      compose.runOnIdle {
        assertEquals(listOf(RecordsList), host.backStack.toList())
        assertEquals(1, host.events.count { it == BootstrapScreen.Event.CloseRecord })
      }
      // A blank region in the incoming detail must move with that entry's background.
      // Comparing against its settled pixels detects outgoing cards showing through it.
      assertMovingBackground(incomingFrame, settledFrame, incomingLeft,
        paneWidth * .15f, paneTop + paneHeight * .65f)
    } finally {
      compose.mainClock.autoAdvance = true
    }
  }

  @Test fun cancellingPredictiveBackRestoresTheDetailAndRevocationRemovesItMidSlide() {
    val host = Host()
    show(host)
    compose.runOnIdle { host.backStack.openRecord(entries.last().recordId) }
    compose.onNodeWithText(question).assertIsDisplayed()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    val detailLeft = compose.onNodeWithTag("records-detail-pane").fetchSemanticsNode().positionInRoot.x
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
      compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(120f, 0f, .6f, BackEventCompat.EDGE_LEFT)) }
      // NavDisplay seeks from a LaunchedEffect; allow that seek and its layout frame.
      repeat(3) { compose.mainClock.advanceTimeByFrame() }
      assertTrue(compose.onNodeWithTag("records-detail-pane").fetchSemanticsNode().positionInRoot.x > detailLeft + 1f)
      compose.runOnIdle { dispatcher.dispatchOnBackCancelled() }
      compose.mainClock.advanceTimeBy(320)
      finishOutgoingEntry("records-list-pane")
      assertEquals(detailLeft, compose.onNodeWithTag("records-detail-pane").fetchSemanticsNode().positionInRoot.x, 1f)
      compose.onNodeWithTag("records-list-pane").assertDoesNotExist()
      compose.runOnIdle {
        assertEquals(RecordDetail(entries.last().recordId), host.backStack.last())
        assertEquals(0, host.events.count { it == BootstrapScreen.Event.CloseRecord })
        dispatcher.onBackPressed()
      }
      compose.mainClock.advanceTimeBy(96)
      compose.onNodeWithTag("records-detail-pane").assertExists()
      compose.runOnIdle { host.authorized.value = false }
      // One composition frame, not the remaining duration of the outgoing transition.
      compose.mainClock.advanceTimeByFrame()
      compose.onNodeWithTag("records-detail-pane").assertDoesNotExist()
      compose.onNodeWithTag("records-list-pane").assertDoesNotExist()
      compose.onNodeWithText(question).assertDoesNotExist()
      compose.runOnIdle { assertEquals(1, host.events.count { it == BootstrapScreen.Event.CloseRecord }) }
    } finally {
      compose.mainClock.autoAdvance = true
    }
  }

  private fun listPosition(): Float = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
    .config[SemanticsProperties.VerticalScrollAxisRange].value()

  private fun finishOutgoingEntry(tag: String) {
    // The 280ms slide reaches its final offset before NavDisplay's seek/snap coroutine
    // and AnimatedContent disposal complete. Bound only that final frame handoff.
    repeat(6) {
      if (compose.onAllNodesWithTag(tag).fetchSemanticsNodes().isEmpty()) return
      compose.mainClock.advanceTimeByFrame()
    }
    compose.onNodeWithTag(tag).assertDoesNotExist()
  }

  private fun assertMovingBackground(moving: Bitmap, settled: Bitmap, offset: Float, x: Float, y: Float) {
    var colorDifference = 0L
    for (dx in 0 until 16) for (dy in 0 until 16) {
      val actual = moving.getPixel((x + offset).roundToInt() + dx, y.roundToInt() + dy)
      val expected = settled.getPixel(x.roundToInt() + dx, y.roundToInt() + dy)
      for (shift in listOf(0, 8, 16)) {
        colorDifference += abs(((actual ushr shift) and 255) - ((expected ushr shift) and 255))
      }
    }
    assertTrue("The detail background must cover the outgoing list throughout its slide",
      colorDifference / (16f * 16f * 3f) < 3f)
  }
}
