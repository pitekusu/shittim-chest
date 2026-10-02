package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsFocused
import androidx.compose.ui.test.assertIsNotSelected
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.then
import androidx.compose.ui.unit.DpSize
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordSearchNavigationTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun fullScreenSearchOpensOnlyOnRequestAndRetainsCriteriaWhenClosed() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-active-query").assertDoesNotExist()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsDisplayed().assertIsFocused().performTextInput("散歩")
    compose.onNodeWithTag("records-query-toolbar").assertDoesNotExist()
    compose.onNodeWithTag("records-search-close").performClick()
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-query-text-chip").assertIsDisplayed()
    compose.runOnIdle { assertEquals("散歩", host.query.text) }
    compose.onNodeWithTag("records-query-text-chip").performClick()
    compose.onNodeWithTag("records-active-query").assertDoesNotExist()
    compose.runOnIdle { assertTrue(host.query.isDefault) }
  }

  @Test fun filtersApplyImmediatelyAndClosingWithoutChangesKeepsTheListPosition() {
    val host = Host()
    show(host)
    val anchor = "journal-card-${host.entries[16].recordId}"
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasTestTag(anchor))
    val top = compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("records-filter-sheet").assertIsDisplayed()
    compose.onNodeWithTag("records-query-toolbar").assertDoesNotExist()
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag(anchor).assertIsDisplayed()
    assertEquals(top, compose.onNodeWithTag(anchor).fetchSemanticsNode().boundsInRoot.top, 1f)
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("winner-Plana").performClick()
    compose.runOnIdle { assertEquals(RecordWinner.Plana, host.query.winner) }
    compose.onNodeWithTag("order-Oldest").performScrollTo().performClick()
    compose.runOnIdle { assertEquals(RecordOrder.Oldest, host.query.order) }
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-query-winner-chip").assertIsDisplayed()
    compose.onNodeWithTag("records-query-order-chip").assertIsDisplayed()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNode(hasTestTag("records-query-winner-chip") and
      hasAnyAncestor(hasTestTag("records-search-results"))).assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(RecordWinner.All, host.query.winner) }
    compose.onNode(hasTestTag("records-query-order-chip") and
      hasAnyAncestor(hasTestTag("records-search-results"))).assertIsDisplayed().performClick()
    compose.runOnIdle { assertTrue(host.query.isDefault) }
    compose.onNodeWithTag("records-search-close").performClick()
    compose.onNodeWithTag("records-active-query").assertDoesNotExist()
  }

  @Test fun searchResultOpeningTransfersReadingPositionWithoutRestoringSearchFocusOnBack() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsFocused().performTextInput("架空")
    val id = host.entries[18].recordId
    compose.onNodeWithTag("records-search-results").performScrollToNode(hasTestTag("journal-card-$id"))
    compose.onNodeWithTag("journal-card-$id").performClick()
    compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("records-search-screen", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("records-query-toolbar").assertDoesNotExist()
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { compose.activity.onBackPressedDispatcher.onBackPressed() }
      repeat(8) {
        compose.mainClock.advanceTimeBy(80)
        compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
      }
    } finally { compose.mainClock.autoAdvance = true }
    compose.onNodeWithTag("journal-card-$id").assertIsDisplayed().assertIsNotSelected()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsFocused()
    compose.runOnIdle { assertEquals("架空", host.query.text) }
  }

  @Test fun requesterAppliesWithWinnerAndSearchAndCanBeRemovedFromEitherScreen() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("requester-0").performClick().assertIsSelected()
    compose.onNodeWithTag("winner-Plana").performScrollTo().performClick()
    compose.runOnIdle {
      assertEquals("架空の依頼者A", host.query.requesterName)
      assertEquals(listOf("架空の依頼者A", "架空の依頼者B"), host.state.value.requesters.map { it.displayName })
      assertEquals(setOf(5, 11, 17, 23).map { it.toString().padStart(43, 'q') }.toSet(),
        (host.state.value.records as RecordListState.Ready).loadedIds)
    }
    compose.onNodeWithTag("requester-1").performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-query-requester-chip").assertIsDisplayed()
    compose.onNodeWithTag("records-filter-toggle").assertIsSelected()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").performTextInput("散歩 2")
    compose.runOnIdle {
      assertEquals(setOf("23".padStart(43, 'q')), (host.state.value.records as RecordListState.Ready).loadedIds)
    }
    compose.onNode(hasTestTag("records-query-requester-chip") and
      hasAnyAncestor(hasTestTag("records-search-results"))).assertIsDisplayed().performClick()
    compose.runOnIdle {
      assertEquals(null, host.query.requesterName)
      assertEquals("散歩 2", host.query.text)
      assertEquals(RecordWinner.Plana, host.query.winner)
      assertEquals(setOf(2, 20, 23).map { it.toString().padStart(43, 'q') }.toSet(),
        (host.state.value.records as RecordListState.Ready).loadedIds)
    }
    compose.onNodeWithTag("records-search-close").performClick()
    compose.onNodeWithTag("records-query-requester-chip").assertDoesNotExist()
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("requester-1").performClick().assertIsSelected()
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-query-requester-chip").assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(null, host.query.requesterName) }
  }

  @Test fun authorizationLossRemovesSearchAndFilterContentImmediately() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsFocused().performTextInput("架空")
    compose.runOnIdle { host.setAuthorized(false) }
    compose.onNodeWithTag("record-search", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("records-search-screen", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("journal-card-${host.entries.first().recordId}").assertDoesNotExist()
    compose.runOnIdle { host.setAuthorized(true) }
    compose.onNodeWithTag("record-search").assertDoesNotExist()
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("records-filter-sheet").assertIsDisplayed()
    compose.onNodeWithTag("requester-0").assertIsDisplayed()
    compose.runOnIdle { host.setAuthorized(false) }
    compose.onNodeWithTag("records-filter-sheet", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("winner-All", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("requester-0", useUnmergedTree = true).assertDoesNotExist()
  }

  @Test fun narrowLargeTextKeepsToolbarAndAllWinnerChoicesReachable() {
    val host = Host()
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(320.dp, 800.dp)) then
        DeviceConfigurationOverride.FontScale(2f)) { BootstrapUi(host.state.value) }
    } }
    compose.onNodeWithTag("records-search-toggle").assertIsDisplayed()
    compose.onNodeWithTag("records-filter-toggle").assertIsDisplayed().performClick()
    for (index in 0..1) {
      compose.onNodeWithTag("requester-$index").performScrollTo().performClick().assertIsSelected()
    }
    compose.onNodeWithTag("requester-all").performScrollTo().performClick().assertIsSelected()
    for (winner in listOf(RecordWinner.Arona, RecordWinner.Plana, RecordWinner.Abe, RecordWinner.All)) {
      compose.onNodeWithTag("winner-${winner.name}").performScrollTo().performClick()
      compose.runOnIdle { assertEquals(winner, host.query.winner) }
    }
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-filter-sheet").assertDoesNotExist()
  }

  @Test fun captureSearchAndFilterPreview() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureJournal") != "true") return
    val bytes = compose.activity.resources.openRawResource(R.drawable.participant_b).use { it.readBytes() }
    val host = Host(bytes)
    show(host)
    compose.onNodeWithTag("records-filter-toggle").performClick()
    compose.onNodeWithTag("requester-0").performClick()
    capture("requester-filter-dark", "records-filter-sheet")
    compose.runOnIdle { host.setTheme(ThemeChoice.Light) }
    capture("requester-filter-light", "records-filter-sheet")
    compose.runOnIdle { host.setTheme(ThemeChoice.Dark) }
    capture("journal-filter-sheet", "records-filter-sheet")
    compose.onNodeWithTag("winner-Plana").performScrollTo().performClick()
    capture("journal-filter-selected", "records-filter-sheet")
    compose.onNodeWithTag("records-filter-done").performScrollTo().performClick()
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").performTextInput("散歩")
    capture("journal-fullscreen-search", "records-search-screen")
    compose.onNodeWithTag("records-search-close").performClick()
  }

  private fun capture(name: String, tag: String) {
    compose.waitForIdle()
    compose.onNodeWithTag(tag).assertIsDisplayed()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      // Dialog roots are translated surfaces; capture the composed screen, not cropped local bounds.
      InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        .compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private fun show(host: Host) {
    compose.activityRule.scenario.onActivity { it.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(DpSize(420.dp, 900.dp))) {
        BootstrapUi(host.state.value)
      }
    } }
  }

  /** The fake API boundary publishes the same saved Paging Flow, like the real repository. */
  private class Host(avatarBytes: ByteArray? = null) {
    val entries = (1..24).map { index ->
      val slot = (index - 1) % 3
      RecordListEntry(index.toString().padStart(43, 'q'),
        "架空の散歩 $index：休日に無理なく気分転換するには？", "架空の依頼者${if (index % 2 == 1) "A" else "B"}",
        RecordAvatar(null, "cyan", bytes = avatarBytes.takeIf { index % 2 == 1 }),
        Instant.parse("2026-10-02T14:00:00Z").minusSeconds(index.toLong()),
        listOf("アロナ", "プラナ", "安倍晋三AI")[slot], listOf("participant-a", "participant-b", "participant-c")[slot])
    }
    var query = RecordListQuery()
      private set
    private var selected: String? = null
    private var authorized = true
    private var theme = ThemeChoice.Dark
    private var records = RecordListState.Ready.fromSaved(entries)
    private val session = SessionState.SignedIn(
      MobileSessionUser("架空の利用者", MobileAvatar("placeholder", "確認用", "cyan")),
      "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")
    val state = mutableStateOf(screen())

    fun setAuthorized(value: Boolean) {
      authorized = value
      selected = null
      state.value = screen()
    }

    fun setTheme(value: ThemeChoice) {
      theme = value
      state.value = screen()
    }

    private fun screen() = BootstrapScreen.State(theme, session,
      records = records, record = RecordPreviewState.Ready(
        RecordPreview("架空の議題全文", "架空の結論", "アロナ")), selectedRecordId = selected,
      canReadRecords = authorized, listQuery = query,
      requesters = if (authorized) recordRequesterChoices(entries) else emptyList(), eventSink = ::event)

    private fun event(event: BootstrapScreen.Event) {
      val old = query
      when (event) {
        is BootstrapScreen.Event.SearchRecords -> query = query.copy(text = event.text)
        is BootstrapScreen.Event.SelectWinner -> query = query.copy(winner = event.winner)
        is BootstrapScreen.Event.SelectRequester -> query = query.copy(requesterName = event.displayName)
        is BootstrapScreen.Event.SelectOrder -> query = query.copy(order = event.order)
        BootstrapScreen.Event.ClearRecordQuery -> query = RecordListQuery()
        is BootstrapScreen.Event.OpenRecord -> selected = event.recordId
        BootstrapScreen.Event.CloseRecord -> selected = null
        else -> Unit
      }
      if (old != query) {
        val filtered = query.sorted(entries.filter {
          query.acceptsWinner(it) && query.acceptsRequester(it) && query.matches(it)
        })
        records = RecordListState.Ready.fromSaved(filtered, records, total = entries.size)
      }
      state.value = screen()
    }
  }
}
