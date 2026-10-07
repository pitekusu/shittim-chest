package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.then
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Manual, synthetic previews; standard ScreenTest exclusion keeps these out of normal CI. */
@RunWith(AndroidJUnit4::class)
@ScreenTest
class DebateScreensUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private val question = "架空の相談：雨の日に、アロナ・プラナ・安倍晋三AIの3人で楽しむ方法を考えてください。"

  @Test fun draftUsesNativeInputAndKnownOfflineCannotSubmit() {
    val online = mutableStateOf(true)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = true) {
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateComposeScreen(DebateSubmissionState(DebateWorkspace(draft = question)), online.value,
            onEdit = {}, onSubmit = {}, onCheck = {}, onRetry = {}, onReauth = {}, onNew = {},
            modifier = Modifier.safeDrawingPadding())
        }
      }
    } }
    compose.onNodeWithTag("debate-question").assertIsDisplayed()
    screenshot("debate-versus-dark.png")
    compose.runOnIdle { online.value = false }
    compose.onNodeWithTag("debate-submit").performScrollTo().assertIsNotEnabled()
    compose.onNodeWithText(compose.activity.getString(R.string.debate_offline)).assertIsDisplayed()
  }

  @Test fun lightDraftStillRequiresExplicitSubmission() {
    var submitted = 0
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = false) {
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateComposeScreen(DebateSubmissionState(DebateWorkspace(draft = question)), true,
            onEdit = {}, onSubmit = { submitted++ }, onCheck = {}, onRetry = {}, onReauth = {}, onNew = {},
            modifier = Modifier.safeDrawingPadding())
        }
      }
    } }
    screenshot("debate-versus-light.png")
    compose.runOnIdle { assertEquals(0, submitted) }
    compose.onNodeWithTag("debate-submit").performScrollTo().performClick()
    compose.runOnIdle { assertEquals(1, submitted) }
  }

  @Test fun narrowLargeTextDraftRemainsScrollableAndCanSubmit() {
    var submitted = 0
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(320.dp, 640.dp))
        then DeviceConfigurationOverride.FontScale(2f)) {
        ShittimTheme(darkTheme = true) {
          ShittimBackdrop(Modifier.fillMaxSize()) {
            DebateComposeScreen(DebateSubmissionState(DebateWorkspace(draft = question)), true,
              onEdit = {}, onSubmit = { submitted++ }, onCheck = {}, onRetry = {}, onReauth = {}, onNew = {},
              modifier = Modifier.safeDrawingPadding())
          }
        }
      }
    } }
    compose.onNodeWithTag("debate-versus").assertIsDisplayed()
    screenshot("debate-versus-large-text.png")
    compose.onNodeWithTag("debate-submit").performScrollTo().performClick()
    compose.runOnIdle { assertEquals(1, submitted) }
  }

  @Test fun entranceChangesVisibleBadgeThenStaysStill() {
    compose.mainClock.autoAdvance = false
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = true) { DebateVersusHeader(animationsEnabled = true) }
    } }
    compose.mainClock.advanceTimeByFrame()
    val initialWidth = compose.onAllNodesWithText("VS")[0].fetchSemanticsNode().boundsInRoot.width
    compose.mainClock.advanceTimeBy(96)
    val enteringWidth = compose.onAllNodesWithText("VS")[0].fetchSemanticsNode().boundsInRoot.width
    assertTrue("The entrance must not snap directly to its final frame", enteringWidth > initialWidth)
    compose.mainClock.advanceTimeBy(5_000)
    val settledWidth = compose.onAllNodesWithText("VS")[0].fetchSemanticsNode().boundsInRoot.width
    compose.mainClock.advanceTimeBy(1_000)
    assertEquals(settledWidth, compose.onAllNodesWithText("VS")[0].fetchSemanticsNode().boundsInRoot.width, .01f)
  }

  @Test fun publishedProgressOpensOnlyTheArchivedResult() {
    val receipt = DebateRequest(id, question, "published", phase = "completed",
      createdAt = "2026-10-04T00:00:00Z", updatedAt = "2026-10-04T00:01:00Z", recordId = "r".repeat(43))
    var selected: String? = null
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = false) {
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateRequestStatusScreen(id, DebateStatusState(id, receipt), DebateSubmissionState(), true,
            onRetry = {}, onResult = { selected = it }, onReauth = {}, onNew = {},
            modifier = Modifier.safeDrawingPadding())
        }
      }
    } }
    compose.onNodeWithTag("debate-open-result").assertIsDisplayed()
    screenshot("debate-status-light.png")
    compose.onNodeWithTag("debate-open-result").performClick()
    compose.runOnIdle { assertEquals("r".repeat(43), selected) }
  }

  private fun screenshot(name: String) {
    compose.waitForIdle()
    val target = File(requireNotNull(compose.activity.getExternalFilesDir(null)), name)
    target.outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
