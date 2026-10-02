package dev.pitekusu.shittim.records

import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.text.style.TextOverflow
import androidx.paging.LoadState
import androidx.paging.LoadStates
import androidx.paging.PagingData
import androidx.paging.compose.LazyPagingItems
import androidx.paging.compose.itemKey
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimProgress
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow

internal sealed interface RecordListState {
  data object Idle : RecordListState
  class Error(val reason: RecordReadFailure) : RecordListState
  class Ready(
    val pages: Flow<PagingData<RecordListEntry>>,
    val loadedIds: Set<String>,
    val saved: Boolean = false,
    val refreshFailure: RecordReadFailure? = null,
    private val savedSnapshots: MutableStateFlow<PagingData<RecordListEntry>>? = null,
    val savedTotal: Int? = null,
  ) : RecordListState {
    companion object {
      fun fromSaved(entries: List<RecordListEntry>, previous: Ready? = null, total: Int = entries.size): Ready {
        // Static snapshots must publish completion so Compose leaves its initial Loading state.
        val snapshot = PagingData.from(entries, sourceLoadStates = LoadStates(
          refresh = LoadState.NotLoading(false),
          prepend = LoadState.NotLoading(true),
          append = LoadState.NotLoading(true),
        ))
        // Keep the Paging presenter alive: replacing its Flow briefly removes every card
        // and clamps the LazyColumn to the top before the next snapshot arrives.
        val snapshots = previous?.savedSnapshots ?: MutableStateFlow(snapshot)
        snapshots.value = snapshot
        return Ready(snapshots, entries.map { it.recordId }.toSet(),
          saved = true, savedSnapshots = snapshots, savedTotal = total)
      }
    }
  }
}

private val journalDate = DateTimeFormatter.ofPattern("yyyy.MM.dd EEEE", Locale.JAPAN)
private val journalTime = DateTimeFormatter.ofPattern("HH:mm", Locale.JAPAN).withZone(recordJournalZone)

internal enum class RecordQueryMode { Closed, Search, Filters }

internal fun LazyListScope.recordListItems(
  state: RecordListState,
  pagingItems: LazyPagingItems<RecordJournalRow>?,
  onEvent: (BootstrapScreen.Event) -> Unit,
  sync: RecordSyncState = RecordSyncState.Idle,
  query: RecordListQuery = RecordListQuery(),
  searching: Boolean = false,
  selectedRecordId: String? = null,
  searchCanFocus: Boolean = true,
  queryMode: RecordQueryMode = RecordQueryMode.Closed,
  onQueryModeChange: (RecordQueryMode) -> Unit = {},
  offline: Boolean = false,
) {
  val queryAvailable = (state is RecordListState.Ready && state.saved) || !query.isDefault
  item(key = "records-heading") {
    val focusManager = LocalFocusManager.current
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.End) {
      if (queryAvailable) {
        IconButton(onClick = {
          focusManager.clearFocus(force = true)
          onQueryModeChange(if (queryMode == RecordQueryMode.Search)
            RecordQueryMode.Closed else RecordQueryMode.Search)
        },
          modifier = Modifier.testTag("records-search-toggle").semantics { selected = query.searchesText }) {
          Icon(painterResource(R.drawable.ic_search),
            contentDescription = stringResource(if (queryMode == RecordQueryMode.Search)
              R.string.record_search_close else R.string.record_search_open),
            tint = if (query.searchesText || queryMode == RecordQueryMode.Search)
              MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
        }
        IconButton(onClick = {
          focusManager.clearFocus(force = true)
          onQueryModeChange(if (queryMode == RecordQueryMode.Filters)
            RecordQueryMode.Closed else RecordQueryMode.Filters)
        },
          modifier = Modifier.testTag("records-filter-toggle").semantics {
            selected = query.winner != RecordWinner.All || query.order != RecordOrder.Newest
          }) {
          Icon(painterResource(R.drawable.ic_filter),
            contentDescription = stringResource(if (queryMode == RecordQueryMode.Filters)
              R.string.record_filter_close else R.string.record_filter_open),
            tint = if (query.winner != RecordWinner.All || query.order != RecordOrder.Newest ||
              queryMode == RecordQueryMode.Filters) MaterialTheme.colorScheme.primary
              else MaterialTheme.colorScheme.onSurfaceVariant)
        }
      }
    }
  }
  if (queryAvailable && queryMode != RecordQueryMode.Closed) item(key = "records-query") {
    RecordQueryControls(query, searchCanFocus,
      showSearch = queryMode == RecordQueryMode.Search,
      showFilters = queryMode == RecordQueryMode.Filters,
      onEvent = onEvent)
  }
  if (queryMode == RecordQueryMode.Closed && !query.isDefault) item(key = "records-query-active") {
    Text(stringResource(R.string.record_query_active),
      style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
  }
  item(key = "records-context") {
    val ready = state as? RecordListState.Ready
    val shownCount = if (ready?.saved == true) ready.loadedIds.size
      else pagingItems?.itemSnapshotList?.items?.count { it is RecordJournalRow.Record } ?: 0
    RecordJournalContext(ready, shownCount, sync, searching, offline)
  }
  if (state !is RecordListState.Ready || pagingItems == null) {
    item(key = "records-waiting") {
      if (state is RecordListState.Error) {
        if (sync !is RecordSyncState.Failed) Text(stringResource(R.string.record_list_error))
      } else ShittimProgress(stringResource(R.string.record_list_loading))
    }
    return
  }
  when (pagingItems.loadState.refresh) {
    is LoadState.Loading -> item(key = "records-loading") {
      ShittimProgress(stringResource(R.string.record_list_loading))
    }
    is LoadState.Error -> item(key = "records-error") {
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(R.string.record_list_error))
        Button(onClick = pagingItems::retry) { Text(stringResource(R.string.record_retry)) }
      }
    }
    is LoadState.NotLoading -> if (pagingItems.itemCount == 0 && !searching) {
      item(key = "records-empty") { Text(stringResource(if (query.isDefault)
        R.string.record_empty else R.string.record_search_empty)) }
    }
  }
  items(count = pagingItems.itemCount, key = pagingItems.itemKey { it.stableKey },
    contentType = { index -> when (pagingItems.peek(index)) {
      is RecordJournalRow.DateHeading -> "journal-date"
      is RecordJournalRow.Record -> "journal-record"
      null -> null
    } }) { index ->
    when (val row = pagingItems[index]) {
      is RecordJournalRow.DateHeading -> Text(journalDate.format(row.date),
        style = MaterialTheme.typography.titleSmallEmphasized,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.fillMaxWidth().testTag("journal-date-${row.date}").semantics { heading() })
      is RecordJournalRow.Record -> RecordListCard(row.entry, row.entry.recordId == selectedRecordId) {
        onEvent(BootstrapScreen.Event.OpenRecord(row.entry.recordId))
      }
      null -> Unit
    }
  }
  when (val append = pagingItems.loadState.append) {
    is LoadState.Loading -> item(key = "records-more-loading") {
      ShittimProgress(stringResource(R.string.record_list_more_loading))
    }
    is LoadState.Error -> item(key = "records-more-error") {
      val expired = (append.error as? RecordReadException)?.failure == RecordReadFailure.CURSOR_INVALID
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(if (expired) R.string.record_list_cursor_expired else R.string.record_list_more_error))
        Button(onClick = if (expired) pagingItems::refresh else pagingItems::retry) {
          Text(stringResource(if (expired) R.string.record_list_restart else R.string.record_retry))
        }
      }
    }
    is LoadState.NotLoading -> Unit
  }
}

@Composable
private fun RecordJournalContext(ready: RecordListState.Ready?, shownCount: Int,
  sync: RecordSyncState, searching: Boolean, offline: Boolean) {
  val failure = (sync as? RecordSyncState.Failed)?.reason ?: ready?.refreshFailure
  val status = when {
    failure == RecordReadFailure.AUTH_REQUIRED -> R.string.journal_auth_error
    failure == RecordReadFailure.STORAGE_UNAVAILABLE -> R.string.journal_storage_error
    failure != null -> R.string.journal_sync_error
    offline -> R.string.journal_offline
    searching -> R.string.journal_searching
    sync == RecordSyncState.Running -> R.string.journal_sync_running
    else -> null
  }
  val lineHeight = with(LocalDensity.current) { MaterialTheme.typography.bodySmall.lineHeight.toDp() }
  // Reserve the same text-scaled area in every state; finishing sync must not move a reading card.
  Column(Modifier.fillMaxWidth().height(lineHeight * 3 + 8.dp).testTag("journal-context"),
    verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Box(Modifier.fillMaxWidth().height(lineHeight)) {
      ready?.savedTotal?.let { total ->
        Text(stringResource(if (shownCount == total) R.string.journal_saved_count
          else R.string.journal_filtered_count, if (shownCount == total) total else shownCount, total),
          style = MaterialTheme.typography.bodySmall, maxLines = 1, overflow = TextOverflow.Ellipsis,
          color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    }
    Row(Modifier.fillMaxWidth().height(lineHeight * 2),
      horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.Top) {
      if (sync == RecordSyncState.Running && !searching && !offline && failure == null) {
        CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
      }
      if (status != null) Text(stringResource(status), style = MaterialTheme.typography.bodySmall,
        color = if (failure != null) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
        maxLines = 2, overflow = TextOverflow.Ellipsis,
        modifier = Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite })
    }
  }
}

@Composable
private fun RecordListCard(item: RecordListEntry, isSelected: Boolean, onClick: () -> Unit) {
  OutlinedCard(onClick = onClick, modifier = Modifier.fillMaxWidth().testTag("journal-card-${item.recordId}")
    .semantics { selected = isSelected },
    shape = MaterialTheme.shapes.large,
    colors = CardDefaults.outlinedCardColors(containerColor = if (isSelected)
      MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceContainerLow,
      contentColor = MaterialTheme.colorScheme.onSurface)) {
    Column(Modifier.padding(ShittimSpacing.Medium), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        ShittimParticipantAvatar(item.winnerName, item.winnerSlot, size = 56.dp, crowned = true)
        Column(Modifier.weight(1f)) {
          Text(stringResource(R.string.record_list_winner, item.winnerName),
            style = MaterialTheme.typography.titleSmallEmphasized,
            color = shittimParticipantColor(item.winnerName, item.winnerSlot))
          if (isSelected) Text(stringResource(R.string.record_selected), style = MaterialTheme.typography.labelMedium)
        }
      }
      Text(item.questionPreview, style = MaterialTheme.typography.titleMedium,
        maxLines = 4, overflow = TextOverflow.Ellipsis,
        modifier = Modifier.testTag("journal-question-${item.recordId}"))
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        RequesterAvatar(item.requesterName, item.requesterAvatar)
        Text(item.requesterName, style = MaterialTheme.typography.labelMedium,
          color = MaterialTheme.colorScheme.onSurfaceVariant,
          modifier = Modifier.weight(1f), maxLines = 2, overflow = TextOverflow.Ellipsis)
        Text(journalTime.format(item.completedAt), style = MaterialTheme.typography.labelMedium,
          color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    }
  }
}

@Composable
private fun RequesterAvatar(name: String, avatar: RecordAvatar) {
  val background = when (avatar.fallbackVariant) {
    "pink" -> MaterialTheme.colorScheme.secondaryContainer
    "lavender" -> MaterialTheme.colorScheme.tertiaryContainer
    else -> MaterialTheme.colorScheme.primaryContainer
  }
  Box(Modifier.size(24.dp).border(1.dp, MaterialTheme.colorScheme.outlineVariant, CircleShape)
    .clip(CircleShape).clearAndSetSemantics {}, contentAlignment = Alignment.Center) {
    Surface(color = background, shape = CircleShape, modifier = Modifier.size(24.dp)) {
      Box(contentAlignment = Alignment.Center) {
        Text(name.take(1), style = MaterialTheme.typography.labelSmall)
      }
    }
    val context = LocalContext.current
    val request = remember(avatar.bytes, avatar.url) {
      (avatar.bytes ?: avatar.url)?.let { ImageRequest.Builder(context).data(it).build() }
    }
    if (request != null) {
      AsyncImage(model = request, contentDescription = null,
        modifier = Modifier.size(24.dp).clip(CircleShape), contentScale = ContentScale.Crop)
    }
  }
}
