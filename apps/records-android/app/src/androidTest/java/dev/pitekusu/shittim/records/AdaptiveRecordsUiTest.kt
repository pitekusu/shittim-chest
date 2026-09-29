package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.BackEventCompat
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.input.InputMode
import androidx.compose.ui.input.InputModeManager
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.LocalInputModeManager
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotFocused
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.test.then
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

@RunWith(AndroidJUnit4::class)
class AdaptiveRecordsUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val entries = (1..6).map { index ->
    RecordListEntry(index.toString().padStart(43, 'a'), "架空の相談 $index：休日に楽しめる小さなことは？",
      "レイアウト確認用", RecordAvatar(null, "cyan"), Instant.parse("2026-09-27T00:00:00Z"), "アロナ")
  }
  private val records = RecordListState.Ready.fromSaved(entries)
  private val session = SessionState.SignedIn(
    MobileSessionUser("レイアウト確認用", MobileAvatar("placeholder", "確認用", "cyan")),
    "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")

  private fun screen(selected: String? = null, theme: ThemeChoice = ThemeChoice.Dark,
    onEvent: (BootstrapScreen.Event) -> Unit = {}): BootstrapScreen.State =
    BootstrapScreen.State(theme, session, records = records, selectedRecordId = selected,
      record = RecordPreviewState.Ready(RecordPreview("架空の議題：休日に楽しむ散歩と読書",
        "天気と気分に合わせて、無理のない小さな楽しみを選びましょう。", "アロナ"), saved = true),
      eventSink = onEvent)

  @Test fun resizeKeepsTheSelectedRecordAndListScrollAndRevocationHidesContent() {
    val window = mutableStateOf(DpSize(1000.dp, 700.dp))
    val state = mutableStateOf(screen())
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value)) { BootstrapUi(state.value) }
    } }
    val lastQuestion = entries.last().questionPreview
    // Scroll to the record rather than depending on the number of fixed header rows.
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(lastQuestion))
    val initialScroll = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue(initialScroll > 0f)
    compose.runOnIdle { state.value = screen(entries.last().recordId) }
    compose.waitUntil(10_000) { compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").isDisplayed() }
    compose.onNode(SemanticsMatcher.expectValue(SemanticsProperties.PaneTitle, "議論詳細"))
      .assert(SemanticsMatcher.expectValue(SemanticsProperties.IsTraversalGroup, true))
    capture("adaptive-wide-dark")
    compose.runOnIdle { window.value = DpSize(820.dp, 700.dp) }
    compose.waitUntil(10_000) { compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").isDisplayed() }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.runOnIdle { window.value = DpSize(1000.dp, 700.dp) }
    compose.waitUntil(10_000) { compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").isDisplayed() }
    compose.runOnIdle { state.value = screen() }
    val restoredScroll = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    assertTrue("The list must not jump back to its top on resize", restoredScroll > 0f)
    compose.runOnIdle { state.value = screen(entries.last().recordId, ThemeChoice.Light) }
    capture("adaptive-wide-light")
    compose.runOnIdle { state.value = BootstrapScreen.State(ThemeChoice.Dark) {} }
    compose.onNodeWithText(lastQuestion).assertDoesNotExist()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
  }

  @Test fun largeTextUsesOnePaneAndSystemBackWorksWhileReading() {
    val window = mutableStateOf(DpSize(1000.dp, 700.dp))
    val state = mutableStateOf(screen(entries.first().recordId, ThemeChoice.Light))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value) then
        DeviceConfigurationOverride.FontScale(2f)) {
        BootstrapUi(screen(state.value.selectedRecordId, state.value.themeChoice, events::add))
      }
    } }
    compose.onNodeWithTag("record-detail-content").assertDoesNotExist()
    compose.onNodeWithText("一覧に戻る").assertDoesNotExist()
    compose.runOnIdle { window.value = DpSize(320.dp, 640.dp) }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText("親愛度の変化"))
    compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.CloseRecord, events.last()) }
    capture("adaptive-large-text")
  }

  @Test fun keyboardCanOpenARecordWithoutReadingItsDecorativeAvatar() {
    val events = mutableListOf<BootstrapScreen.Event>()
    var inputMode: InputModeManager? = null
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      val manager = LocalInputModeManager.current
      SideEffect { inputMode = manager }
      BootstrapUi(screen(onEvent = events::add))
    } }
    compose.runOnIdle { requireNotNull(inputMode).requestInputMode(InputMode.Keyboard) }
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(entries.first().questionPreview))
    val card = compose.onNodeWithText(entries.first().questionPreview)
    card.performSemanticsAction(SemanticsActions.RequestFocus)
    compose.waitForIdle()
    card.assertIsFocused()
    card.performKeyInput { pressKey(Key.Enter) }
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.OpenRecord(entries.first().recordId), events.last()) }
    // The fallback initial repeats the adjacent name and must not become an extra TalkBack utterance.
    compose.onNodeWithText("レ").assertDoesNotExist()
    capture("adaptive-compact-dark")
  }

  @Test fun predictiveBackCanBeCancelledAndThenCommittedWithoutLosingListPosition() {
    val state = mutableStateOf(screen())
    val events = mutableListOf<BootstrapScreen.Event>()
    val selected = entries.last().recordId
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      BootstrapUi(screen(state.value.selectedRecordId, onEvent = { event ->
        events.add(event)
        if (event is BootstrapScreen.Event.OpenRecord) state.value = screen(event.recordId)
        if (event == BootstrapScreen.Event.CloseRecord) state.value = screen()
      }))
    } }
    val question = entries.last().questionPreview
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(question))
    compose.onNodeWithText(question).performClick()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(120f, 0f, 0.65f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { assertEquals(selected, state.value.selectedRecordId) }
    capture("adaptive-back-preview")
    compose.runOnIdle { dispatcher.dispatchOnBackCancelled() }
    compose.waitForIdle()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.runOnIdle { assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord }) }
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(120f, 0f, 0.7f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.onBackPressed() }
    compose.waitForIdle()
    compose.onNodeWithText(question).assertIsDisplayed()
    compose.runOnIdle {
      assertEquals(null, state.value.selectedRecordId)
      assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
  }

  @Test fun returningFromDetailKeepsSearchClosedAndUnfocused() {
    val state = mutableStateOf(screen())
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 1000.dp))) {
        BootstrapUi(screen(state.value.selectedRecordId, onEvent = { event ->
          if (event is BootstrapScreen.Event.OpenRecord) state.value = screen(event.recordId)
          if (event == BootstrapScreen.Event.CloseRecord) state.value = screen()
        }))
      }
    } }
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsDisplayed().performClick().assertIsFocused()
    compose.onNodeWithText(entries.first().questionPreview).assertIsDisplayed().performClick()
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithText("一覧に戻る").assertDoesNotExist()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { dispatcher.onBackPressed() }
      // Closing the disclosure before the transition prevents the old focus flash.
      repeat(8) {
        compose.mainClock.advanceTimeBy(80)
        compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
      }
    } finally {
      compose.mainClock.autoAdvance = true
    }
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsDisplayed().assertIsNotFocused()
      .performClick().assertIsFocused()
  }

  @Test fun revocationDuringTheBackGestureHidesTheRecordAndDoesNotCommitIt() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val state = mutableStateOf(screen(entries.first().recordId, onEvent = events::add))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent { BootstrapUi(state.value) } }
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { state.value = BootstrapScreen.State(ThemeChoice.Dark) {} }
    compose.waitForIdle()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
    compose.runOnIdle {
      dispatcher.dispatchOnBackCancelled()
      assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
  }

  @Test fun anOldGestureDoesNotCloseTheNewlySelectedRecord() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val state = mutableStateOf(screen(entries.first().recordId, onEvent = events::add))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent { BootstrapUi(state.value) } }
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { state.value = screen(entries.last().recordId, onEvent = events::add) }
    compose.runOnIdle { dispatcher.onBackPressed() }
    compose.waitForIdle()
    compose.runOnIdle {
      assertEquals(entries.last().recordId, state.value.selectedRecordId)
      assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureAdaptive") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
