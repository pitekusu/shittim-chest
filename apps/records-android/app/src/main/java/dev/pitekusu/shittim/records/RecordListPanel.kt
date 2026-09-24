package dev.pitekusu.shittim.records

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
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.paging.LoadState
import androidx.paging.PagingData
import androidx.paging.compose.LazyPagingItems
import androidx.paging.compose.itemKey
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale
import kotlinx.coroutines.flow.Flow

internal sealed interface RecordListState {
  data object Idle : RecordListState
  class Ready(
    val pages: Flow<PagingData<RecordListEntry>>,
    val loadedIds: Set<String>,
  ) : RecordListState
}

private val japanZone = ZoneId.of("Asia/Tokyo")
private val recordDate = DateTimeFormatter.ofPattern("yyyy.MM.dd", Locale.JAPAN).withZone(japanZone)

internal fun LazyListScope.recordListItems(
  state: RecordListState,
  pagingItems: LazyPagingItems<RecordListEntry>?,
  onEvent: (BootstrapScreen.Event) -> Unit,
) {
  item(key = "records-heading") {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
      Text("RECORDS ARCHIVE", fontFamily = ShittimDisplayFont,
        color = MaterialTheme.colorScheme.primary)
      Text(stringResource(R.string.record_title), style = MaterialTheme.typography.headlineSmallEmphasized,
        modifier = Modifier.semantics { heading() })
    }
  }
  if (state !is RecordListState.Ready || pagingItems == null) {
    item(key = "records-waiting") { LoadingRecords() }
    return
  }
  when (pagingItems.loadState.refresh) {
    is LoadState.Loading -> item(key = "records-loading") { LoadingRecords() }
    is LoadState.Error -> item(key = "records-error") {
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(stringResource(R.string.record_list_error))
        Button(onClick = pagingItems::retry) { Text(stringResource(R.string.record_retry)) }
      }
    }
    is LoadState.NotLoading -> if (pagingItems.itemCount == 0) {
      item(key = "records-empty") { Text(stringResource(R.string.record_empty)) }
    }
  }
  items(count = pagingItems.itemCount, key = pagingItems.itemKey { it.recordId }) { index ->
    pagingItems[index]?.let { entry ->
      RecordListCard(entry) { onEvent(BootstrapScreen.Event.OpenRecord(entry.recordId)) }
    }
  }
  when (val append = pagingItems.loadState.append) {
    is LoadState.Loading -> item(key = "records-more-loading") {
      Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
        CircularProgressIndicator(Modifier.size(24.dp))
        Text(stringResource(R.string.record_list_more_loading))
      }
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
private fun LoadingRecords() {
  Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
    CircularProgressIndicator(Modifier.size(24.dp))
    Text(stringResource(R.string.record_list_loading))
  }
}

@Composable
private fun RecordListCard(item: RecordListEntry, onClick: () -> Unit) {
  OutlinedCard(onClick = onClick, modifier = Modifier.fillMaxWidth()) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
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
      Text(item.questionPreview, style = MaterialTheme.typography.bodyLarge)
      Text(stringResource(R.string.record_list_winner, item.winnerName),
        style = MaterialTheme.typography.labelMedium,
        color = MaterialTheme.colorScheme.primary)
    }
  }
}

@Composable
private fun RequesterAvatar(name: String, avatar: RecordAvatar) {
  val background = when (avatar.fallbackVariant) {
    "pink" -> MaterialTheme.colorScheme.tertiaryContainer
    "lavender" -> MaterialTheme.colorScheme.secondaryContainer
    else -> MaterialTheme.colorScheme.primaryContainer
  }
  Box(Modifier.size(48.dp).clip(CircleShape), contentAlignment = Alignment.Center) {
    Surface(color = background, shape = CircleShape, modifier = Modifier.size(48.dp)) {
      Box(contentAlignment = Alignment.Center) {
        Text(name.take(1), style = MaterialTheme.typography.titleMediumEmphasized)
      }
    }
    val context = LocalContext.current
    val request = remember(avatar.url) {
      avatar.url?.let { ImageRequest.Builder(context).data(it).build() }
    }
    if (request != null) {
      AsyncImage(model = request, contentDescription = null,
        modifier = Modifier.size(48.dp).clip(CircleShape), contentScale = ContentScale.Crop)
    }
  }
}
