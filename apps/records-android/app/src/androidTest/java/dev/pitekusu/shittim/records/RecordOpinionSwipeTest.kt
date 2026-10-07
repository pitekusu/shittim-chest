package dev.pitekusu.shittim.records

import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.isSelected
import androidx.compose.ui.test.hasStateDescription
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
@ScreenTest
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

  private fun stage(final: Boolean) = compose.onNode(isSelected() and hasStateDescription(compose.activity.getString(
    if (final) R.string.record_final_proposal else R.string.record_initial_opinion)))

  private fun assertSelection(person: Int, final: Boolean, name: String = names[person]) {
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    compose.onNodeWithTag("opinion-person-$person").assertIsSelected()
      .assertTextContains(name)
      .assertTextContains(compose.activity.getString(
        if (final) R.string.record_final_proposal else R.string.record_initial_opinion))
    stage(final).assertExists()
    stage(!final).assertDoesNotExist()
  }

  private fun assertAnswer(person: Int, final: Boolean) {
    val title = "${names[person]}の${if (final) "最終案" else "初回意見"}"
    compose.waitUntil(5_000) { compose.onNodeWithText(title).isDisplayed() }
    compose.waitForIdle()
    assertSelection(person, final)
  }

  private fun swipeAnswer(forward: Boolean) {
    compose.onNodeWithTag("detail-pager").performTouchInput {
      if (forward) swipeLeft() else swipeRight()
    }
    compose.waitForIdle()
  }

  @Test fun shuffledPersonasReadInOrderAndReachVotingWithoutLoopingAtTheFirstAnswer() {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(true) {
      RecordDetailScreen(RecordPreviewState.Ready(preview()), "swipe-sample", {}, motionAllowed = false)
    } } }
    // A new visit starts with Arona's initial answer, regardless of winner or response order.
    assertAnswer(0, false)
    compose.onNodeWithTag("opinion-pager").assertExists()
    swipeAnswer(forward = false)
    assertAnswer(0, false)
    for (step in 1..5) {
      swipeAnswer(forward = true)
      assertAnswer(step / 2, step % 2 == 1)
    }
    swipeAnswer(forward = true)
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    compose.onNodeWithText("この記録には投票データがありません。").assertExists()
    swipeAnswer(forward = false)
    assertAnswer(2, true)
    for (step in 4 downTo 0) {
      swipeAnswer(forward = false)
      assertAnswer(step / 2, step % 2 == 1)
    }
    swipeAnswer(forward = false)
    assertAnswer(0, false)
  }

  @Test fun personaTapsToggleTheSelectedAnswerAndReopeningTheSameRecordStartsWithArona() {
    val open = mutableStateOf(true)
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      if (open.value) RecordDetailScreen(RecordPreviewState.Ready(preview()), "swipe-sample", {}, motionAllowed = false)
    } } }
    assertAnswer(0, false)
    stage(false).performClick()
    assertAnswer(0, true)
    // The selected two-line persona button toggles between the two answers.
    compose.onNodeWithTag("opinion-person-0").performClick()
    assertAnswer(0, false)
    compose.onNodeWithTag("opinion-person-1").performClick()
    assertAnswer(1, false)
    swipeAnswer(forward = true)
    assertAnswer(1, true)
    compose.onNodeWithTag("opinion-person-1").performClick()
    assertAnswer(1, false)
    stage(false).performClick()
    assertAnswer(1, true)
    compose.onNodeWithTag("opinion-person-2").performClick()
    assertAnswer(2, false)
    stage(false).performClick()
    assertAnswer(2, true)
    swipeAnswer(forward = true)
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    swipeAnswer(forward = false)
    assertAnswer(2, true)
    stage(true).performClick()
    assertAnswer(2, false)
    compose.runOnIdle { open.value = false }
    compose.onNodeWithTag("detail-pager").assertDoesNotExist()
    compose.runOnIdle { open.value = true }
    assertAnswer(0, false)
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

  @Test fun refreshAndRestoreKeepTheCurrentReaderButChangingAnswersReturnsToTheTop() {
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
    stage(false).performClick()
    assertAnswer(2, true)
    readPartway(2, true)
    stage(true).performClick()
    assertAnswer(2, false)
    assertEquals(0f, readingPosition(), 0.01f)
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
    stage(false).performClick()
    assertAnswer(2, true)
    assertEquals(0f, readingPosition(), 0.01f)
    swipeAnswer(forward = false)
    assertAnswer(2, false)
    assertEquals(0f, readingPosition(), 0.01f)
  }

  @Test fun aLegacyRecordWithOnePersonaHasTwoAnswersThenVotingAndABoundedFirstPage() {
    val legacy = RecordPreview("古い架空の議題", "架空の結論", "アロナ", opinions = listOf(
      RecordOpinion("プラナ", "昔の初回意見", "昔の初回本文", "昔の最終案", "昔の最終本文")))
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      RecordDetailScreen(RecordPreviewState.Ready(legacy), "legacy-sample", {}, motionAllowed = false)
    } } }
    fun assertLegacy(final: Boolean) {
      compose.waitUntil(5_000) {
        compose.onNodeWithText(if (final) "昔の最終案" else "昔の初回意見").isDisplayed()
      }
      assertSelection(0, final, "プラナ")
      compose.onNodeWithTag("opinion-person-1").assertDoesNotExist()
      compose.onNodeWithTag("opinion-person-2").assertDoesNotExist()
    }
    assertLegacy(false)
    swipeAnswer(forward = false)
    assertLegacy(false)
    swipeAnswer(forward = true)
    assertLegacy(true)
    swipeAnswer(forward = true)
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    swipeAnswer(forward = false)
    assertLegacy(true)
    swipeAnswer(forward = false)
    assertLegacy(false)
    swipeAnswer(forward = false)
    assertLegacy(false)
  }
}
