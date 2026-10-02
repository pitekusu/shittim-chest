package dev.pitekusu.shittim.records

import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeRight
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordOpinionSwipeTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  private val names = listOf("アロナ", "プラナ", "安倍晋三AI")
  private val slots = listOf("participant-a", "participant-b", "participant-c")

  private fun preview(longAnswers: Boolean = false): RecordPreview {
    val ordered = names.mapIndexed { index, name ->
      val count = if (longAnswers) 40 else 1
      RecordOpinion(name, "${name}の初回意見",
        "架空の初回本文$index。順番に読み進めるための説明です。\n\n".repeat(count) + "末尾の初回本文$index",
        "${name}の最終案",
        "架空の最終本文$index。順番に読み進めるための説明です。\n\n".repeat(count) + "末尾の最終本文$index",
        participantSlot = slots[index])
    }
    // The server is not required to send participant-a/b/c order.
    return RecordPreview("架空の議題", "架空の結論", "プラナ",
      opinions = listOf(ordered[2], ordered[0], ordered[1]), winnerSlot = "participant-b")
  }

  private fun stage(final: Boolean) = compose.onNodeWithText(compose.activity.getString(
    if (final) R.string.record_final_proposal else R.string.record_initial_opinion))

  private fun assertSelection(person: Int, final: Boolean) {
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    compose.onNodeWithTag("opinion-person-$person").assertIsSelected()
    stage(final).assertIsOn()
    stage(!final).assertIsOff()
  }

  private fun assertAnswer(person: Int, final: Boolean) {
    val title = "${names[person]}の${if (final) "最終案" else "初回意見"}"
    compose.waitUntil(5_000) { compose.onNodeWithText(title).isDisplayed() }
    assertSelection(person, final)
  }

  private fun swipeAnswer(forward: Boolean) {
    compose.onNodeWithTag("opinion-pager").performTouchInput {
      if (forward) swipeLeft() else swipeRight()
    }
    compose.waitForIdle()
  }

  @Test fun shuffledPersonasLoopThroughAllSixAnswersInBothDirectionsWithoutLeavingOpinions() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(true) {
      RecordDetailScreen(RecordPreviewState.Ready(preview()), "swipe-sample", {}, motionAllowed = false)
    } } }
    // Winner starts at their initial answer, not the first item in the response.
    var step = 2
    assertAnswer(step / 2, step % 2 == 1)
    repeat(6) {
      swipeAnswer(forward = true)
      step = (step + 1) % 6
      assertAnswer(step / 2, step % 2 == 1)
    }
    repeat(6) {
      swipeAnswer(forward = false)
      step = (step + 5) % 6
      assertAnswer(step / 2, step % 2 == 1)
    }
    // Cross both ends of the SDK's bounded virtual window, not only a middle cycle.
    val answers = compose.onNodeWithTag("opinion-pager")
    answers.performSemanticsAction(SemanticsActions.ScrollToIndex) { it(6) }
    assertAnswer(0, false)
    swipeAnswer(forward = false)
    assertAnswer(2, true)
    val pageCount = answers.fetchSemanticsNode().config[SemanticsProperties.CollectionInfo].columnCount
    answers.performSemanticsAction(SemanticsActions.ScrollToIndex) { it(pageCount - 7) }
    assertAnswer(2, true)
    swipeAnswer(forward = true)
    assertAnswer(0, false)
  }

  @Test fun tappingAnyPersonaResetsToInitialAndStageButtonsStayInSyncWithSwipes() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      RecordDetailScreen(RecordPreviewState.Ready(preview()), "swipe-sample", {}, motionAllowed = false)
    } } }
    assertAnswer(1, false)
    swipeAnswer(forward = true)
    assertAnswer(1, true)
    // Tapping the currently selected persona must also reset the answer stage.
    compose.onNodeWithTag("opinion-person-1").performClick()
    assertAnswer(1, false)
    stage(true).performClick()
    assertAnswer(1, true)
    compose.onNodeWithTag("opinion-person-2").performClick()
    assertAnswer(2, false)
    stage(true).performClick()
    assertAnswer(2, true)
    swipeAnswer(forward = true)
    assertAnswer(0, false)
    swipeAnswer(forward = false)
    assertAnswer(2, true)
    stage(false).performClick()
    assertAnswer(2, false)
  }

  private fun readingPosition(): Float = compose.onNodeWithTag("record-detail-content")
    .fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value()

  private fun readPartway(person: Int, final: Boolean): Float {
    val tail = "末尾の${if (final) "最終" else "初回"}本文$person"
    // Markdown parsing is asynchronous; do not capture a position on its empty placeholder.
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText(tail, substring = true).fetchSemanticsNodes().isNotEmpty() &&
        compose.onNodeWithTag("record-detail-content").fetchSemanticsNode()
          .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
    }
    compose.onNodeWithTag("record-detail-content").performTouchInput {
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    return readingPosition().also { assertTrue(it > 0f) }
  }

  @Test fun refreshingAndRestoringRetainTheStageAndEachAnswersIndependentReadingPosition() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(preview(longAnswers = true)))
    val restoration = StateRestorationTester(compose)
    // MainActivity installs a normal UI first; give the standard helper an empty host.
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent { ShittimTheme(true) {
      RecordDetailScreen(state.value, "swipe-sample", {}, motionAllowed = false)
    } }
    compose.onNodeWithTag("opinion-person-2").performClick()
    stage(true).performClick()
    assertAnswer(2, true)
    val finalPosition = readPartway(2, true)
    stage(false).performClick()
    assertAnswer(2, false)
    val initialPosition = readPartway(2, false)
    compose.runOnIdle {
      state.value = RecordPreviewState.Ready(preview(longAnswers = true), updating = true)
    }
    assertSelection(2, false)
    assertEquals(initialPosition, readingPosition(), 0.01f)
    restoration.emulateSavedInstanceStateRestore()
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("末尾の初回本文2", substring = true).fetchSemanticsNodes().isNotEmpty()
    }
    assertSelection(2, false)
    assertEquals(initialPosition, readingPosition(), 0.01f)
    stage(true).performClick()
    compose.waitForIdle()
    assertSelection(2, true)
    assertEquals(finalPosition, readingPosition(), 0.01f)
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には投票データがありません。").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").performClick()
    compose.waitForIdle()
    assertSelection(2, true)
    assertEquals(finalPosition, readingPosition(), 0.01f)
  }

  @Test fun aLegacyRecordWithOneAvailablePersonaLoopsOnlyTheirTwoAnswers() {
    val legacy = RecordPreview("古い架空の議題", "架空の結論", "アロナ", opinions = listOf(
      RecordOpinion("プラナ", "昔の初回意見", "昔の初回本文", "昔の最終案", "昔の最終本文")))
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      RecordDetailScreen(RecordPreviewState.Ready(legacy), "legacy-sample", {}, motionAllowed = false)
    } } }
    fun assertLegacy(final: Boolean) {
      compose.waitUntil(5_000) {
        compose.onNodeWithText(if (final) "昔の最終案" else "昔の初回意見").isDisplayed()
      }
      assertSelection(0, final)
      compose.onNodeWithTag("opinion-person-1").assertDoesNotExist()
      compose.onNodeWithTag("opinion-person-2").assertDoesNotExist()
    }
    assertLegacy(false)
    for (forward in listOf(true, false)) {
      swipeAnswer(forward)
      assertLegacy(true)
      swipeAnswer(forward)
      assertLegacy(false)
    }
  }
}
