package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.runtime.mutableStateOf
import androidx.paging.PagingData
import androidx.paging.compose.collectAsLazyPagingItems
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.time.Instant
import kotlinx.coroutines.flow.flowOf
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
    val entries = listOf(
      RecordListEntry(id, "架空の議題", "架空の依頼者", RecordAvatar(null, "cyan"),
        Instant.parse("2026-09-24T00:00:00Z"), "アロナ"))
    val state = mutableStateOf<RecordListState>(RecordListState.Ready(
      flowOf(PagingData.from(entries)), setOf(id)))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        val current = state.value
        val items = (current as? RecordListState.Ready)?.pages?.collectAsLazyPagingItems()
        LazyColumn { recordListItems(current, items, events::add) }
      } }
    }
    compose.waitUntil(5_000) {
      compose.onAllNodesWithText("架空の議題").fetchSemanticsNodes().isNotEmpty()
    }
    compose.onNodeWithText("架空の依頼者").assertIsDisplayed()
    compose.onNodeWithText("架空の議題").performClick()
    assertEquals(id, (events.single() as BootstrapScreen.Event.OpenRecord).recordId)
    compose.runOnIdle { state.value = RecordListState.Idle }
    compose.onNodeWithText("架空の議題").assertDoesNotExist()
    assertEquals(1, events.size)
  }
}
