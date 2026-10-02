package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.then
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.io.File
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Only synthetic metadata is rendered here; decorating the list must not require record details. */
@RunWith(AndroidJUnit4::class)
class RecordJournalVisualTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun datesUseTokyoMidnightAndLongQuestionsStopAtFourLines() {
    val entries = journalSamples()
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(360.dp, 800.dp))) {
        BootstrapUi(journalState(entries))
      }
    } }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag("journal-date-2026-10-03"))
    compose.onNodeWithTag("journal-date-2026-10-03").assertIsDisplayed()
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag("journal-card-${entries.first().recordId}"))
    val layouts = mutableListOf<TextLayoutResult>()
    compose.onNodeWithText(entries.first().questionPreview, useUnmergedTree = true)
      .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(layouts) }
    assertEquals(4, layouts.single().lineCount)
    assertTrue("An ellipsized question remains available in full in record detail", layouts.single().hasVisualOverflow)
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag("journal-date-2026-10-02"))
    compose.onNodeWithTag("journal-date-2026-10-02").assertIsDisplayed()
    compose.onNodeWithText("RECORDS ARCHIVE").assertDoesNotExist()
  }

  @Test fun requesterLeadsMetadataOnlyCardsWhileEveryWinnerAndMissingAvatarRemainReadable() {
    val entries = journalSamples()
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { it.setContent {
      BootstrapUi(journalState(entries, onEvent = events::add))
    } }
    for (entry in entries.take(4)) {
      val tag = "journal-card-${entry.recordId}"
      compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(tag))
      val requesterAvatar = compose.onNodeWithTag("journal-requester-avatar-${entry.recordId}", useUnmergedTree = true)
      val requesterName = compose.onNodeWithTag("journal-requester-name-${entry.recordId}", useUnmergedTree = true)
      val winnerAvatar = compose.onNodeWithTag("journal-winner-avatar-${entry.recordId}", useUnmergedTree = true)
      requesterAvatar.assertIsDisplayed()
      requesterName.assertIsDisplayed().assertTextEquals(entry.requesterName)
      winnerAvatar.assertIsDisplayed()
      compose.onNode(hasText(compose.activity.getString(R.string.record_list_winner, entry.winnerName)) and
        hasAnyAncestor(hasTestTag(tag)), useUnmergedTree = true).assertIsDisplayed()
      assertTrue("The requester is the card's primary face; the winner is supplementary",
        requesterAvatar.fetchSemanticsNode().boundsInRoot.height > winnerAvatar.fetchSemanticsNode().boundsInRoot.height)
      val nameLayouts = mutableListOf<TextLayoutResult>()
      requesterName.performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(nameLayouts) }
      assertTrue("Long requester names must remain bounded without replacing the question",
        nameLayouts.single().lineCount in 1..2)
      compose.onNodeWithTag(tag).assertIsDisplayed().assertIsNotSelected().performClick()
    }
    assertEquals(entries.take(4).map { BootstrapScreen.Event.OpenRecord(it.recordId) }, events)
  }

  @Test fun syncNotificationChangesAndDateInsertionsKeepTheReadingAnchor() {
    val entries = (1..20).map { index -> journalSamples().first().let { sample ->
      RecordListEntry(index.toString().padStart(43, 'j'), "架空の記録 $index：休日の散歩を楽しむには？",
        sample.requesterName, sample.requesterAvatar, sample.completedAt, sample.winnerName, sample.winnerSlot)
    } }
    var records = RecordListState.Ready.fromSaved(entries)
    val state = mutableStateOf(journalState(entries, records = records, sync = RecordSyncState.Running))
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    val anchor = "journal-card-${entries[13].recordId}"
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(anchor))
    val before = compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top
    val newer = journalSamples().last().let { sample ->
      RecordListEntry("n".repeat(43), "新しく届いた架空の記録", "架空の依頼者",
        sample.requesterAvatar, Instant.parse("2026-10-03T15:00:00Z"), sample.winnerName, sample.winnerSlot)
    }
    for ((updated, sync) in listOf(
      listOf(newer) + entries to RecordSyncState.Running,
      listOf(newer) + entries to RecordSyncState.Completed,
      listOf(newer) + entries.drop(1) to RecordSyncState.Failed(RecordReadFailure.UNAVAILABLE),
    )) {
      compose.runOnIdle {
        records = RecordListState.Ready.fromSaved(updated, records)
        state.value = journalState(updated, records = records, sync = sync)
      }
      compose.onNodeWithTag(anchor).assertIsDisplayed()
      assertEquals("Status and preceding date headers must not move the visible record", before,
        compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top, 1f)
    }
  }

  @Test fun lastCardRemainsAboveTheToolbarAndSelectionIsOnlyHighlightedInTwoPanes() {
    val entries = journalSamples()
    val records = RecordListState.Ready.fromSaved(entries)
    val window = mutableStateOf(DpSize(360.dp, 800.dp))
    val selected = mutableStateOf<String?>(null)
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value)) {
        BootstrapUi(journalState(entries, records = records, selected = selected.value))
      }
    } }
    val lastTag = "journal-card-${entries.last().recordId}"
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(lastTag))
    compose.onNodeWithTag(lastTag).performScrollTo().assertIsDisplayed().assertIsNotSelected()
    val card = compose.onNodeWithTag(lastTag).fetchSemanticsNode().boundsInRoot
    val toolbar = compose.onNodeWithTag("records-query-toolbar").fetchSemanticsNode().boundsInRoot
    assertTrue("The final card must be readable and tappable above the floating tools", card.bottom <= toolbar.top)
    compose.runOnIdle { window.value = DpSize(1000.dp, 700.dp); selected.value = entries.last().recordId }
    // Width changes alter line wrapping and card heights; inspect the selected card explicitly.
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(lastTag))
    compose.onNodeWithTag(lastTag).assertIsDisplayed().assertIsSelected()
    compose.onNodeWithTag("records-query-toolbar").assertDoesNotExist()
    compose.runOnIdle { selected.value = null; window.value = DpSize(360.dp, 800.dp) }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(lastTag))
    compose.onNodeWithTag(lastTag).assertIsDisplayed().assertIsNotSelected()
  }

  /** Optional screenshots and real-frame pacing are not part of normal CI execution. */
  @Test fun captureJournalLayoutsAndInteractionPreview() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureJournal") != "true") return
    val window = mutableStateOf(DpSize(420.dp, 800.dp))
    val scale = mutableStateOf(1f)
    val dark = mutableStateOf(false)
    val selected = mutableStateOf<String?>(null)
    // Bundled character art stands in for a synthetic requester's saved offline thumbnail.
    val localAvatar = compose.activity.resources.openRawResource(R.drawable.participant_b).use {
      RecordAvatar(null, "cyan", bytes = it.readBytes())
    }
    val entries = journalSamples(localAvatar)
    val records = RecordListState.Ready.fromSaved(entries)
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value) then
        DeviceConfigurationOverride.FontScale(scale.value)) {
        BootstrapUi(journalState(entries, records = records,
          theme = if (dark.value) ThemeChoice.Dark else ThemeChoice.Light,
          selected = selected.value, onEvent = { event ->
            if (event is BootstrapScreen.Event.OpenRecord) selected.value = event.recordId
            if (event == BootstrapScreen.Event.CloseRecord) selected.value = null
          }))
      }
    } }
    for (mode in listOf(false, true)) {
      compose.runOnIdle { dark.value = mode }
      capture("journal-${if (mode) "dark" else "light"}")
    }
    compose.runOnIdle { window.value = DpSize(320.dp, 800.dp); scale.value = 2f }
    capture("journal-320dp-2x")
    compose.runOnIdle {
      window.value = DpSize(1000.dp, 700.dp)
      scale.value = 1f
      selected.value = entries.first().recordId
    }
    capture("journal-wide")
    if (InstrumentationRegistry.getArguments().getString("shittimRecordJournal") == "true") {
      compose.runOnIdle { window.value = DpSize(420.dp, 800.dp); selected.value = null }
      compose.waitForIdle()
      compose.mainClock.autoAdvance = false
      try {
        holdPreview(90)
        compose.mainClock.autoAdvance = true
        compose.onNodeWithTag("bootstrap-content")
          .performScrollToNode(hasTestTag("journal-card-${entries[2].recordId}"))
        compose.mainClock.autoAdvance = false
        holdPreview(45)
        compose.mainClock.autoAdvance = true
        compose.onNodeWithTag("journal-card-${entries[2].recordId}").performClick()
        compose.mainClock.autoAdvance = false
        holdPreview(120)
        compose.runOnUiThread { compose.activity.onBackPressedDispatcher.onBackPressed() }
        holdPreview(100)
      } finally { compose.mainClock.autoAdvance = true }
    }
  }

  private fun holdPreview(frames: Int) = repeat(frames) {
    compose.mainClock.advanceTimeByFrame()
    Thread.sleep(16)
  }

  private fun capture(name: String) {
    compose.waitForIdle()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private fun journalState(entries: List<RecordListEntry>,
    records: RecordListState = RecordListState.Ready.fromSaved(entries),
    theme: ThemeChoice = ThemeChoice.Dark, sync: RecordSyncState = RecordSyncState.Idle,
    selected: String? = null, onEvent: (BootstrapScreen.Event) -> Unit = {}): BootstrapScreen.State =
    BootstrapScreen.State(theme, SessionState.SignedIn(
      MobileSessionUser("架空の利用者", MobileAvatar("placeholder", "確認用", "cyan")),
      "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/"),
      records = records, sync = sync, selectedRecordId = selected,
      record = RecordPreviewState.Ready(RecordPreview("架空の議題の全文です。", "架空の結論", "アロナ")),
      eventSink = onEvent)

  private fun journalSamples(firstAvatar: RecordAvatar = RecordAvatar(null, "cyan")): List<RecordListEntry> = listOf(
    RecordListEntry("a".repeat(43),
      "3人が休日に小さなカフェを開くなら、接客や準備はどう分担しますか。".repeat(6).take(160),
      "架空の依頼者・とても長い名前の表示も確認します", firstAvatar,
      Instant.parse("2026-10-02T15:00:00Z"), "アロナ", "participant-a"),
    RecordListEntry("p".repeat(43), "雨の日も気分転換できる、静かな過ごし方を考えてください。",
      "架空の読書好き", RecordAvatar(null, "pink"), Instant.parse("2026-10-02T14:59:59Z"),
      "プラナ", "participant-b"),
    RecordListEntry("s".repeat(43), "友人同士で料理を楽しむための、無理のない役割分担は？",
      "架空の料理好き", RecordAvatar(null, "lavender"), Instant.parse("2026-10-02T12:45:00Z"),
      "安倍晋三AI", "participant-c"),
    RecordListEntry("x".repeat(43), "過去の記録に未知の人物が残っている場合の表示確認です。",
      "架空の利用者", RecordAvatar(null, "cyan"), Instant.parse("2026-10-01T12:00:00Z"), "未知の人格"),
  )
}
