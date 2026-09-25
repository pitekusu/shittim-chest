package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordPreviewPanelTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun oneRecordAndRetryAreVisibleWithoutShowingOldBodyInErrorState() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(
      RecordPreview("架空の議題", "架空の結論", "アロナ")))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordPreviewPanel(state.value, events::add) } }
    }
    compose.onNodeWithText("架空の議題").assertIsDisplayed()
    compose.onNodeWithText("アロナ").assertIsDisplayed()
    compose.onNodeWithText("架空の結論").assertIsDisplayed()
    compose.runOnIdle { state.value = RecordPreviewState.Error(RecordReadFailure.UNAVAILABLE) }
    compose.onNodeWithText("架空の議題").assertDoesNotExist()
    compose.onNodeWithText(label(R.string.record_retry)).performClick()
    assertEquals(BootstrapScreen.Event.RetryRecord, events.single())
  }

  @Test
  fun markdownBodyShowsAllThreeOpinionsAndLongText() {
    val longProposal = "長文の提案です。".repeat(80)
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "要約${index + 1}", "**強調** と [資料](https://example.com)\n\n- 箇条書き",
        "案${index + 1}", longProposal)
    }
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        RecordPreviewPanel(RecordPreviewState.Ready(
          RecordPreview("架空の議題", "架空の結論", "アロナ", opinions)), {})
      } }
    }
    compose.onNodeWithText("3人の意見").assertExists()
    for (name in listOf("アロナ", "プラナ", "安倍晋三AI")) {
      compose.onAllNodesWithText(name).onFirst().assertExists()
    }
    for (text in listOf("強調", "資料", "箇条書き", longProposal)) {
      compose.onAllNodesWithText(text, substring = true).onFirst().assertExists()
    }
  }

  @Test
  fun votingShowsSavedBallotsAndExpandsAssessmentDetails() {
    val score = RecordAssessment("プラナ", 5, 4, 3, 2, 1, "プラナらしい視点です。", 67)
    val voting = RecordVoting(
      listOf(RecordVote("アロナ", "プラナ", "理由を話します。", listOf(score,
        RecordAssessment("安倍晋三AI", 1, 2, 3, 4, 5, "別の視点です。", 53)))),
      listOf(RecordVoteCount("アロナ", 0), RecordVoteCount("プラナ", 1),
        RecordVoteCount("安倍晋三AI", 0)),
      VoteDecisionMethod.COMPOSITE_SCORE, true)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordVotingPanel(voting) } }
    }
    compose.onNodeWithText("アロナ → プラナ").assertExists()
    compose.onNodeWithText("プラナ：1票").assertExists()
    compose.onNodeWithText("同票のため、5項目の総合評価で決定しました。").assertExists()
    compose.onNodeWithText("採点の内訳を見る").performClick()
    compose.onNodeWithText("プラナ：67 / 100点").assertExists()
    compose.onNodeWithText("プラナらしい視点です。").assertExists()
  }

  @Test
  fun affectionSeparatesQuestionScoreFromRealChangeAndOptionalDecisionText() {
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 995, 50, 5, 1000),
      RecordAffectionChange("プラナ", 500, -20, -20, 480),
      RecordAffectionChange("安倍晋三AI", 100, 0, 0, 100),
    ))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        RecordPreviewPanel(RecordPreviewState.Ready(RecordPreview(
          "架空の議題", "架空の結論", "アロナ", victoryMessage = "ありがとう！",
          actions = listOf("まず確認する"), caveats = listOf("無理をしない"), affection = affection,
        )), {})
      } }
    }
    for (text in listOf("ありがとう！", "• まず確認する", "• 無理をしない",
      "質問評価：+50点", "親愛度：995 → 1000", "実増減：+5点", "質問評価：-20点")) {
      compose.onNodeWithText(text).assertExists()
    }
  }

  @Test
  fun oldAndUnavailableAffectionDoNotShowMadeUpScores() {
    val current = mutableStateOf<RecordAffection?>(null)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordAffectionPanel(current.value) } }
    }
    compose.onNodeWithText("この記録には親愛度データがありません。").assertExists()
    compose.runOnIdle {
      current.value = RecordAffection(RecordAffectionStatus.UNAVAILABLE, listOf(
        RecordAffectionChange("アロナ", 500, null, 0, 500),
      ))
    }
    compose.onNodeWithText("質問の評価を完了できなかったため、親愛度は変更されませんでした。").assertExists()
    compose.onNodeWithText("質問評価：未評価").assertExists()
    compose.onNodeWithText("実増減：0点").assertExists()
    compose.onNodeWithText("この記録には親愛度データがありません。").assertDoesNotExist()
  }

  @Test
  fun onlyAbsoluteHttpsLinksCanLeaveTheRecord() {
    assertTrue(allowedRecordLink("https://example.com/article?q=1"))
    for (url in listOf("http://example.com", "javascript:alert(1)", "intent://example.com",
      "file:///etc/passwd", "content://other.app/private", "//example.com",
      "https://user:pass" + "@" + "example.com", "https://")) {
      assertFalse(url, allowedRecordLink(url))
    }
  }

  @Test
  fun markdownLinksApplyTheAppPolicyBeforeOpening() {
    val opened = mutableListOf<String>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        CompositionLocalProvider(LocalUriHandler provides object : UriHandler {
          override fun openUri(uri: String) { opened += uri }
        }) {
          RecordMarkdown("[安全なリンク](https://example.com)\n\n[拒否するリンク](intent://other.app)")
        }
      } }
    }
    // Markdown parsing is asynchronous, especially on CI's cold emulator.
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("安全なリンク", substring = true).fetchSemanticsNodes().isNotEmpty() &&
        compose.onAllNodesWithText("拒否するリンク", substring = true).fetchSemanticsNodes().isNotEmpty()
    }
    compose.onAllNodesWithText("安全なリンク", substring = true).onFirst().performClick()
    compose.onAllNodesWithText("拒否するリンク", substring = true).onFirst().performClick()
    compose.runOnIdle { assertEquals(listOf("https://example.com"), opened) }
  }

  private fun label(id: Int): String = compose.activity.getString(id)
}
