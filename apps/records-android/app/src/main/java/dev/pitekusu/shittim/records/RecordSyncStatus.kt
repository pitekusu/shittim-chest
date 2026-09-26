package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.size
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

internal sealed interface RecordSyncState {
  data object Idle : RecordSyncState
  data object Running : RecordSyncState
  data object Completed : RecordSyncState
  class Failed(val reason: RecordReadFailure) : RecordSyncState
}

@Composable
internal fun RecordSyncStatus(state: RecordSyncState) {
  val description = when (state) {
    RecordSyncState.Running -> R.string.record_sync_running
    is RecordSyncState.Failed -> when (state.reason) {
      RecordReadFailure.AUTH_REQUIRED -> R.string.record_sync_auth_error
      RecordReadFailure.STORAGE_UNAVAILABLE -> R.string.record_sync_storage_error
      else -> R.string.record_sync_error
    }
    RecordSyncState.Idle, RecordSyncState.Completed -> return
  }
  Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp),
    verticalAlignment = Alignment.CenterVertically) {
    if (state == RecordSyncState.Running) {
      // Background enumeration has no known total; do not invent a percentage.
      CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
    }
    Text(stringResource(description), style = MaterialTheme.typography.bodySmall,
      color = if (state is RecordSyncState.Failed) MaterialTheme.colorScheme.error
        else MaterialTheme.colorScheme.onSurfaceVariant,
      modifier = Modifier.weight(1f).semantics { liveRegion = LiveRegionMode.Polite })
  }
}
