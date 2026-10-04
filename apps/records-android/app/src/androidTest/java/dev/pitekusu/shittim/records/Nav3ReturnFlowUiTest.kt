package dev.pitekusu.shittim.records

import android.app.Activity
import android.app.Instrumentation
import android.content.Intent
import android.view.WindowInsets
import androidx.activity.compose.setContent
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.navigation3.runtime.NavBackStack
import androidx.navigation3.runtime.NavKey
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Screen return retains the current entry; it is not another deep-link or login request. */
@RunWith(AndroidJUnit4::class)
@ScreenTest
class Nav3ReturnFlowUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun returningFromTheWebMenuKeepsTheAnswerAndReadingPosition() {
    val host = Host(detailOpen = true)
    show(host)
    compose.waitUntil(10_000) { compose.onNodeWithText("復帰試験の初回意見0").isDisplayed() }
    compose.onNodeWithTag("opinion-person-1").performClick()
    compose.waitUntil(10_000) { compose.onNodeWithText("復帰試験の初回意見1").isDisplayed() }
    compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).performClick()
    compose.waitUntil(10_000) {
      compose.onAllNodesWithText("復帰本文の末尾1", substring = true).fetchSemanticsNodes().isNotEmpty() &&
        compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
          .config[SemanticsProperties.VerticalScrollAxisRange].maxValue() > 0f
    }
    compose.onNodeWithTag("bootstrap-content").performTouchInput {
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    val position = readingPosition()
    assertTrue(position > 0f)
    val routes = host.backStack.toList()

    // Standard instrumentation intercepts the launch: no browser, Discord or network is opened.
    val instrumentation = InstrumentationRegistry.getInstrumentation()
    val launchedUrls = mutableListOf<String?>()
    val monitor = object : Instrumentation.ActivityMonitor() {
      override fun onStartActivity(intent: Intent): Instrumentation.ActivityResult? {
        if (intent.action != Intent.ACTION_VIEW) return null
        launchedUrls += intent.dataString
        return Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null)
      }
    }
    instrumentation.addMonitor(monitor)
    try {
      compose.onNodeWithTag("records-menu-open").performClick()
      compose.onNodeWithText(compose.activity.getString(R.string.menu_momotalk)).performScrollTo().performClick()
      compose.onNodeWithTag("records-menu").assertDoesNotExist()
      stopAndResume()
      compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
      compose.onNodeWithTag("opinion-person-1").assertIsSelected()
      compose.onNodeWithText(compose.activity.getString(R.string.record_final_proposal)).assertIsOn()
      compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
      compose.onNodeWithTag("brand-intro").assertDoesNotExist()
      assertEquals(position, readingPosition(), .01f)
      compose.runOnIdle {
        assertEquals(listOf("https://shittim.pitekusu.dev/momotalk"), launchedUrls)
        assertEquals(routes, host.backStack.toList())
      }
    } finally {
      instrumentation.removeMonitor(monitor)
    }
  }

  @Test fun screenReturnKeepsListCriteriaAndPositionWithoutReopeningSearchOrKeyboard() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("winner-Abe").performScrollTo().performClick()
    compose.onNodeWithTag("order-Oldest").performScrollTo().performClick()
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsFocused().performTextInput("散歩")
    compose.onNodeWithTag("records-search-close").performClick()
    val anchor = "journal-card-${host.entries[14].recordId}"
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(anchor))
    compose.onNodeWithTag(anchor).assertIsDisplayed()
    val before = compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top
    val query = host.query
    stopAndResume()
    compose.onNodeWithTag(anchor).assertIsDisplayed()
    assertEquals(before, compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top, 1f)
    compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("records-search-screen", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("records-filter-sheet", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()
    compose.runOnIdle {
      assertEquals(RecordListQuery(text = "散歩", winner = RecordWinner.Abe, order = RecordOrder.Oldest), query)
      assertEquals(query, host.query)
      assertEquals(listOf(RecordsList), host.backStack.toList())
      assertFalse(requireNotNull(compose.activity.window.decorView.rootWindowInsets)
        .isVisible(WindowInsets.Type.ime()))
    }
    compose.onNodeWithTag("records-query-toolbar").assertIsDisplayed()
  }

  private fun show(host: Host) {
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 850.dp))) {
        BootstrapUi(host.screen())
      }
    } }
    compose.waitForIdle()
  }

  private fun stopAndResume() {
    // Match leaving for a browser and returning without destroying the Activity or its entry.
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.waitForIdle()
  }

  private fun readingPosition() = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
    .config[SemanticsProperties.VerticalScrollAxisRange].value()

  private class Host(detailOpen: Boolean = false) {
    val entries = (1..36).map { index ->
      val person = (index - 1) % 3
      RecordListEntry(index.toString().padStart(43, 'r'), "架空の散歩 $index：休日を楽しむには？",
        "架空の依頼者", RecordAvatar(null, "cyan"), Instant.parse("2026-10-02T12:00:00Z").minusSeconds(index.toLong()),
        listOf("アロナ", "プラナ", "安倍晋三AI")[person], "participant-${('a'.code + person).toChar()}")
    }
    val backStack = NavBackStack<NavKey>(RecordsList).apply {
      if (detailOpen) openRecord(entries.first().recordId)
    }
    var query by mutableStateOf(RecordListQuery())
      private set
    private var records by mutableStateOf(RecordListState.Ready.fromSaved(entries))
    private val session = SessionState.SignedIn(
      MobileSessionUser("架空の利用者", MobileAvatar("placeholder", "確認用", "cyan")),
      "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"))
    private val preview = RecordPreviewState.Ready(RecordPreview("復帰試験の架空の議題", "架空の結論", "プラナ",
      opinions = listOf("アロナ", "プラナ", "安倍晋三AI").mapIndexed { index, name ->
        RecordOpinion(name, "復帰試験の初回意見$index", "架空の初回本文$index",
          "復帰試験の最終案$index", "架空の最終本文$index。読み位置の確認用です。\n\n".repeat(40) + "復帰本文の末尾$index",
          participantSlot = "participant-${('a'.code + index).toChar()}")
      }, winnerSlot = "participant-b"), saved = true)

    fun screen() = BootstrapScreen.State(ThemeChoice.Dark, session, records = records, record = preview,
      selectedRecordId = (backStack.lastOrNull() as? RecordDetail)?.recordId, backStack = backStack.toList(),
      listQuery = query, eventSink = ::event)

    private fun event(event: BootstrapScreen.Event) {
      val previous = query
      when (event) {
        is BootstrapScreen.Event.SearchRecords -> query = query.copy(text = event.text)
        is BootstrapScreen.Event.SelectWinner -> query = query.copy(winner = event.winner)
        is BootstrapScreen.Event.SelectOrder -> query = query.copy(order = event.order)
        is BootstrapScreen.Event.OpenRecord -> backStack.openRecord(event.recordId)
        BootstrapScreen.Event.CloseRecord -> backStack.closeRecord()
        else -> Unit
      }
      if (query != previous) {
        records = RecordListState.Ready.fromSaved(query.sorted(entries.filter {
          query.acceptsWinner(it) && query.matches(it)
        }), records, total = entries.size)
      }
    }
  }
}
