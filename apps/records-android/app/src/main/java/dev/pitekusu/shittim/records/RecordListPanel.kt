package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
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
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

internal sealed interface RecordListState {
  data object Idle : RecordListState
  data object Loading : RecordListState
  data object Empty : RecordListState
  class Ready(val page: RecordListPage) : RecordListState
  class Error(val reason: RecordReadFailure) : RecordListState
}

private val japanZone = ZoneId.of("Asia/Tokyo")
private val recordDate = DateTimeFormatter.ofPattern("yyyy.MM.dd", Locale.JAPAN).withZone(japanZone)

@Composable
internal fun RecordListPanel(
  state: RecordListState,
  onEvent: (BootstrapScreen.Event) -> Unit,
) {
  Column(verticalArrangement = Arrangement.spacedBy(16.dp)) {
    Text("RECORDS ARCHIVE", fontFamily = ShittimDisplayFont,
      color = MaterialTheme.colorScheme.primary)
    Text(stringResource(R.string.record_title), style = MaterialTheme.typography.headlineSmallEmphasized,
      modifier = Modifier.semantics { heading() })
    when (state) {
      RecordListState.Idle, RecordListState.Loading -> {
        Text(stringResource(R.string.record_list_loading))
        CircularProgressIndicator()
      }
      RecordListState.Empty -> Text(stringResource(R.string.record_empty))
      is RecordListState.Error -> {
        Text(stringResource(R.string.record_list_error))
        Button(onClick = { onEvent(BootstrapScreen.Event.RetryRecords) }) {
          Text(stringResource(R.string.record_retry))
        }
      }
      is RecordListState.Ready -> {
        state.page.items.forEach { item ->
          RecordListCard(item) {
            onEvent(BootstrapScreen.Event.OpenRecord(item.recordId))
          }
        }
        if (state.page.hasMore) {
          Text(stringResource(R.string.record_list_first_page),
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.bodySmall)
        }
      }
    }
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
