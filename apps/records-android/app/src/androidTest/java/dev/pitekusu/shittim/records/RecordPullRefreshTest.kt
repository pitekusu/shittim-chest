package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.DeviceConfigurationOverride
import androidx.compose.ui.test.FontScale
import androidx.compose.ui.test.SemanticsMatcher
import androidx.compose.ui.test.WindowSize
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
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
@ScreenTest
class RecordPullRefreshTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun topPullRefreshesOnceKeepsSavedCardsAndAllowsAnotherPullOnlyAfterCompletion() {
    val host = Host()
    show(host)
    val first = "journal-card-${host.entries.first().recordId}"
    compose.onNodeWithTag(first).assertIsDisplayed()
    val top = compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top
    pull()
    compose.runOnIdle { assertEquals(1, host.refreshes) }
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    compose.onNodeWithTag("journal-sync-progress").assertIsDisplayed()
    compose.onNodeWithTag(first).assertIsDisplayed()
    assertEquals(top, compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top, 1f)
    pull()
    compose.runOnIdle { assertEquals(1, host.refreshes) }
    compose.runOnIdle { host.setSync(RecordSyncState.Completed) }
    assertEquals(top, compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top, 1f)
    pull()
    compose.runOnIdle { assertEquals(2, host.refreshes) }
  }

  @Test fun automaticSyncUsesAStatusBesideTheSavedCountWithoutCoveringTheBrand() {
    val host = Host()
    show(host)
    val first = "journal-card-${host.entries.first().recordId}"
    val top = compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top
    compose.runOnIdle { host.setSync(RecordSyncState.Running) }
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    compose.onNodeWithTag("journal-sync-progress").assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.journal_sync_running)).assertIsDisplayed()
    assertEquals(top, compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top, 1f)
    compose.runOnIdle { host.setSync(RecordSyncState.Completed) }
    compose.onNodeWithTag("journal-sync-progress").assertDoesNotExist()
    assertEquals(top, compose.onNodeWithTag(first).fetchSemanticsNode().boundsInRoot.top, 1f)
  }

  @Test fun unavailableSessionNeverShowsCommunicationProgressForAStillRunningWorker() {
    val host = Host(offline = true)
    host.setSync(RecordSyncState.Running)
    show(host)
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    compose.onNodeWithTag("journal-sync-progress").assertDoesNotExist()
    compose.onNodeWithText(compose.activity.getString(R.string.journal_offline)).assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.journal_sync_running)).assertDoesNotExist()
    compose.onNodeWithTag("journal-card-${host.entries.first().recordId}").assertIsDisplayed()
  }

  @Test fun draggingWithinTheListDoesNotRefreshAndSyncStateChangesKeepTheReadingPosition() {
    val host = Host()
    show(host)
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(
      hasTestTag("journal-card-${host.entries[23].recordId}"))
    pull()
    val position = scrollPosition()
    assertTrue("The downward drag must still be within the saved list", position > 0f)
    compose.runOnIdle { assertEquals(0, host.refreshes) }
    for (status in listOf(RecordSyncState.Running, RecordSyncState.Completed, RecordSyncState.Idle)) {
      compose.runOnIdle { host.setSync(status) }
      assertEquals(position, scrollPosition(), 0.01f)
    }
  }

  @Test fun authorizedOfflinePullQueuesRefreshWithoutAnEndlessSpinnerAndRevocationDisablesIt() {
    val host = Host(offline = true)
    show(host)
    pull()
    compose.runOnIdle {
      assertEquals(1, host.refreshes)
      assertEquals(RecordSyncState.Idle, host.state.value.sync)
    }
    compose.onNodeWithTag("journal-card-${host.entries.first().recordId}").assertIsDisplayed()
    // The queued offline operation is not a running WorkManager operation.
    compose.onAllNodes(SemanticsMatcher.expectValue(SemanticsProperties.ProgressBarRangeInfo,
      ProgressBarRangeInfo.Indeterminate) and (hasTestTag("records-refresh-indicator") or
      hasAnyAncestor(hasTestTag("records-refresh-indicator"))), useUnmergedTree = true).assertCountEquals(0)
    compose.runOnIdle { host.setAuthorized(false) }
    compose.onNodeWithTag("records-pull-refresh").assertDoesNotExist()
    compose.onNodeWithTag("journal-card-${host.entries.first().recordId}").assertDoesNotExist()
    pull("bootstrap-content")
    compose.runOnIdle { assertEquals(1, host.refreshes) }
  }

  @Test fun visibleListCannotRefreshWhileDetailOrFullScreenSearchIsOpen() {
    val host = Host()
    show(host, wide = true)
    compose.runOnIdle { host.select(host.entries.first().recordId) }
    compose.onNodeWithTag("records-pull-refresh").assertIsDisplayed()
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    pull()
    compose.runOnIdle { assertEquals(0, host.refreshes) }
    compose.runOnIdle { host.select(null) }
    compose.onNodeWithTag("records-search-toggle").performClick()
    compose.onNodeWithTag("record-search").assertIsDisplayed()
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    pull("records-search-results")
    compose.runOnIdle { assertEquals(0, host.refreshes) }
    compose.onNodeWithTag("records-search-close").performClick()
    pull()
    compose.runOnIdle { assertEquals(1, host.refreshes) }
  }

  @Test fun captureRefreshingIndicatorWhenExplicitlyRequested() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureJournal") != "true") return
    val host = Host()
    val large = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(
        DpSize(if (large.value) 320.dp else 420.dp, 900.dp)) then
        DeviceConfigurationOverride.FontScale(if (large.value) 2f else 1f)) { BootstrapUi(host.state.value) }
    } }
    for ((name, theme, largeText) in listOf(
      Triple("meaningful-sync-light", ThemeChoice.Light, false),
      Triple("meaningful-sync-dark", ThemeChoice.Dark, false),
      Triple("meaningful-sync-large-text", ThemeChoice.Dark, true),
    )) {
      compose.runOnIdle { large.value = largeText; host.setTheme(theme); host.setSync(RecordSyncState.Running) }
      compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
      compose.onNodeWithTag("journal-sync-progress").assertIsDisplayed()
      compose.waitForIdle()
      File(compose.activity.cacheDir, "$name.png").outputStream().use { output ->
        InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
          .compress(Bitmap.CompressFormat.PNG, 100, output)
      }
    }
    compose.runOnIdle { large.value = false; host.setTheme(ThemeChoice.Dark); host.setOffline(true) }
    compose.onNodeWithTag("journal-sync-progress").assertDoesNotExist()
    compose.onNodeWithTag("records-refresh-indicator").assertDoesNotExist()
    compose.waitForIdle()
    File(compose.activity.cacheDir, "meaningful-sync-offline.png").outputStream().use { output ->
      InstrumentationRegistry.getInstrumentation().uiAutomation.takeScreenshot()
        .compress(Bitmap.CompressFormat.PNG, 100, output)
    }
  }

  private fun show(host: Host, wide: Boolean = false) {
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      DeviceConfigurationOverride(DeviceConfigurationOverride.WindowSize(
        DpSize(if (wide) 1000.dp else 420.dp, 900.dp))) { BootstrapUi(host.state.value) }
    } }
    compose.waitForIdle()
  }

  private fun pull(tag: String = "records-pull-refresh") {
    compose.onNodeWithTag(tag).performTouchInput {
      swipe(Offset(center.x, height * 0.16f), Offset(center.x, height * 0.82f), durationMillis = 500)
    }
    compose.waitForIdle()
  }

  private fun scrollPosition(): Float = compose.onNodeWithTag("bootstrap-content").fetchSemanticsNode()
    .config[SemanticsProperties.VerticalScrollAxisRange].value()

  /** Stable saved Paging data; the fixture models scheduling, never makes a network request. */
  private class Host(private var offline: Boolean = false) {
    val entries = (1..30).map { index ->
      RecordListEntry(index.toString().padStart(43, 'r'),
        "架空の記録 $index：休日の散歩や読書を無理なく楽しむには？", "架空の依頼者",
        RecordAvatar(null, "cyan"), Instant.parse("2026-10-02T12:00:00Z").minusSeconds(index.toLong()),
        "アロナ", "participant-a")
    }
    private val records = RecordListState.Ready.fromSaved(entries)
    private val signedIn = SessionState.SignedIn(
      MobileSessionUser("架空の利用者", MobileAvatar("placeholder", "確認用", "cyan")),
      "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"))
    private val session get() = if (offline) SessionState.Unavailable else signedIn
    private var authorized = true
    private var selected: String? = null
    private var sync: RecordSyncState = RecordSyncState.Idle
    private var theme = ThemeChoice.Dark
    var refreshes = 0
      private set
    val state = mutableStateOf(screen())

    fun setAuthorized(value: Boolean) { authorized = value; state.value = screen() }
    fun setSync(value: RecordSyncState) { sync = value; state.value = screen() }
    fun select(value: String?) { selected = value; state.value = screen() }
    fun setTheme(value: ThemeChoice) { theme = value; state.value = screen() }
    fun setOffline(value: Boolean) { offline = value; state.value = screen() }

    private fun screen() = BootstrapScreen.State(theme, session,
      records = records, sync = sync, canReadRecords = authorized, selectedRecordId = selected,
      record = RecordPreviewState.Ready(RecordPreview("架空の議題全文", "架空の結論", "アロナ"), saved = true),
      eventSink = ::event)

    private fun event(event: BootstrapScreen.Event) {
      when (event) {
        BootstrapScreen.Event.RefreshRecords -> {
          refreshes++
          if (!offline) sync = RecordSyncState.Running
        }
        is BootstrapScreen.Event.OpenRecord -> selected = event.recordId
        BootstrapScreen.Event.CloseRecord -> selected = null
        else -> Unit
      }
      state.value = screen()
    }
  }
}
