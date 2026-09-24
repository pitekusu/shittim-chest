package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
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
      SessionState.SignedIn(user, Instant.parse("2027-01-01T00:00:00Z"), "/records/${"a".repeat(43)}"),
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
    val session = SessionState.SignedIn(user, Instant.parse("2027-01-01T00:00:00Z"), "/")
    val page = RecordListPage((1..12).map { index ->
      RecordListEntry(index.toString().padStart(43, 'a'), "議題 $index", "架空の依頼者",
        RecordAvatar(null, "cyan"), Instant.parse("2026-09-24T00:00:00Z"), "アロナ")
    }, false)
    val list = BootstrapScreen.State(ThemeChoice.System, session,
      records = RecordListState.Ready(page), eventSink = {})
    val detail = BootstrapScreen.State(ThemeChoice.System, session,
      records = RecordListState.Ready(page),
      record = RecordPreviewState.Ready(RecordPreview("架空の議題", "架空の結論", "アロナ")),
      selectedRecordId = id, eventSink = {})
    val state = mutableStateOf(list)
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    compose.onNodeWithText("議題 12").performScrollTo().assertIsDisplayed()
    compose.runOnIdle { state.value = detail }
    compose.runOnIdle { state.value = list }
    compose.onNodeWithText("議題 12").assertIsDisplayed()
  }
}
