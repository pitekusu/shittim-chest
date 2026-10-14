package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.then
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Synthetic receipts exercise the waiting UI without sending a real debate. */
@RunWith(AndroidJUnit4::class)
@ScreenTest
class DebateProgressUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private val recordId = "r".repeat(43)
  private val question = "架空の相談：雨の日に3人で楽しめる過ごし方を考えてください。"

  @Test fun receivedPublicationStopsWaitingAndOpensOnlyItsRecord() {
    val state = mutableStateOf(DebateStatusState(id, request("running", "discussing")))
    var opened: String? = null
    show(dark = false) {
      Screen(state.value, onResult = { opened = it })
    }
    compose.onNodeWithTag("debate-progress-stage-2", useUnmergedTree = true).assertIsSelected()
    compose.onNodeWithTag("debate-open-result").assertDoesNotExist()
    capture("progress-light.png")
    compose.runOnIdle { state.value = DebateStatusState(id, request("published", "completed")) }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("debate-progress-stage-3", useUnmergedTree = true).assertIsSelected()
    compose.onNodeWithTag("debate-open-result").performScrollTo().assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(recordId, opened) }
  }

  @Test fun unknownPhaseShowsTheReceivedStatusWithoutClaimingAnUnobservedStage() {
    show {
      DebateProgressCard(request("running", "future_phase"), motionAllowed = true, animationsEnabled = false)
    }
    compose.onNodeWithTag("debate-progress-card").assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_running), useUnmergedTree = true).assertExists()
    for (stage in 0..3) {
      compose.onNodeWithTag("debate-progress-stage-$stage", useUnmergedTree = true).assertIsNotSelected()
    }
    compose.onNodeWithTag("debate-open-result").assertDoesNotExist()
  }

  @Test fun lostConnectionOrFailedReadStopsActivityAndKeepsRecoveryGuidance() {
    val state = mutableStateOf(DebateStatusState(id, request("running", "discussing")))
    val submission = mutableStateOf(DebateSubmissionState())
    val online = mutableStateOf(true)
    show(dark = true) { Screen(state.value, online = online.value, submission = submission.value) }
    if (ValueAnimator.areAnimatorsEnabled()) {
      compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertExists()
    }
    capture("progress-dark.png")
    compose.runOnIdle { online.value = false }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_requests_online))
      .performScrollTo().assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_running), useUnmergedTree = true).assertExists()
    compose.runOnIdle {
      online.value = true
      state.value = DebateStatusState(id, request("running", "discussing"), failure = DebateFailure.UNAVAILABLE)
    }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_unavailable))
      .performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("debate-open-result").assertDoesNotExist()
    compose.runOnIdle {
      submission.value = DebateSubmissionState(DebateWorkspace(draft = question, requestId = id,
        frozenQuestion = question, receipt = request("running", "discussing")))
      state.value = DebateStatusState(id, failure = DebateFailure.NOT_FOUND)
    }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_running), useUnmergedTree = true).assertExists()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_phase_discussing), useUnmergedTree = true).assertExists()
    compose.onNodeWithTag("debate-open-result").assertDoesNotExist()
  }

  @Test fun failedAndCancelledReceiptsCannotLookPublishedOrKeepWaiting() {
    val state = mutableStateOf(DebateStatusState(id, request("failed", "failed")))
    show { Screen(state.value) }
    for ((status, label) in listOf("failed" to R.string.debate_failed, "cancelled" to R.string.debate_cancelled)) {
      compose.runOnIdle { state.value = DebateStatusState(id, request(status, status)) }
      compose.onNodeWithText(compose.activity.getString(label), useUnmergedTree = true).assertExists()
      compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
      compose.onNodeWithTag("debate-open-result").assertDoesNotExist()
      for (stage in 0..3) {
        compose.onNodeWithTag("debate-progress-stage-$stage", useUnmergedTree = true).assertDoesNotExist()
      }
      compose.onNodeWithText(compose.activity.getString(R.string.debate_new))
        .performScrollTo().assertIsDisplayed()
    }
  }

  @Test fun narrowLargeTextKeepsParticipantsPhaseAndPublishedActionReachable() {
    val state = mutableStateOf(DebateStatusState(id, request("running", "discussing")))
    var opened: String? = null
    show(dark = true) {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(320.dp, 800.dp)) then
        DeviceConfigurationOverride.FontScale(2f)) {
        Screen(state.value, onResult = { opened = it })
      }
    }
    for (name in listOf("アロナ", "プラナ", "安倍晋三AI")) {
      compose.onNodeWithText(name, useUnmergedTree = true).performScrollTo().assertIsDisplayed()
    }
    compose.onNodeWithText(compose.activity.getString(R.string.debate_phase_discussing), useUnmergedTree = true)
      .performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("debate-progress-card").performScrollTo()
    capture("progress-large-text.png")
    compose.runOnIdle { state.value = DebateStatusState(id, request("published", "completed")) }
    compose.onNodeWithTag("debate-open-result").performScrollTo().assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(recordId, opened) }
  }

  @Test fun disabledAnimationsKeepTheCurrentStageAndParticipantsStill() {
    show {
      DebateProgressCard(request("running", "discussing"), motionAllowed = true, animationsEnabled = false)
    }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("debate-progress-stage-2", useUnmergedTree = true).assertIsSelected()
    val avatars = (0..2).map { index ->
      compose.onNodeWithTag("debate-progress-avatar-$index", useUnmergedTree = true)
        .assertIsDisplayed().fetchSemanticsNode().boundsInRoot
    }
    compose.mainClock.advanceTimeBy(2_000)
    for (index in 0..2) {
      assertEquals(avatars[index], compose.onNodeWithTag("debate-progress-avatar-$index", useUnmergedTree = true)
        .fetchSemanticsNode().boundsInRoot)
    }
  }

  @Test fun leavingForegroundStopsMotionWithoutDiscardingReceivedProgress() {
    lateinit var owner: Owner
    compose.runOnUiThread { owner = Owner().apply { lifecycle.currentState = Lifecycle.State.RESUMED } }
    show {
      CompositionLocalProvider(LocalLifecycleOwner provides owner) {
        DebateProgressCard(request("running", "discussing"), motionAllowed = true, animationsEnabled = true)
      }
    }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertExists()
    compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.STARTED }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("debate-progress-stage-2", useUnmergedTree = true).assertIsSelected()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_phase_discussing), useUnmergedTree = true).assertExists()
    compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.RESUMED }
    compose.onNodeWithTag("debate-progress-motion", useUnmergedTree = true).assertExists()
  }

  private fun request(status: String, phase: String) = DebateRequest(id, question, status, phase,
    createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:01:00Z",
    recordId = recordId.takeIf { status == "published" })

  @Composable
  private fun Screen(state: DebateStatusState, online: Boolean = true,
    submission: DebateSubmissionState = DebateSubmissionState(), onResult: (String) -> Unit = {}) {
    DebateRequestStatusScreen(id, state, submission, online, onRetry = {},
      onResult = onResult, onReauth = {}, onNew = {}, modifier = Modifier.safeDrawingPadding())
  }

  private fun show(dark: Boolean = false, content: @Composable () -> Unit) {
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = dark) { ShittimBackdrop(Modifier.fillMaxSize()) { content() } }
    } }
    compose.waitForIdle()
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureProgress") != "true") return
    compose.waitForIdle()
    File(requireNotNull(compose.activity.getExternalFilesDir(null)), name).outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private class Owner : LifecycleOwner {
    override val lifecycle = LifecycleRegistry(this)
  }
}
