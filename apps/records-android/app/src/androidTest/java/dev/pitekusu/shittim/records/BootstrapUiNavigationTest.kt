package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.hasText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.paging.PagingData
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
import kotlinx.coroutines.flow.flowOf
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BootstrapUiNavigationTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun systemBackClosesSelectedRecordInsteadOfFinishingActivity() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val user = MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan"))
    val state = BootstrapScreen.State(
      ThemeChoice.System,
      SessionState.SignedIn(user, "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/records/${"a".repeat(43)}"),
      record = RecordPreviewState.Ready(RecordPreview("架空の議題", "架空の結論", "アロナ")),
      selectedRecordId = "a".repeat(43),
      eventSink = events::add,
    )
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state) } }
    compose.waitForIdle()
    compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
    compose.runOnIdle { assertEquals(listOf(BootstrapScreen.Event.CloseRecord), events) }
  }

  @Test fun listKeepsItsScrollPositionWhileDetailIsOpen() {
    val id = "a".repeat(43)
    val user = MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan"))
    val session = SessionState.SignedIn(user, "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")
    val entries = (1..12).map { index ->
      RecordListEntry(index.toString().padStart(43, 'a'), "議題 $index", "架空の依頼者",
        RecordAvatar(null, "cyan"), Instant.parse("2026-09-24T00:00:00Z"), "アロナ")
    }
    val recordList = RecordListState.Ready(flowOf(PagingData.from(entries)), entries.map { it.recordId }.toSet())
    val list = BootstrapScreen.State(ThemeChoice.System, session,
      records = recordList, eventSink = {})
    val detail = BootstrapScreen.State(ThemeChoice.System, session,
      records = recordList,
      record = RecordPreviewState.Ready(RecordPreview("架空の議題", "架空の結論", "アロナ")),
      selectedRecordId = id, eventSink = {})
    val state = mutableStateOf(list)
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    // The first card may start below the fold on a smaller CI device.
    compose.waitForIdle()
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText("議題 12"))
    compose.onNodeWithText("議題 12").assertIsDisplayed()
    compose.runOnIdle { state.value = detail }
    compose.runOnIdle { state.value = list }
    compose.onNodeWithText("議題 12").assertIsDisplayed()
  }

  @Test fun automaticSyncStatusIsHiddenAfterLeavingTheAuthenticatedSession() {
    val user = MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan"))
    val state = mutableStateOf(BootstrapScreen.State(ThemeChoice.System,
      SessionState.SignedIn(user, "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/"),
      sync = RecordSyncState.Running, eventSink = {}))
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    val status = compose.activity.getString(R.string.record_sync_running)
    compose.onNodeWithTag("bootstrap-content").performScrollToNode(hasText(status))
    compose.onNodeWithText(status).assertIsDisplayed()
    compose.runOnIdle { state.value = BootstrapScreen.State(ThemeChoice.System) {} }
    compose.onNodeWithText(status).assertDoesNotExist()
  }
}
