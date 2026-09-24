package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordListPanelTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun cardOpensExistingDetailAndErrorDoesNotRetainPreviousRecord() {
    val id = "r".repeat(43)
    val state = mutableStateOf<RecordListState>(RecordListState.Ready(RecordListPage(listOf(
      RecordListEntry(id, "架空の議題", "架空の依頼者", RecordAvatar(null, "cyan"),
        Instant.parse("2026-09-24T00:00:00Z"), "アロナ")), false)))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordListPanel(state.value, events::add) } }
    }
    compose.onNodeWithText("架空の依頼者").assertIsDisplayed()
    compose.onNodeWithText("架空の議題").performClick()
    assertEquals(id, (events.single() as BootstrapScreen.Event.OpenRecord).recordId)
    compose.runOnIdle { state.value = RecordListState.Error(RecordReadFailure.UNAVAILABLE) }
    compose.onNodeWithText("架空の議題").assertDoesNotExist()
    compose.onNodeWithText(compose.activity.getString(R.string.record_retry)).performClick()
    assertEquals(BootstrapScreen.Event.RetryRecords, events.last())
  }
}
