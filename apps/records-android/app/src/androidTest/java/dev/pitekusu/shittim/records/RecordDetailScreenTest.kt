package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.isSelected
import androidx.compose.ui.test.hasStateDescription
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeRight
import androidx.compose.ui.test.swipeUp
import androidx.compose.ui.test.then
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import java.io.File
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import android.view.KeyEvent
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import androidx.compose.ui.text.TextLayoutResult
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordDetailScreenTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private fun preview() = RecordPreview("架空の議題", "架空の結論", "アロナ")

  @Test fun bottomNavigationAndSwipeKeepTheirSelectionDuringRefresh() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(preview()))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(true) { RecordDetailScreen(state.value, "sample", {}) }
    } }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    val navigation = RecordDetailSection.entries.map { section ->
      compose.onNodeWithTag("detail-section-${section.name}").fetchSemanticsNode().boundsInRoot.left
    }
    assertTrue(navigation.zipWithNext().all { (left, right) -> left < right })
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には投票データがありません。").isDisplayed() }
    compose.runOnIdle { state.value = RecordPreviewState.Ready(preview(), updating = true) }
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    compose.onNodeWithTag("detail-pager").performTouchInput { swipeRight() }
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には意見データがありません。").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun questionStaysFixedWhileTabsSlideAndThePageChanges() {
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(false) { RecordDetailScreen(RecordPreviewState.Ready(preview()), "sample", {}) }
    } }
    val questionBounds = compose.onNodeWithTag("detail-question-open").fetchSemanticsNode().boundsInRoot
    compose.mainClock.autoAdvance = false
    try {
      compose.onNodeWithTag("detail-section-Voting").performClick()
      compose.mainClock.advanceTimeBy(96)
      val duringSlide = compose.onNodeWithTag("detail-question-open").fetchSemanticsNode().boundsInRoot
      assertEquals(questionBounds.left, duringSlide.left, 1f)
      assertEquals(questionBounds.top, duringSlide.top, 1f)
      assertEquals(questionBounds.width, duringSlide.width, 1f)
      assertEquals(questionBounds.height, duringSlide.height, 1f)
      compose.mainClock.advanceTimeBy(1_000)
    } finally {
      compose.mainClock.autoAdvance = true
    }
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    compose.onNodeWithTag("detail-pager").performTouchInput { swipeRight() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    assertEquals(questionBounds, compose.onNodeWithTag("detail-question-open").fetchSemanticsNode().boundsInRoot)
  }

  @Test fun switchingPagesRestoresResultReadingPositionAndAnotherRecordStartsAtOpinions() {
    val id = mutableStateOf("first")
    val state = RecordPreviewState.Ready(RecordPreview("架空の議題", "結論です。\n\n".repeat(70),
      "アロナ", actions = listOf("末尾の実行案")))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(false) { RecordDetailScreen(state, id.value, {}) }
    } }
    compose.onNodeWithTag("detail-section-Result").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("アロナ").isDisplayed() }
    // Markdown is parsed asynchronously. The winner can appear before the long body is laid out.
    compose.waitUntil(10_000) {
      compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
        .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
    }
    compose.onNodeWithTag("record-detail-content").performTouchInput { swipeUp() }
    val position = compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue(position > 0f)
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には投票データがありません。").isDisplayed() }
    compose.onNodeWithTag("detail-section-Result").performClick()
    compose.waitForIdle()
    val restored = compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertEquals(position, restored, 0.01f)
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には親愛度データがありません。").isDisplayed() }
    compose.runOnIdle { id.value = "second" }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun questionSheetAndResultDisclosuresKeepEverySavedFieldReachable() {
    val state = RecordPreviewState.Ready(RecordPreview("架空の長い議題：".repeat(40),
      "架空の結論", "アロナ", victoryMessage = "一緒に楽しみましょう！",
      actions = listOf("散歩する"), caveats = listOf("天気を確認する")))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(true) { RecordDetailScreen(state, "sample", {}) }
    } }
    compose.onNodeWithTag("detail-section-Result").performClick()
    compose.onNodeWithText("• 散歩する").assertDoesNotExist()
    compose.onNodeWithTag("detail-actions-expand").performScrollTo().performClick()
    compose.onNodeWithText("• 散歩する").assertExists()
    compose.onNodeWithTag("detail-question-full").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
    compose.waitUntil(5_000) {
      !compose.onNodeWithTag("detail-question-sheet").isDisplayed()
    }
    compose.onNodeWithTag("detail-section-Result").assertIsSelected()
    compose.onNodeWithTag("detail-actions-expand").performScrollTo()
    compose.onNodeWithText("• 散歩する").assertExists()
    compose.onNodeWithTag("detail-actions-expand").performClick()
    compose.onNodeWithText("• 散歩する").assertDoesNotExist()
    compose.onNodeWithTag("detail-caveats-expand").performScrollTo().performClick()
    compose.onNodeWithText("• 天気を確認する").assertExists()
  }

  @Test fun longQuestionStartsWithFiveLinesAndFullTextKeepsTheLastParagraphReachable() {
    val question = "長い架空の議題について、順番に読み進められるように考えてください。\n".repeat(30) +
      "\n議題の最後の一行です。"
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(false) { RecordDetailScreen(RecordPreviewState.Ready(
        RecordPreview(question, "結論", "アロナ")), "long-question", {}) }
    } }
    val layouts = mutableListOf<TextLayoutResult>()
    compose.onNodeWithTag("detail-question-text", useUnmergedTree = true)
      .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
    assertEquals(question, layouts.single().layoutInput.text.text)
    assertEquals(5, layouts.single().lineCount)
    assertTrue(layouts.single().isLineEllipsized(4))
    compose.onNodeWithTag("detail-question-scroll").assertDoesNotExist()
    compose.onNodeWithText("この記録には意見データがありません。").assertIsDisplayed()
    captureQuestion("detail-question-collapsed-light")
    compose.onNodeWithTag("detail-question-full").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    captureQuestion("detail-question-full-light")
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("議題の最後の一行です。").fetchSemanticsNodes().isNotEmpty()
    }
    compose.onNodeWithText("議題の最後の一行です。").performScrollTo().assertIsDisplayed()
    // The close action remains accessible at the end of a long question.
    compose.onNodeWithTag("detail-question-close").assertIsDisplayed().performClick()
    compose.waitUntil(5_000) { !compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun largeTextKeepsTheCollapsedQuestionAndFullTextActionsReachable() {
    val question = "長い架空の議題について、休日に楽しむ散歩と読書の計画を考えてください。\n".repeat(20) +
      "\n議題の最後の一行です。"
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(320.dp, 640.dp)) then
        DeviceConfigurationOverride.FontScale(2f)) {
        ShittimTheme(true) { RecordDetailScreen(RecordPreviewState.Ready(
          RecordPreview(question, "結論", "アロナ")), "large-question", {}) }
      }
    } }
    val card = compose.onNodeWithTag("detail-question-open").fetchSemanticsNode().boundsInRoot
    val text = compose.onNodeWithTag("detail-question-text", useUnmergedTree = true)
    val bounds = text.fetchSemanticsNode().boundsInRoot
    val layouts = mutableListOf<TextLayoutResult>()
    text.performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
    assertEquals(question, layouts.single().layoutInput.text.text)
    assertTrue(layouts.single().lineCount in 1..5)
    assertTrue(bounds.height > 0f && bounds.top >= card.top && bounds.bottom <= card.bottom)
    compose.onNodeWithTag("detail-question-full").assertIsDisplayed()
    compose.onNodeWithText("この記録には意見データがありません。").assertIsDisplayed()
    compose.onNodeWithTag("detail-section-Opinions").assertIsDisplayed()
    captureQuestion("detail-question-collapsed-large-text")
    compose.onNodeWithTag("detail-question-full").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("議題の最後の一行です。").fetchSemanticsNodes().isNotEmpty()
    }
    compose.onNodeWithText("議題の最後の一行です。").performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("detail-question-close").assertIsDisplayed()
    captureQuestion("detail-question-full-large-text-end")
    InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_BACK)
    compose.waitUntil(5_000) { !compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun narrowLargeTextPersonaButtonsKeepBothAnswerStagesReachable() {
    val names = listOf("アロナ", "プラナ", "安倍晋三AI")
    val opinions = names.mapIndexed { index, name ->
      RecordOpinion(name, "初回の要約$index", "架空の初回本文$index",
        "最終の要約$index", "架空の最終本文$index")
    }
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(320.dp, 640.dp)) then
        DeviceConfigurationOverride.FontScale(2f)) {
        ShittimTheme(true) { RecordDetailScreen(RecordPreviewState.Ready(
          RecordPreview("架空の議題", "結論", "アロナ", opinions)), "large-buttons", {}) }
      }
    } }
    for (index in opinions.indices) {
      val button = compose.onNodeWithTag("opinion-person-$index").assertIsDisplayed()
      if (index != 0) button.performClick()
      button.assertIsSelected().performClick()
      compose.onNode(isSelected() and hasStateDescription(
        compose.activity.getString(R.string.record_final_proposal))).assertExists()
      compose.waitUntil(10_000) { compose.onAllNodesWithText("最終の要約$index").fetchSemanticsNodes().isNotEmpty() }
      compose.onNodeWithText("最終の要約$index").performScrollTo().assertIsDisplayed()
      compose.onNodeWithTag("detail-section-Opinions").assertIsDisplayed()
    }
    captureQuestion("detail-opinions-large-text")
  }

  private fun captureQuestion(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  @Test fun opinionsStartWithAronaAndResetOnAnswerChangesButKeepTheCurrentPositionDuringRefresh() {
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "初回の要約$index", "初回の本文$index\n\n".repeat(60),
        "最終案の題名$index", "最終案の本文$index\n\n".repeat(60))
    }
    val preview = RecordPreview("架空の議題", "結論", "プラナ", opinions)
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(preview))
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(true) { RecordDetailScreen(state.value, "sample", {}) }
    } }
    compose.onNodeWithTag("detail-section-Opinions").performClick()
    compose.waitForIdle()
    compose.onNodeWithTag("opinion-person-0").assertIsSelected()
    // Async Markdown parsing is independent of Compose idleness; await the real answer list.
    compose.waitUntil(10_000) { compose.onNodeWithText("初回の要約0").isDisplayed() }
    compose.onNode(isSelected() and hasStateDescription(compose.activity.getString(R.string.record_initial_opinion))).performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("最終案の題名0").isDisplayed() }
    compose.onNode(isSelected() and hasStateDescription(compose.activity.getString(R.string.record_final_proposal))).performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("初回の要約0").isDisplayed() }
    compose.onNodeWithTag("record-detail-content").performTouchInput { swipeUp() }
    val position = compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue(position > 0f)
    compose.onNodeWithTag("opinion-person-1").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("初回の要約1").isDisplayed() }
    compose.waitForIdle()
    assertEquals(0f, compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value(), 0.01f)
    compose.onNodeWithTag("opinion-person-0").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("初回の要約0").isDisplayed() }
    compose.waitForIdle()
    assertEquals(0f, compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value(), 0.01f)
    compose.onNodeWithTag("record-detail-content").performTouchInput { swipeUp() }
    val currentPosition = compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue(currentPosition > 0f)
    compose.runOnIdle { state.value = RecordPreviewState.Ready(preview, updating = true) }
    compose.waitForIdle()
    val restored = compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertEquals(currentPosition, restored, 0.01f)
    compose.onNodeWithTag("opinion-person-0").assertIsSelected()
  }

  @Test fun swipingFromResultReadsAllAffectionsInOrderAndStopsAtTheLastPersona() {
    val names = listOf("アロナ", "プラナ", "安倍晋三AI")
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, names.mapIndexed { index, name ->
      RecordAffectionChange(name, 500 + index * 100, 10, 10, 510 + index * 100,
        listOf("participant-a", "participant-b", "participant-c")[index])
    })
    val state = RecordPreviewState.Ready(RecordPreview("架空の議題", "架空の結論", "プラナ",
      affection = affection, winnerSlot = "participant-b"))
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(true) { RecordDetailScreen(state, "affection-swipe", {}, motionAllowed = false) }
    } }
    fun assertPerson(index: Int) {
      compose.waitForIdle()
      compose.onNodeWithTag("detail-section-Affection").assertIsSelected()
      compose.onNodeWithTag("affection-person-$index").assertIsSelected()
      compose.onNodeWithText("親愛度：${500 + index * 100} → ${510 + index * 100}").assertExists()
    }
    fun swipe(forward: Boolean) {
      compose.onNodeWithTag("detail-pager").performTouchInput {
        if (forward) swipeLeft() else swipeRight()
      }
      compose.waitForIdle()
    }
    // Direct tab navigation keeps the established winner default.
    compose.onNodeWithTag("detail-section-Affection").performClick()
    assertPerson(1)
    compose.onNodeWithTag("detail-section-Result").performClick()
    swipe(forward = true)
    assertPerson(0)
    compose.onNodeWithTag("affection-pager").assertExists()
    swipe(forward = true)
    assertPerson(1)
    swipe(forward = true)
    assertPerson(2)
    swipe(forward = true)
    assertPerson(2)
    for (index in 1 downTo 0) {
      swipe(forward = false)
      assertPerson(index)
    }
    swipe(forward = false)
    compose.onNodeWithTag("detail-section-Result").assertIsSelected()
  }
}
