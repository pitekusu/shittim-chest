package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalUriHandler
import androidx.compose.ui.platform.UriHandler
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onFirst
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
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
      activity.setContent { ShittimTheme(false) { RecordDetailScreen(state.value, "sample", events::add) } }
    }
    compose.onNodeWithTag("detail-section-Result").performClick()
    // Markdown parsing is asynchronous; wait for both bodies before checking the ready screen.
    compose.waitUntil(10_000) {
      compose.onNodeWithText("架空の議題").isDisplayed() &&
        compose.onNodeWithText("架空の結論").isDisplayed()
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
  fun markdownBodyKeepsEveryPersonAndAnswerStageReachable() {
    val longProposal = "長文の提案です。".repeat(80)
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "要約${index + 1}", "**強調** と [資料](https://example.com)\n\n- 箇条書き",
        "案${index + 1}", longProposal)
    }
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        RecordDetailScreen(RecordPreviewState.Ready(
          RecordPreview("架空の議題", "架空の結論", "アロナ", opinions)), "sample", {})
      } }
    }
    compose.onNodeWithTag("detail-section-Opinions").performClick()
    compose.waitForIdle()
    for (index in opinions.indices) {
      compose.onNodeWithTag("opinion-person-$index").performClick()
      compose.onNodeWithText(label(R.string.record_final_proposal)).performClick()
      compose.waitUntil(10_000) {
        compose.onAllNodesWithText(longProposal, substring = true).fetchSemanticsNodes().isNotEmpty()
      }
      compose.onNodeWithText("案${index + 1}").assertExists()
      compose.onNodeWithText(label(R.string.record_initial_opinion)).performClick()
      val markdownTexts = listOf("強調", "資料", "箇条書き")
      compose.waitUntil(10_000) {
        markdownTexts.all { text ->
          compose.onAllNodesWithText(text, substring = true).fetchSemanticsNodes().isNotEmpty()
        }
      }
      compose.onNodeWithText("要約${index + 1}").assertExists()
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
      activity.setContent { ShittimTheme(false) { RecordVotingPanel(voting, "プラナ", null) } }
    }
    compose.onNodeWithText("アロナ → プラナ").assertExists()
    compose.onNodeWithText("同票のため、5項目の総合評価で決定しました。").assertExists()
    compose.onNodeWithTag("vote-route-0").performClick()
    compose.onNodeWithText("プラナ：67 / 100点").assertExists()
    compose.onNodeWithText("プラナらしい視点です。").assertExists()
    compose.onNodeWithText("面白さ・魅力：5 / 5").assertExists()
  }

  @Test
  fun votingDiagramShowsAThreeWayCycleAndOpensTheSelectedBallot() {
    val voting = RecordVoting(
      listOf(
        RecordVote("アロナ", "プラナ", "アロナの投票理由", null, "participant-a", "participant-b"),
        RecordVote("プラナ", "安倍晋三AI", "プラナの投票理由", null, "participant-b", "participant-c"),
        RecordVote("安倍晋三AI", "アロナ", "安倍晋三AIの投票理由", null, "participant-c", "participant-a"),
      ),
      listOf(RecordVoteCount("アロナ", 1, "participant-a"),
        RecordVoteCount("プラナ", 1, "participant-b"),
        RecordVoteCount("安倍晋三AI", 1, "participant-c")),
      VoteDecisionMethod.TIE_LOTTERY, true,
    )
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { Column {
        RecordVotingPanel(voting, "アロナ", "participant-a")
      } } }
    }
    capture("voting-graph")
    compose.onNodeWithTag("vote-person-1").performClick()
    compose.onNodeWithText("プラナ → 安倍晋三AI").assertExists()
    compose.onNodeWithText("プラナの投票理由").assertExists()
    compose.onNodeWithText("得票数・総合評価が同じため、抽選で決定しました。").assertExists()
    capture("voting-detail")
  }

  @Test
  fun unknownLegacyParticipantsKeepTheirVoteRoutesReadable() {
    val voting = RecordVoting(
      listOf(RecordVote("未知A", "未知B", "理由A", null), RecordVote("未知B", "未知C", "理由B", null),
        RecordVote("未知C", "未知A", "理由C", null)),
      listOf(RecordVoteCount("未知A", 2), RecordVoteCount("未知B", 1),
        RecordVoteCount("未知C", 0)), VoteDecisionMethod.MAJORITY, false)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordVotingPanel(voting, "未知A", null) } }
    }
    compose.onNodeWithTag("vote-route-0").assertExists()
    compose.onNodeWithText("未知A → 未知B").assertExists()
    for (count in listOf("2票", "1票", "0票")) compose.onNodeWithText(count).assertExists()
    assertTrue(voteParticipantMatches("未知A", null, "未知A", null))
    assertFalse(voteParticipantMatches("未知A", null, "未知B", null))
  }

  @Test
  fun affectionShowsAppliedChangeAndOptionalDecisionTextWithoutQuestionScore() {
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 995, 50, 5, 1000),
      RecordAffectionChange("プラナ", 500, -20, -20, 480),
      RecordAffectionChange("安倍晋三AI", 100, 0, 0, 100),
    ))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        RecordDetailScreen(RecordPreviewState.Ready(RecordPreview(
          "架空の議題", "架空の結論", "アロナ", victoryMessage = "ありがとう！",
          actions = listOf("まず確認する"), caveats = listOf("無理をしない"), affection = affection,
        )), "sample", {}, motionAllowed = false)
      } }
    }
    compose.onNodeWithTag("detail-section-Result").performClick()
    compose.onNodeWithTag("detail-actions-expand").performClick()
    compose.onNodeWithTag("detail-caveats-expand").performClick()
    for (text in listOf("ありがとう！", "• まず確認する", "• 無理をしない")) {
      compose.onNodeWithText(text).assertExists()
    }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.onNodeWithText("親愛度：995 → 1000").assertExists()
    compose.onNodeWithText("実増減：+5点").assertExists()
    compose.onNodeWithText("質問評価：+50点").assertDoesNotExist()
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
    compose.onNodeWithText("質問評価：未評価").assertDoesNotExist()
    compose.onNodeWithText("実増減：0点").assertExists()
    compose.onNodeWithText("この記録には親愛度データがありません。").assertDoesNotExist()
  }

  @Test
  fun affectionCardsKeepTheRealDeltaAtTheUpperBoundAndZero() {
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 995, 50, 5, 1_000, "participant-a"),
      RecordAffectionChange("プラナ", 500, -20, -20, 480, "participant-b"),
      RecordAffectionChange("安倍晋三AI", 100, 0, 0, 100, "participant-c"),
    ))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(true) {
        Surface(color = MaterialTheme.colorScheme.background) {
          RecordDetailScreen(RecordPreviewState.Ready(RecordPreview(
            "架空の議題", "結論", "アロナ", affection = affection)),
            "sample", {}, motionAllowed = false)
        }
      } }
    }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitForIdle()
    compose.onNodeWithText("親愛度：995 → 1000").assertExists()
    compose.onNodeWithText("実増減：+5点").assertExists()
    compose.onNodeWithTag("affection-person-1").performClick()
    compose.onNodeWithText("親愛度：500 → 480").assertExists()
    compose.onNodeWithTag("affection-person-2").performClick()
    compose.onNodeWithText("実増減：0点").assertExists()
    capture("affection-cards")
  }

  @Test
  fun largeTextUsesBallotRowsAndKeepsAffectionScoresReadable() {
    val voting = RecordVoting(listOf(
      RecordVote("アロナ", "プラナ", "理由A", null),
      RecordVote("プラナ", "安倍晋三AI", "理由B", null),
      RecordVote("安倍晋三AI", "アロナ", "理由C", null),
    ), listOf(RecordVoteCount("アロナ", 1), RecordVoteCount("プラナ", 1),
      RecordVoteCount("安倍晋三AI", 1)), VoteDecisionMethod.TIE_LOTTERY, true)
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 995, 50, 5, 1_000),
      RecordAffectionChange("プラナ", 500, -20, -20, 480),
      RecordAffectionChange("安倍晋三AI", 100, 0, 0, 100),
    ))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(false) {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, 2f)) {
          Box(Modifier.width(320.dp)) { Column {
            RecordVotingPanel(voting, "アロナ", null)
            RecordAffectionPanel(affection)
          } }
        }
      }
    } }
    compose.onNodeWithTag("vote-route-0").assertExists()
    compose.onNodeWithText("親愛度：995 → 1000").assertExists()
    compose.onNodeWithText("質問評価：+50点").assertDoesNotExist()
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

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureUi") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private fun label(id: Int): String = compose.activity.getString(id)
}
