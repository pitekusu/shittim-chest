package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeRight
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordDetailScreenTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private fun preview() = RecordPreview("架空の議題", "架空の結論", "アロナ")

  @Test fun bottomNavigationAndSwipeKeepTheirSelectionDuringRefresh() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(preview()))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(true) { RecordDetailScreen(state.value, "sample", {}) }
    } }
    compose.onNodeWithTag("detail-section-Result").assertIsSelected()
    compose.onNodeWithTag("detail-section-Voting").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には投票データがありません。").isDisplayed() }
    compose.runOnIdle { state.value = RecordPreviewState.Ready(preview(), updating = true) }
    compose.onNodeWithTag("detail-section-Voting").assertIsSelected()
    compose.onNodeWithTag("detail-pager").performTouchInput { swipeRight() }
    compose.waitUntil(5_000) { compose.onNodeWithText("この記録には意見データがありません。").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun switchingPagesRestoresResultReadingPositionAndAnotherRecordStartsAtResult() {
    val id = mutableStateOf("first")
    val state = RecordPreviewState.Ready(RecordPreview("架空の議題", "結論です。\n\n".repeat(70),
      "アロナ", actions = listOf("末尾の実行案")))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(false) { RecordDetailScreen(state, id.value, {}) }
    } }
    compose.waitUntil(10_000) { compose.onNodeWithText("アロナ").isDisplayed() }
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
    compose.onNodeWithTag("detail-section-Result").assertIsSelected()
  }
}
