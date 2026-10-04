package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import android.graphics.Bitmap
import android.view.ViewGroup
import androidx.activity.BackEventCompat
import androidx.activity.compose.setContent
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.input.InputMode
import androidx.compose.ui.input.InputModeManager
import androidx.compose.ui.input.key.Key
import androidx.compose.ui.platform.LocalInputModeManager
import androidx.compose.ui.platform.ComposeView
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
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performKeyInput
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.pressKey
import androidx.compose.ui.test.then
import androidx.compose.ui.test.swipe
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.rememberNavBackStack
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
@ScreenTest
class AdaptiveRecordsUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val entries = (1..6).map { index ->
    RecordListEntry(index.toString().padStart(43, 'a'), "架空の相談 $index：休日に楽しめる小さなことは？",
      "レイアウト確認用", RecordAvatar(null, "cyan"), Instant.parse("2026-09-27T00:00:00Z"), "アロナ")
  }
  private val records = RecordListState.Ready.fromSaved(entries)
  private val session = SessionState.SignedIn(
    MobileSessionUser("レイアウト確認用", MobileAvatar("placeholder", "確認用", "cyan")),
    "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"))

  private fun screen(selected: String? = null, theme: ThemeChoice = ThemeChoice.Dark,
    backStack: List<NavKey> = listOf(RecordsList) + listOfNotNull(selected?.let(::RecordDetail)),
    onEvent: (BootstrapScreen.Event) -> Unit = {}): BootstrapScreen.State =
    BootstrapScreen.State(theme, session, records = records,
      selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId, backStack = backStack,
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
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value) then
        DeviceConfigurationOverride.FontScale(2f)) {
        BootstrapUi(screen(theme = ThemeChoice.Light, backStack = backStack.toList(), onEvent = { event ->
          events += event
          if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
        }))
      }
    } }
    compose.onNodeWithTag("record-detail-content").assertDoesNotExist()
    compose.onNodeWithText("一覧に戻る").assertDoesNotExist()
    compose.runOnIdle { window.value = DpSize(320.dp, 640.dp) }
    compose.onNodeWithTag("detail-section-Affection").performClick()
    compose.waitUntil(5_000) { compose.onNodeWithText("親愛度の変化").isDisplayed() }
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
    val backStack = NavBackStack<NavKey>(RecordsList)
    val events = mutableListOf<BootstrapScreen.Event>()
    val selected = entries.last().recordId
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      BootstrapUi(screen(backStack = backStack.toList(), onEvent = { event ->
        events.add(event)
        if (event is BootstrapScreen.Event.OpenRecord) backStack.openRecord(event.recordId)
        if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
      }))
    } }
    val question = entries.last().questionPreview
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(question))
    compose.onNodeWithText(question).performClick()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(120f, 0f, 0.65f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { assertEquals(RecordDetail(selected), backStack.last()) }
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
      assertEquals(listOf(RecordsList), backStack.toList())
      assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
  }

  @Test fun returningFromDetailKeepsSearchClosedAndUnfocused() {
    val backStack = NavBackStack<NavKey>(RecordsList)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 1000.dp))) {
        BootstrapUi(screen(backStack = backStack.toList(), onEvent = { event ->
          if (event is BootstrapScreen.Event.OpenRecord) backStack.openRecord(event.recordId)
          if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
        }))
      }
    } }
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsDisplayed().performClick().assertIsFocused()
    compose.onNode(hasText(entries.first().questionPreview) and
      hasAnyAncestor(hasTestTag("records-search-results"))).assertIsDisplayed().performClick()
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
    // Explicitly opening the dedicated search is the only automatic-focus path.
    compose.onNodeWithTag("record-search").assertIsDisplayed().assertIsFocused()
  }

  @Test fun reopeningTheSameCompactRecordStartsAtAronasInitialAnswerAndKeepsListPosition() {
    val backStack = NavBackStack<NavKey>(RecordsList)
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "再入場試験の初回意見$index",
        "架空の初回本文$index。読み位置の確認用です。\n\n".repeat(40) + "初回本文の末尾$index",
        "再入場試験の最終案$index",
        "架空の最終本文$index。読み位置の確認用です。\n\n".repeat(40) + "最終本文の末尾$index",
        participantSlot = "participant-${('a'.code + index).toChar()}")
    }
    val preview = RecordPreviewState.Ready(RecordPreview("再入場試験の架空の議題", "架空の結論", "プラナ",
      opinions = listOf(opinions[2], opinions[0], opinions[1]), winnerSlot = "participant-b"), saved = true)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 850.dp))) {
        BootstrapUi(BootstrapScreen.State(ThemeChoice.Dark, session, records = records,
          selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId,
          backStack = backStack.toList(), record = preview, eventSink = { event ->
            when (event) {
              is BootstrapScreen.Event.OpenRecord -> backStack.openRecord(event.recordId)
              BootstrapScreen.Event.CloseRecord -> backStack.closeRecord()
              else -> Unit
            }
          }))
      }
    } }
    val question = entries.last().questionPreview
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(question))
    fun readingPosition() = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    val listPosition = readingPosition()
    assertTrue(listPosition > 0f)
    fun assertInitialAnswer() {
      compose.waitUntil(10_000) { compose.onNodeWithText("再入場試験の初回意見0").isDisplayed() }
      compose.waitForIdle()
      compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
      compose.onNodeWithTag("opinion-person-0").assertIsSelected()
      compose.onNodeWithText(compose.activity.getString(R.string.record_initial_opinion)).assertIsOn()
      assertEquals(0f, readingPosition(), .01f)
    }
    fun readPartway(tail: String) {
      // Wait for the actual parsed answer, not its empty asynchronous placeholder.
      compose.waitUntil(10_000) {
        compose.onAllNodesWithText(tail, substring = true).fetchSemanticsNodes().isNotEmpty() &&
          compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
            .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
      }
      compose.onNodeWithTag("bootstrap-content").performTouchInput {
        swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
      }
      assertTrue(readingPosition() > 0f)
    }
    fun closeAndReopen() {
      compose.runOnIdle { compose.activity.onBackPressedDispatcher.onBackPressed() }
      compose.waitUntil(10_000) { backStack.last() == RecordsList && compose.onNodeWithText(question).isDisplayed() }
      compose.waitForIdle()
      compose.onNodeWithTag("detail-pager").assertDoesNotExist()
      assertEquals(listPosition, readingPosition(), .01f)
      compose.onNodeWithText(question).performClick()
      assertInitialAnswer()
    }
    compose.onNodeWithText(question).performClick()
    assertInitialAnswer()
    readPartway("初回本文の末尾0")
    // A popped NavEntry must discard its reader state even when the previous answer was Arona.
    closeAndReopen()
    compose.onNodeWithTag("opinion-person-2").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("再入場試験の初回意見2").isDisplayed() }
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("再入場試験の最終案2").isDisplayed() }
    readPartway("最終本文の末尾2")
    // A later answer must not be restored when the same record is entered again.
    closeAndReopen()
  }

  @Test fun restoredDetailWaitsForTheSavedBodyBeforeRestoringItsAnswerAndReadingPosition() {
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "復元試験の初回意見$index", "架空の初回本文$index",
        "復元試験の最終案$index", "架空の最終本文$index。読み位置の確認用です。\n\n".repeat(40) + "復元本文の末尾$index",
        participantSlot = "participant-${('a'.code + index).toChar()}")
    }
    val ready = RecordPreviewState.Ready(RecordPreview("復元試験の架空の議題", "架空の結論", "プラナ",
      opinions = listOf(opinions[2], opinions[0], opinions[1]), winnerSlot = "participant-b"), saved = true)
    // The real presenter holds decrypted text only in memory, then reads its encrypted cache
    // asynchronously after recreation. Preserve only the reader's local SavedState here too.
    val record = mutableStateOf<RecordPreviewState>(ready)
    var restoreWithLoading = false
    val restoration = StateRestorationTester(compose)
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent {
      DisposableEffect(Unit) {
        onDispose { if (restoreWithLoading) record.value = RecordPreviewState.Loading }
      }
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 850.dp))) {
        val backStack = rememberNavBackStack(RecordsList, RecordDetail(entries.last().recordId))
        BootstrapUi(BootstrapScreen.State(ThemeChoice.Dark, session, records = records,
          selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId,
          backStack = backStack.toList(), record = record.value, eventSink = {}))
      }
    }
    compose.waitUntil(10_000) { compose.onNodeWithText("復元試験の初回意見0").isDisplayed() }
    compose.onNodeWithTag("opinion-person-2").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("復元試験の初回意見2").isDisplayed() }
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).performClick()
    compose.waitUntil(10_000) {
      compose.onNodeWithText("復元試験の最終案2").isDisplayed() &&
        compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
          .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
    }
    compose.onNodeWithTag("bootstrap-content").performTouchInput {
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    fun readingPosition() = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    val position = readingPosition()
    assertTrue(position > 0f)
    compose.runOnIdle { restoreWithLoading = true }
    restoration.emulateSavedInstanceStateRestore()
    compose.runOnIdle {
      restoreWithLoading = false
      assertTrue(record.value is RecordPreviewState.Loading)
    }
    compose.onNodeWithText(compose.activity.getString(R.string.record_loading)).assertIsDisplayed()
    compose.waitForIdle() // A real Loading layout must occur before the cache read completes.
    compose.runOnIdle { record.value = ready }
    compose.waitUntil(10_000) { compose.onNodeWithText("復元試験の架空の議題").isDisplayed() }
    compose.waitForIdle()
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
    compose.onNodeWithTag("opinion-person-2").assertIsSelected()
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).assertIsOn()
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("復元本文の末尾2", substring = true).fetchSemanticsNodes().isNotEmpty()
    }
    assertEquals(position, readingPosition(), .01f)
  }

  @Test fun threeButtonBackPopsOnceAndSlidesWithoutLeavingASelectedListCard() {
    val backStack = NavBackStack<NavKey>(RecordsList)
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { it.setContent {
      BootstrapUi(screen(backStack = backStack.toList(), onEvent = { event ->
        events += event
        if (event is BootstrapScreen.Event.OpenRecord) backStack.openRecord(event.recordId)
        if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
      }))
    } }
    val question = entries.last().questionPreview
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(question))
    val position = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    compose.onNodeWithText(question).performClick()
    compose.waitForIdle()
    val detailQuestion = "架空の議題：休日に楽しむ散歩と読書"
    val detailLeft = compose.onNodeWithText(detailQuestion).fetchSemanticsNode().boundsInRoot.left
    val navigationHeight = compose.onNodeWithTag("detail-section-Opinions", useUnmergedTree = true)
      .fetchSemanticsNode().boundsInRoot.height
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { compose.activity.onBackPressedDispatcher.onBackPressed() }
      compose.mainClock.advanceTimeBy(96)
      compose.runOnIdle {
        assertEquals(listOf(RecordsList), backStack.toList())
        assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
      }
      if (ValueAnimator.areAnimatorsEnabled()) {
        // A moving outgoing pane, not a frozen selected card or a placeholder flash.
        assertTrue(compose.onNodeWithText(detailQuestion, useUnmergedTree = true)
          .fetchSemanticsNode().boundsInRoot.left > detailLeft + 1f)
        // Button Back slides horizontally; it must not shrink the entire scaffold
        // and leave a separate scale-settle animation after the list appears.
        assertEquals(navigationHeight, compose.onNodeWithTag("detail-section-Opinions", useUnmergedTree = true)
          .fetchSemanticsNode().boundsInRoot.height, 1f)
        compose.onNodeWithText(compose.activity.getString(R.string.record_select)).assertDoesNotExist()
        capture("adaptive-back-slide-middle")
      }
      compose.mainClock.advanceTimeBy(320)
      compose.runOnIdle {
        assertEquals(listOf(RecordsList), backStack.toList())
        assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
      }
      compose.onNodeWithText(question).assertIsDisplayed().assertIsNotSelected()
      compose.onNodeWithText(compose.activity.getString(R.string.record_selected)).assertDoesNotExist()
      val restored = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
        .config[SemanticsProperties.VerticalScrollAxisRange].value()
      assertEquals(position, restored, .01f)
      capture("adaptive-back-slide-complete")
    } finally {
      compose.mainClock.autoAdvance = true
    }
  }

  @Test fun revocationDuringTheReturnSlideHidesTheOutgoingRecordWithoutAnotherPop() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val authorized = mutableStateOf(true)
    compose.activityRule.scenario.onActivity { it.setContent {
      BootstrapUi(if (authorized.value) screen(backStack = backStack.toList(), onEvent = { event ->
        events += event
        if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
      }) else BootstrapScreen.State(ThemeChoice.Dark) {})
    } }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.waitForIdle() // Let the initial entry and its standard Back handler become active.
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { compose.activity.onBackPressedDispatcher.onBackPressed() }
      compose.mainClock.advanceTimeBy(64)
      compose.runOnIdle { authorized.value = false }
      compose.mainClock.advanceTimeBy(400)
      compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
      compose.runOnIdle {
        assertEquals(listOf(RecordsList), backStack.toList())
        assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
      }
    } finally {
      compose.mainClock.autoAdvance = true
    }
  }

  @Test fun revocationDuringTheBackGestureHidesTheRecordAndDoesNotCommitIt() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val authorized = mutableStateOf(true)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      BootstrapUi(if (authorized.value) screen(backStack = backStack.toList(), onEvent = { event ->
        events += event
        if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
      }) else BootstrapScreen.State(ThemeChoice.Dark) {})
    } }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { authorized.value = false }
    compose.waitForIdle()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
    compose.runOnIdle {
      dispatcher.dispatchOnBackCancelled()
      assertEquals(RecordDetail(entries.first().recordId), backStack.last())
      assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
  }

  @Test fun anOldGestureDoesNotCloseTheNewlySelectedRecord() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      BootstrapUi(screen(backStack = backStack.toList(), onEvent = { event ->
        events += event
        if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
      }))
    } }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { backStack.openRecord(entries.last().recordId) }
    compose.runOnIdle { dispatcher.onBackPressed() }
    compose.waitForIdle()
    compose.runOnIdle {
      assertEquals(RecordDetail(entries.last().recordId), backStack.last())
      assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
  }

  @Test fun switchingAccountsDuringBackPreviewNeverKeepsTheOldOutgoingBody() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val state = mutableStateOf(screen(backStack = backStack.toList(), onEvent = events::add))
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertIsDisplayed()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    val nextAccount = "v".repeat(43)
    val nextEntry = RecordListEntry(entries.first().recordId, "新しいアカウントの架空の一覧",
      "新しい架空の依頼者", RecordAvatar(null, "pink"), Instant.parse("2026-09-27T00:00:00Z"), "プラナ")
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle {
        backStack.closeRecord()
        state.value = BootstrapScreen.State(ThemeChoice.Dark, SessionState.SignedIn(
          MobileSessionUser("新しい架空の利用者", MobileAvatar("placeholder", "確認用", "pink")),
          nextAccount, Instant.parse("2027-01-01T00:00:00Z")),
          records = RecordListState.Ready.fromSaved(listOf(nextEntry)), backStack = backStack.toList(),
          recordOwner = nextAccount, eventSink = events::add)
      }
      compose.mainClock.advanceTimeBy(32)
      compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書", useUnmergedTree = true).assertDoesNotExist()
      compose.onNodeWithText(nextEntry.questionPreview).assertIsDisplayed()
      compose.runOnIdle {
        dispatcher.dispatchOnBackCancelled()
        assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
      }
    } finally { compose.mainClock.autoAdvance = true }
  }

  @Test fun movingBetweenCompactAndWideKeepsTheCurrentAnswerAndReadingPosition() {
    val window = mutableStateOf(DpSize(420.dp, 850.dp))
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
      RecordOpinion(name, "幅変更試験の初回意見$index", "架空の初回本文$index",
        "幅変更試験の最終案$index", "幅変更試験の架空本文$index。読み位置の確認用です。\n\n".repeat(40) + "幅変更本文の末尾$index",
        participantSlot = "participant-${('a'.code + index).toChar()}")
    }
    val record = RecordPreviewState.Ready(RecordPreview("幅変更試験の架空の議題", "架空の結論", "アロナ",
      opinions = opinions, winnerSlot = "participant-a"))
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(window.value)) {
        BootstrapUi(BootstrapScreen.State(ThemeChoice.Dark, session, records = records,
          selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId,
          backStack = backStack.toList(), record = record, eventSink = {}))
      }
    } }
    compose.onNodeWithTag("opinion-person-2").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("幅変更試験の初回意見2").isDisplayed() }
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).performClick()
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("幅変更本文の末尾2", substring = true).fetchSemanticsNodes().isNotEmpty() &&
        compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
          .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
    }
    fun readingPosition(tag: String) = compose.onNodeWithTag(tag).fetchSemanticsNode()
      .config[SemanticsProperties.VerticalScrollAxisRange].value()
    compose.onNodeWithTag("bootstrap-content").performTouchInput {
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    val compactPosition = readingPosition("bootstrap-content")
    assertTrue(compactPosition > 0f)
    compose.runOnIdle { window.value = DpSize(1000.dp, 850.dp) }
    compose.onNodeWithTag("record-detail-content").assertIsDisplayed()
    compose.waitForIdle()
    compose.onNodeWithTag("opinion-person-2").assertIsSelected()
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).assertIsOn()
    assertTrue(readingPosition("record-detail-content") > 0f)
    compose.runOnIdle { window.value = DpSize(420.dp, 850.dp) }
    compose.waitForIdle()
    compose.onNodeWithTag("opinion-person-2").assertIsSelected()
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).assertIsOn()
    assertEquals(compactPosition, readingPosition("bootstrap-content"), .01f)
    compose.runOnIdle { assertEquals(RecordDetail(entries.first().recordId), backStack.last()) }
  }

  @Test fun unchangedRouteDisplaysTheLoadedBodyAndLaterUpdatesWithoutReopening() {
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    val record = mutableStateOf<RecordPreviewState>(RecordPreviewState.Loading)
    fun ready(question: String, body: String) = RecordPreviewState.Ready(RecordPreview(
      question, "架空の結論", "アロナ", opinions = listOf(RecordOpinion(
        "アロナ", "架空の初回意見", body, "架空の最終案", "架空の最終本文", participantSlot = "participant-a"))))
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 850.dp))) {
        BootstrapUi(BootstrapScreen.State(ThemeChoice.Dark, session, records = records,
          selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId,
          backStack = backStack.toList(), record = record.value, eventSink = {}))
      }
    } }
    compose.onNodeWithText(compose.activity.getString(R.string.record_loading)).assertIsDisplayed()
    compose.runOnIdle { record.value = ready("最初に読み込んだ架空の議題", "最初の架空本文") }
    compose.waitUntil(10_000) {
      compose.onNodeWithText("最初に読み込んだ架空の議題").isDisplayed() &&
        compose.onNodeWithText("最初の架空本文").isDisplayed()
    }
    compose.runOnIdle { record.value = ready("更新された架空の議題", "更新された架空本文") }
    compose.waitUntil(10_000) {
      compose.onNodeWithText("更新された架空の議題").isDisplayed() &&
        compose.onNodeWithText("更新された架空本文").isDisplayed()
    }
    compose.onNodeWithText("最初に読み込んだ架空の議題").assertDoesNotExist()
    compose.onNodeWithText("最初の架空本文").assertDoesNotExist()
    compose.runOnIdle { assertEquals(listOf(RecordsList, RecordDetail(entries.first().recordId)), backStack.toList()) }
  }

  @Test fun wideSceneBackClosesSelectionAndAnOldGestureCannotCloseItsReplacement() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val backStack = NavBackStack<NavKey>(RecordsList, RecordDetail(entries.first().recordId))
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(1000.dp, 700.dp))) {
        BootstrapUi(screen(backStack = backStack.toList(), onEvent = { event ->
          events += event
          if (event == BootstrapScreen.Event.CloseRecord) backStack.closeRecord()
        }))
      }
    } }
    compose.onNodeWithTag("record-detail-content").assertIsDisplayed()
    compose.waitForIdle()
    val dispatcher = compose.activity.onBackPressedDispatcher
    compose.runOnIdle { dispatcher.dispatchOnBackStarted(BackEventCompat(0f, 0f, 0f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { dispatcher.dispatchOnBackProgressed(BackEventCompat(100f, 0f, 0.4f, BackEventCompat.EDGE_LEFT)) }
    compose.runOnIdle { backStack.openRecord(entries.last().recordId) }
    compose.runOnIdle { dispatcher.onBackPressed() }
    compose.waitForIdle()
    compose.runOnIdle {
      assertEquals(RecordDetail(entries.last().recordId), backStack.last())
      assertEquals(0, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
    compose.runOnIdle { dispatcher.onBackPressed() }
    compose.waitForIdle()
    compose.runOnIdle {
      assertEquals(listOf(RecordsList), backStack.toList())
      assertEquals(1, events.count { it == BootstrapScreen.Event.CloseRecord })
    }
    compose.onNodeWithText(compose.activity.getString(R.string.record_select)).assertIsDisplayed()
    compose.onNodeWithText("架空の議題：休日に楽しむ散歩と読書").assertDoesNotExist()
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(entries.last().questionPreview))
    compose.onNodeWithText(entries.last().questionPreview).assertIsNotSelected()
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureAdaptive") != "true") return
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
