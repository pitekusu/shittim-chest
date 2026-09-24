package dev.pitekusu.shittim.records

import androidx.paging.PagingSource
import androidx.paging.PagingState
import kotlin.coroutines.cancellation.CancellationException

/** The signed API cursor only advances forward; a refresh must start at the newest page. */
internal class RecordPagingSource(
  private val loadPage: suspend (String?) -> RecordListPage,
  private val onLoaded: (List<RecordListEntry>) -> Unit = {},
) : PagingSource<String, RecordListEntry>() {
  private val seenIds = mutableSetOf<String>()
  private val seenCursors = mutableSetOf<String>()

  override suspend fun load(params: LoadParams<String>): LoadResult<String, RecordListEntry> {
    if (params is LoadParams.Prepend) return LoadResult.Page(emptyList(), null, null)
    val cursor = params.key
    if (cursor != null && cursor in seenCursors) {
      return LoadResult.Error(RecordReadException(RecordReadFailure.INVALID_RESPONSE))
    }
    return try {
      val page = loadPage(cursor)
      if (page.nextCursor != null && (page.nextCursor == cursor || page.nextCursor in seenCursors)) {
        return LoadResult.Error(RecordReadException(RecordReadFailure.INVALID_RESPONSE))
      }
      if (cursor != null) seenCursors.add(cursor)
      val unique = page.items.filter { seenIds.add(it.recordId) }
      onLoaded(unique)
      LoadResult.Page(unique, prevKey = null, nextKey = page.nextCursor)
    } catch (error: CancellationException) {
      throw error
    } catch (error: RecordReadException) {
      LoadResult.Error(error)
    } catch (_: Exception) {
      LoadResult.Error(RecordReadException(RecordReadFailure.INVALID_RESPONSE))
    }
  }

  override fun getRefreshKey(state: PagingState<String, RecordListEntry>): String? = null
}
