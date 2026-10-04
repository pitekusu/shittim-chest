package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import android.graphics.Bitmap
import android.view.KeyEvent
import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordDetailVisualTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun affectionDefaultsToWinnerAndRetainsSelectionThroughRefreshAndRestoration() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(visualPreview()))
    val restoration = StateRestorationTester(compose)
    // MainActivity has installed its normal UI. Give the standard restoration helper an empty host.
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent { ShittimTheme(true) {
      RecordDetailScreen(state.value, "visual-sample", {}, motionAllowed = false)
    } }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.onNodeWithTag("affection-person-1").assertIsSelected()
    compose.onNodeWithText("親愛度：200 → 185").assertExists()
    compose.onNodeWithTag("affection-card-0").assertDoesNotExist()
    compose.onNodeWithTag("affection-person-2").performClick()
    compose.onNodeWithText("実増減：0点").assertExists()
    compose.runOnIdle { state.value = RecordPreviewState.Ready(visualPreview(), updating = true) }
    restoration.emulateSavedInstanceStateRestore()
    compose.onNodeWithTag("detail-section-Affection").assertIsSelected()
    compose.onNodeWithTag("affection-person-2").assertIsSelected()
    compose.onNodeWithText("親愛度：1000 → 1000").assertExists()
  }

  @Test fun eachPersonAndReopenedAffectionPagePlayAgainButRefreshDoesNot() {
    val active = mutableStateOf(false)
    val open = mutableStateOf(true)
    val played = mutableStateOf(emptySet<String>())
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(visualPreview()))
    val seen = mutableListOf<String>()
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      if (open.value) RecordDetailScreen(state.value, "visual-sample", {}, motionAllowed = active.value,
        playedSections = played.value, onSectionSeen = { key ->
          seen += key
          played.value = played.value + key
        })
    } } }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.runOnIdle { assertTrue(played.value.isEmpty()) }
    compose.mainClock.autoAdvance = false
    compose.runOnIdle { active.value = true }
    compose.mainClock.advanceTimeBy(64)
    // CI disables system animations. Both settings must preserve the saved final value.
    if (ValueAnimator.areAnimatorsEnabled()) {
      compose.onNodeWithText("親愛度：200 → 185").assertDoesNotExist()
    } else {
      compose.onNodeWithText("親愛度：200 → 185").assertExists()
    }
    compose.mainClock.advanceTimeBy(1_200)
    compose.onNodeWithText("親愛度：200 → 185").assertExists()
    compose.mainClock.autoAdvance = true
    compose.onNodeWithTag("affection-person-0").performClick()
    compose.onNodeWithTag("affection-card-0").performScrollTo()
    compose.waitUntil(5_000) { "affection:visual-sample:0" in played.value }
    compose.onNodeWithTag("affection-person-2").performClick()
    compose.onNodeWithTag("affection-card-2").performScrollTo()
    compose.waitUntil(5_000) { "affection:visual-sample:2" in played.value }
    compose.onNodeWithTag("affection-person-1").performClick()
    compose.onNodeWithTag("detail-section-Opinions").performClick()
    compose.waitForIdle()
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.runOnIdle { state.value = RecordPreviewState.Ready(visualPreview(), updating = true) }
    compose.onNodeWithText("親愛度：200 → 185").assertExists()
    compose.runOnIdle {
      assertEquals(3, seen.count { it == "affection:visual-sample:1" })
      assertEquals(1, seen.count { it == "affection:visual-sample:0" })
      assertEquals(1, seen.count { it == "affection:visual-sample:2" })
    }
    compose.runOnIdle { open.value = false }
    compose.runOnIdle { open.value = true }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.runOnIdle { assertEquals(4, seen.count { it == "affection:visual-sample:1" }) }
  }

  @Test fun voteSheetPreservesVoterAndReadingPositionAcrossRefreshAndReopening() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(visualPreview()))
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      RecordDetailScreen(state.value, "visual-sample", {}, motionAllowed = false)
    } } }
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitForIdle()
    compose.onNodeWithTag("vote-person-0").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("vote-detail-sheet").isDisplayed() }
    compose.onNodeWithTag("vote-detail-content").performTouchInput {
      // Read partway down, away from the bottom bound which changes with sheet window insets.
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    fun position() = compose.onNodeWithTag("vote-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    val offset = position()
    assertTrue(offset > 0f)
    compose.runOnIdle { state.value = RecordPreviewState.Ready(visualPreview(), updating = true) }
    compose.onNodeWithTag("vote-detail-sheet").assertExists()
    assertEquals(offset, position(), 0.01f)
    InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
    compose.waitUntil(5_000) { !compose.onNodeWithTag("vote-detail-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    compose.onNodeWithTag("vote-person-0").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("vote-detail-sheet").isDisplayed() }
    assertEquals(offset, position(), 0.01f)
  }

  @Test fun largeTextKeepsBallotsAndEveryAffectionChoiceReachable() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      val density = LocalDensity.current
      CompositionLocalProvider(LocalDensity provides Density(density.density, 2f)) {
        Box(Modifier.width(320.dp).windowInsetsPadding(WindowInsets.safeDrawing)) {
          RecordDetailScreen(RecordPreviewState.Ready(visualPreview()), "visual-sample", {},
            motionAllowed = false)
        }
      }
    } } }
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitForIdle()
    compose.onNodeWithTag("vote-graph").assertDoesNotExist()
    compose.onNodeWithTag("vote-route-0").performScrollTo().performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("vote-detail-sheet").isDisplayed() }
    InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
    compose.waitUntil(5_000) { !compose.onNodeWithTag("vote-detail-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    for (index in 0..2) {
      // Persona controls now stay above the pager, not inside a scrolling/sticky list.
      compose.onNodeWithTag("affection-person-$index").assertIsDisplayed().performClick().assertIsSelected()
      compose.onNodeWithTag("affection-card-$index").assertExists()
    }
    capture("detail-large-text")
  }

  @Test fun backClosesAnExpandedLongQuestionWithoutChangingThePage() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      RecordDetailScreen(RecordPreviewState.Ready(RecordPreview(
        "架空の長い議題です。\n".repeat(60), "架空の結論", "アロナ")),
        "visual-sample", {}, motionAllowed = false)
    } } }
    compose.onNodeWithTag("detail-question-open").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
    compose.waitUntil(5_000) { !compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  /** Optional, synthetic-only preview used for screenshots and the short interaction recording. */
  @Test fun captureFourPagePreview() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    val dark = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(dark.value) {
      ShittimBackdrop { Box(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
        RecordDetailScreen(RecordPreviewState.Ready(visualPreview()), "visual-sample", {},
          motionAllowed = false)
      } }
    } } }
    for (mode in listOf(false, true)) {
      compose.runOnIdle { dark.value = mode }
      for (section in RecordDetailSection.entries) {
        compose.onNodeWithTag("detail-section-${section.name}").performClick()
        compose.waitForIdle()
        capture("detail-${section.name.lowercase()}-${if (mode) "dark" else "light"}")
        if (section == RecordDetailSection.Result) {
          compose.onNodeWithTag("detail-actions-expand").performScrollTo().performClick()
          compose.onNodeWithTag("detail-caveats-expand").performScrollTo().performClick()
          capture("detail-result-expanded-${if (mode) "dark" else "light"}")
          compose.onNodeWithTag("detail-actions-expand").performClick()
          compose.onNodeWithTag("detail-caveats-expand").performClick()
        }
      }
    }
    if (InstrumentationRegistry.getArguments().getString("shittimRecordUi") == "true") {
      val played = mutableStateOf(emptySet<String>())
      compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(true) {
        ShittimBackdrop { Box(Modifier.fillMaxSize().windowInsetsPadding(WindowInsets.safeDrawing)) {
          RecordDetailScreen(RecordPreviewState.Ready(visualPreview()), "visual-sample", {},
            playedSections = played.value, onSectionSeen = { played.value = played.value + it })
        } }
      } } }
      compose.mainClock.autoAdvance = false
      for (section in listOf(RecordDetailSection.Voting, RecordDetailSection.Affection)) {
        compose.onNodeWithTag("detail-section-${section.name}").performClick()
        // Only the optional recording advances real time. Normal CI has no preview/sleep cost.
        repeat(240) {
          compose.mainClock.advanceTimeByFrame()
          Thread.sleep(16)
        }
      }
      compose.mainClock.autoAdvance = true
    }
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private fun visualPreview(): RecordPreview {
    val names = listOf("アロナ", "プラナ", "安倍晋三AI")
    val slots = listOf("participant-a", "participant-b", "participant-c")
    val votes = names.mapIndexed { index, name ->
      val candidate = if (index == 1) 2 else 1
      RecordVote(name, names[candidate], "一緒に楽しめる提案だと思いました。\n".repeat(30),
        names.indices.filter { it != index }.map { other ->
          RecordAssessment(names[other], 4, 5, 4, 5, 4,
            "相手の提案の良さを活かせています。", 22, slots[other])
        },
        slots[index], slots[candidate])
    }
    return RecordPreview("休日に3人で小さなカフェを開くなら、どんな役割分担にしますか？",
      "## 小さなカフェで、ひと息\n\nそれぞれの得意なことを活かして、無理なく楽しみましょう。",
      "プラナ", names.mapIndexed { index, name ->
        RecordOpinion(name, "役割を分けて楽しむ", "まずは小さな規模で試してみたいです。",
          "3人でできること", "接客、準備、休憩を交代しながら楽しみましょう。", slots[index])
      }, RecordVoting(votes, names.mapIndexed { index, name ->
        RecordVoteCount(name, votes.count { it.candidateSlot == slots[index] }, slots[index]) },
        VoteDecisionMethod.MAJORITY, false),
      "一緒に過ごせる時間を大切にしたいです。", listOf("飲み物の候補を決める"),
      listOf("休憩時間を確保する"), RecordAffection(RecordAffectionStatus.APPLIED, listOf(
        RecordAffectionChange(names[0], 995, 50, 5, 1000, slots[0]),
        RecordAffectionChange(names[1], 200, -15, -15, 185, slots[1]),
        RecordAffectionChange(names[2], 1000, 10, 0, 1000, slots[2]))),
      winnerSlot = slots[1])
  }
}
