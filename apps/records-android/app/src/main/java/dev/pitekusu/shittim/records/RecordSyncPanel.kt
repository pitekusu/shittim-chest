package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

internal sealed interface RecordSyncState {
  data object Idle : RecordSyncState
  data object Running : RecordSyncState
  data object Paused : RecordSyncState
  data object Completed : RecordSyncState
  class Failed(val reason: RecordReadFailure) : RecordSyncState
}

@Composable
internal fun RecordSyncPanel(state: RecordSyncState, onEvent: (BootstrapScreen.Event) -> Unit) {
  OutlinedCard(Modifier.fillMaxWidth()) {
    Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
      Text(stringResource(R.string.record_sync_title),
        style = MaterialTheme.typography.titleLargeEmphasized,
        modifier = Modifier.semantics { heading() })
      val description = when (state) {
        RecordSyncState.Idle -> R.string.record_sync_hint
        RecordSyncState.Running -> R.string.record_sync_running
        RecordSyncState.Paused -> R.string.record_sync_paused
        RecordSyncState.Completed -> R.string.record_sync_completed
        is RecordSyncState.Failed -> when (state.reason) {
          RecordReadFailure.AUTH_REQUIRED -> R.string.record_sync_auth_error
          RecordReadFailure.STORAGE_UNAVAILABLE -> R.string.record_sync_storage_error
          else -> R.string.record_sync_error
        }
      }
      Text(stringResource(description), modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite })
      if (state == RecordSyncState.Running) {
        // The total is not known until enumeration finishes; never invent a percentage.
        LinearProgressIndicator(Modifier.fillMaxWidth())
      } else {
        Button(onClick = { onEvent(BootstrapScreen.Event.StartSync) }) {
          Text(stringResource(when (state) {
            RecordSyncState.Paused, is RecordSyncState.Failed -> R.string.record_sync_resume
            RecordSyncState.Completed -> R.string.record_sync_again
            else -> R.string.record_sync_start
          }))
        }
      }
    }
  }
}
