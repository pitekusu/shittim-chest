package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

internal sealed interface RecordPreviewState {
  data object Idle : RecordPreviewState
  data object Loading : RecordPreviewState
  data object Empty : RecordPreviewState
  class Ready(val preview: RecordPreview) : RecordPreviewState
  class Error(val reason: RecordReadFailure) : RecordPreviewState
}

@Composable
internal fun RecordPreviewPanel(state: RecordPreviewState, onEvent: (BootstrapScreen.Event) -> Unit) {
  OutlinedCard(Modifier.fillMaxWidth()) {
    Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
      Text(stringResource(R.string.record_title), style = MaterialTheme.typography.titleLargeEmphasized,
        modifier = Modifier.semantics { heading() })
      when (state) {
        RecordPreviewState.Idle, RecordPreviewState.Loading -> {
          Text(stringResource(R.string.record_loading))
          CircularProgressIndicator()
        }
        RecordPreviewState.Empty -> Text(stringResource(R.string.record_empty))
        is RecordPreviewState.Error -> {
          Text(stringResource(if (state.reason == RecordReadFailure.NOT_FOUND)
            R.string.record_not_found else R.string.record_error))
          Button(onClick = { onEvent(BootstrapScreen.Event.RetryRecord) }) {
            Text(stringResource(R.string.record_retry))
          }
        }
        is RecordPreviewState.Ready -> {
          Text(stringResource(R.string.record_question), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          Text(state.preview.question, style = MaterialTheme.typography.titleMedium)
          Text(stringResource(R.string.record_winner), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          Text(state.preview.winnerName, style = MaterialTheme.typography.titleMediumEmphasized)
          Text(stringResource(R.string.record_decision), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          Text(state.preview.decision)
        }
      }
    }
  }
}
