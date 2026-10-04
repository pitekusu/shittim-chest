package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeRight
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import android.view.KeyEvent
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    compose.onNodeWithTag("detail-question-open").performClick()
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

  @Test fun longQuestionScrollsIndependentlyWithoutEllipsisOrHidingTheAnswer() {
    val question = "長い架空の議題について、順番に読み進められるように考えてください。\n".repeat(30)
    compose.activityRule.scenario.onActivity { it.setContent {
      ShittimTheme(false) { RecordDetailScreen(RecordPreviewState.Ready(
        RecordPreview(question, "結論", "アロナ")), "long-question", {}) }
    } }
    val layouts = mutableListOf<TextLayoutResult>()
    compose.onNodeWithTag("detail-question-text", useUnmergedTree = true)
      .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
    assertEquals(question, layouts.single().layoutInput.text.text)
    assertTrue(layouts.single().lineCount > 2)
    assertFalse(layouts.single().hasVisualOverflow)
    assertFalse((0 until layouts.single().lineCount).any { layouts.single().isLineEllipsized(it) })
    compose.onNodeWithText("この記録には意見データがありません。").assertIsDisplayed()
    val questionScroll = compose.onNodeWithTag("detail-question-scroll", useUnmergedTree = true)
    assertTrue(questionScroll.fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f)
    questionScroll.performTouchInput { swipeUp() }
    val position = questionScroll.fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue(position > 0f)
    compose.onNodeWithTag("detail-section-Voting").performClick()
    assertEquals(position, questionScroll.fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value(), 0.01f)
    compose.onNodeWithTag("detail-section-Opinions").performClick()
    compose.onNodeWithText("この記録には意見データがありません。").assertIsDisplayed()
    assertEquals(position, questionScroll.fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value(), 0.01f)
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
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("最終案の題名0").isDisplayed() }
    compose.onNodeWithText(compose.activity.getString(R.string.record_initial_opinion)).performClick()
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
