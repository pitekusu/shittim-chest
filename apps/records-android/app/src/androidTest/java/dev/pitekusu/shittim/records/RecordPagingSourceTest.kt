package dev.pitekusu.shittim.records

import androidx.paging.PagingSource
import java.time.Instant
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class RecordPagingSourceTest {
  private val first = entry("a".repeat(43))
  private val second = entry("b".repeat(43))

  @Test
  fun loadsNextPageOnceAndDropsOverlappingRecords() = runBlocking {
    val requested = mutableListOf<String?>()
    val loaded = mutableListOf<String>()
    val source = RecordPagingSource(
      loadPage = { cursor ->
        requested += cursor
        if (cursor == null) RecordListPage(listOf(first), "next.cursor")
        else RecordListPage(listOf(first, second), null)
      },
      onLoaded = { entries -> loaded += entries.map { it.recordId } },
    )

    val initial = source.load(refresh()) as PagingSource.LoadResult.Page
    val appended = source.load(append("next.cursor")) as PagingSource.LoadResult.Page

    assertEquals(listOf(null, "next.cursor"), requested)
    assertEquals(listOf(first.recordId), initial.data.map { it.recordId })
    assertEquals("next.cursor", initial.nextKey)
    assertEquals(listOf(second.recordId), appended.data.map { it.recordId })
    assertNull(appended.nextKey)
    assertEquals(listOf(first.recordId, second.recordId), loaded)
  }

  @Test
  fun expiredCursorCanRetryWithoutLosingEarlierPage() = runBlocking {
    var attempts = 0
    val source = RecordPagingSource(loadPage = { cursor ->
      if (cursor == null) RecordListPage(listOf(first), "next.cursor")
      else {
        attempts++
        if (attempts == 1) throw RecordReadException(RecordReadFailure.CURSOR_INVALID)
        RecordListPage(listOf(second), null)
      }
    })

    assertTrue(source.load(refresh()) is PagingSource.LoadResult.Page)
    val failure = source.load(append("next.cursor")) as PagingSource.LoadResult.Error
    assertEquals(RecordReadFailure.CURSOR_INVALID, (failure.throwable as RecordReadException).failure)
    val retry = source.load(append("next.cursor")) as PagingSource.LoadResult.Page
    assertEquals(listOf(second.recordId), retry.data.map { it.recordId })
    assertEquals(2, attempts)
  }

  @Test
  fun repeatedCursorStopsPagination() = runBlocking {
    val source = RecordPagingSource(loadPage = { cursor ->
      if (cursor == null) RecordListPage(listOf(first), "next.cursor")
      else RecordListPage(listOf(second), "next.cursor")
    })
    assertTrue(source.load(refresh()) is PagingSource.LoadResult.Page)
    val failure = source.load(append("next.cursor")) as PagingSource.LoadResult.Error
    assertEquals(RecordReadFailure.INVALID_RESPONSE, (failure.throwable as RecordReadException).failure)
  }

  private fun refresh() = PagingSource.LoadParams.Refresh<String>(null, 12, false)

  private fun append(cursor: String) = PagingSource.LoadParams.Append(cursor, 12, false)

  private fun entry(id: String) = RecordListEntry(id, "架空の議題", "架空の依頼者",
    RecordAvatar(null, "cyan"), Instant.parse("2026-09-24T00:00:00Z"), "アロナ")
}
