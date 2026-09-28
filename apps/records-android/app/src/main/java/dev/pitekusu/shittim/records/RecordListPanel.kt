package dev.pitekusu.shittim.records

import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.HorizontalDivider
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
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.paging.LoadState
import androidx.paging.LoadStates
import androidx.paging.PagingData
import androidx.paging.compose.LazyPagingItems
import androidx.paging.compose.itemKey
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimProgress
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import java.time.ZoneId
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

private val japanZone = ZoneId.of("Asia/Tokyo")
private val recordDate = DateTimeFormatter.ofPattern("yyyy.MM.dd", Locale.JAPAN).withZone(japanZone)

internal fun LazyListScope.recordListItems(
  state: RecordListState,
  pagingItems: LazyPagingItems<RecordListEntry>?,
  onEvent: (BootstrapScreen.Event) -> Unit,
  sync: RecordSyncState = RecordSyncState.Idle,
  query: RecordListQuery = RecordListQuery(),
  searching: Boolean = false,
  selectedRecordId: String? = null,
) {
  item(key = "records-heading") {
    ShittimSectionHeading(stringResource(R.string.record_title), kicker = "RECORDS ARCHIVE",
      style = MaterialTheme.typography.headlineSmallEmphasized)
  }
  if ((state is RecordListState.Ready && state.saved) || !query.isDefault) item(key = "records-query") {
    RecordQueryControls(query, onEvent)
  }
  if (sync == RecordSyncState.Running || sync is RecordSyncState.Failed) {
    item(key = "records-sync-status") { RecordSyncStatus(sync) }
  }
  if (state !is RecordListState.Ready || pagingItems == null) {
    item(key = "records-waiting") {
      if (state is RecordListState.Error) {
        if (sync !is RecordSyncState.Failed) Text(stringResource(R.string.record_list_error))
      } else ShittimProgress(stringResource(R.string.record_list_loading))
    }
    return
  }
  if (state.saved || state.refreshFailure != null) item(key = "records-source") {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
      if (state.saved) Text(stringResource(R.string.record_saved), color = MaterialTheme.colorScheme.primary)
      if (searching) Text(stringResource(R.string.record_searching), color = MaterialTheme.colorScheme.onSurfaceVariant)
      else state.savedTotal?.let { total ->
        Text(stringResource(R.string.record_search_count, pagingItems.itemCount, total),
          color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
      if (state.refreshFailure != null) Text(stringResource(if (state.refreshFailure == RecordReadFailure.STORAGE_UNAVAILABLE)
        R.string.record_save_failed else R.string.record_refresh_failed))
    }
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
  items(count = pagingItems.itemCount, key = pagingItems.itemKey { it.recordId }) { index ->
    pagingItems[index]?.let { entry ->
      RecordListCard(entry, entry.recordId == selectedRecordId) {
        onEvent(BootstrapScreen.Event.OpenRecord(entry.recordId))
      }
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
private fun RecordListCard(item: RecordListEntry, isSelected: Boolean, onClick: () -> Unit) {
  OutlinedCard(onClick = onClick, modifier = Modifier.fillMaxWidth().semantics { selected = isSelected },
    shape = MaterialTheme.shapes.medium,
    colors = CardDefaults.outlinedCardColors(containerColor = if (isSelected)
      MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surfaceContainerLow,
      contentColor = MaterialTheme.colorScheme.onSurface)) {
    Column(Modifier.padding(ShittimSpacing.Medium), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        RequesterAvatar(item.requesterName, item.requesterAvatar)
        Column(Modifier.weight(1f)) {
          Text(item.requesterName, style = MaterialTheme.typography.titleSmallEmphasized)
          Text(recordDate.format(item.completedAt),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
      }
      HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
      Text(item.questionPreview, style = MaterialTheme.typography.bodyLarge)
      if (isSelected) Text(stringResource(R.string.record_selected), style = MaterialTheme.typography.labelMedium)
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        ShittimParticipantAvatar(item.winnerName, item.winnerSlot, size = 32.dp, crowned = true)
        Text(stringResource(R.string.record_list_winner, item.winnerName),
          style = MaterialTheme.typography.labelMedium,
          color = shittimParticipantColor(item.winnerName, item.winnerSlot))
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
  Box(Modifier.size(48.dp).border(1.dp, MaterialTheme.colorScheme.outlineVariant, CircleShape)
    .clip(CircleShape).clearAndSetSemantics {}, contentAlignment = Alignment.Center) {
    Surface(color = background, shape = CircleShape, modifier = Modifier.size(48.dp)) {
      Box(contentAlignment = Alignment.Center) {
        Text(name.take(1), style = MaterialTheme.typography.titleMediumEmphasized)
      }
    }
    val context = LocalContext.current
    val request = remember(avatar.bytes, avatar.url) {
      (avatar.bytes ?: avatar.url)?.let { ImageRequest.Builder(context).data(it).build() }
    }
    if (request != null) {
      AsyncImage(model = request, contentDescription = null,
        modifier = Modifier.size(48.dp).clip(CircleShape), contentScale = ContentScale.Crop)
    }
  }
}
