package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.paging.compose.collectAsLazyPagingItems
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.time.Instant
import java.time.LocalDate
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordJournalTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  private fun entry(id: String, completedAt: String) = RecordListEntry(id.repeat(43),
    "架空の記録 $id", "架空の依頼者", RecordAvatar(null, "cyan"),
    Instant.parse(completedAt), "アロナ", "participant-a")

  @Test fun tokyoDayBoundariesAndSortChangesKeepOneStableHeaderPerDay() {
    val beforeMidnight = entry("a", "2026-10-01T14:59:59Z")
    val atMidnight = entry("b", "2026-10-01T15:00:00Z")
    val sameDay = entry("c", "2026-10-01T16:00:00Z")
    val entries = listOf(beforeMidnight, atMidnight, sameDay)
    val state = mutableStateOf(RecordListState.Ready.fromSaved(RecordListQuery().sorted(entries)))
    var displayed = emptyList<RecordJournalRow>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        val pages = remember(state.value.pages) { state.value.pages.asRecordJournal() }
        val items = pages.collectAsLazyPagingItems()
        displayed = items.itemSnapshotList.items
        LazyColumn { recordListItems(state.value, items, {}) }
      } }
    }
    compose.waitUntil(5_000) { displayed.size == 5 }
    val newestHeaders = listOf(LocalDate.parse("2026-10-02"), LocalDate.parse("2026-10-01"))
    compose.runOnIdle {
      assertEquals(newestHeaders, displayed.filterIsInstance<RecordJournalRow.DateHeading>().map { it.date })
      assertEquals(listOf(sameDay.recordId, atMidnight.recordId, beforeMidnight.recordId),
        displayed.filterIsInstance<RecordJournalRow.Record>().map { it.stableKey })
      assertEquals(5, displayed.map { it.stableKey }.distinct().size)
      state.value = RecordListState.Ready.fromSaved(
        RecordListQuery(order = RecordOrder.Oldest).sorted(entries), state.value)
    }
    compose.waitUntil(5_000) {
      displayed.filterIsInstance<RecordJournalRow.Record>().firstOrNull()?.entry?.recordId == beforeMidnight.recordId
    }
    compose.runOnIdle {
      assertEquals(newestHeaders.reversed(), displayed.filterIsInstance<RecordJournalRow.DateHeading>().map { it.date })
      assertEquals(listOf("journal-date:2026-10-01", "journal-date:2026-10-02"),
        displayed.filterIsInstance<RecordJournalRow.DateHeading>().map { it.stableKey })
    }
  }

  @Test fun filteringCreatesHeadersFromTheResultWithoutInventingEmptyDays() {
    val hidden = entry("a", "2026-09-30T16:00:00Z")
    val visible = entry("b", "2026-10-01T16:00:00Z")
    val state = RecordListState.Ready.fromSaved(listOf(visible), total = 2)
    var displayed = emptyList<RecordJournalRow>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        val pages = remember(state.pages) { state.pages.asRecordJournal() }
        val items = pages.collectAsLazyPagingItems()
        displayed = items.itemSnapshotList.items
        LazyColumn { recordListItems(state, items, {}, query = RecordListQuery("架空")) }
      } }
    }
    compose.waitUntil(5_000) { displayed.size == 2 }
    compose.onNodeWithText(compose.activity.getString(R.string.journal_filtered_count, 1, 2)).assertIsDisplayed()
    compose.runOnIdle {
      assertEquals(listOf(LocalDate.parse("2026-10-02")),
        displayed.filterIsInstance<RecordJournalRow.DateHeading>().map { it.date })
      assertEquals(listOf(visible.recordId), displayed.filterIsInstance<RecordJournalRow.Record>().map { it.stableKey })
      assertEquals(LocalDate.parse("2026-10-01"), recordJournalDate(hidden))
    }
  }
}
