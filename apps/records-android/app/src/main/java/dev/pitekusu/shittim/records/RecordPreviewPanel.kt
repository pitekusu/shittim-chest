package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
  class Ready(val preview: RecordPreview, val saved: Boolean = false,
    val updating: Boolean = false, val refreshFailure: RecordReadFailure? = null) : RecordPreviewState
  class Error(val reason: RecordReadFailure) : RecordPreviewState
}

@Composable
internal fun RecordPreviewPanel(state: RecordPreviewState, onEvent: (BootstrapScreen.Event) -> Unit) {
  OutlinedCard(Modifier.fillMaxWidth()) {
    Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
      TextButton(onClick = { onEvent(BootstrapScreen.Event.CloseRecord) }) {
        Text(stringResource(R.string.record_close))
      }
      Text(stringResource(R.string.record_title), style = MaterialTheme.typography.titleLargeEmphasized,
        modifier = Modifier.semantics { heading() })
      when (state) {
        RecordPreviewState.Idle, RecordPreviewState.Loading -> {
          Text(stringResource(R.string.record_loading))
          CircularProgressIndicator()
        }
        RecordPreviewState.Empty -> Text(stringResource(R.string.record_not_saved))
        is RecordPreviewState.Error -> {
          Text(stringResource(if (state.reason == RecordReadFailure.NOT_FOUND)
            R.string.record_not_found else R.string.record_error))
          Button(onClick = { onEvent(BootstrapScreen.Event.RetryRecord) }) {
            Text(stringResource(R.string.record_retry))
          }
        }
        is RecordPreviewState.Ready -> {
          if (state.saved) Text(stringResource(R.string.record_saved), color = MaterialTheme.colorScheme.primary)
          if (state.updating) Text(stringResource(R.string.record_refreshing))
          if (state.refreshFailure != null) Text(stringResource(if (state.refreshFailure == RecordReadFailure.STORAGE_UNAVAILABLE)
            R.string.record_save_failed else R.string.record_refresh_failed))
          Text(stringResource(R.string.record_question), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          RecordMarkdown(state.preview.question)
          if (state.preview.opinions.isNotEmpty()) {
            Text(stringResource(R.string.record_opinions),
              style = MaterialTheme.typography.titleMediumEmphasized,
              modifier = Modifier.semantics { heading() })
            state.preview.opinions.forEach { opinion ->
              HorizontalDivider()
              Text(opinion.participantName, style = MaterialTheme.typography.titleMediumEmphasized,
                modifier = Modifier.semantics { heading() })
              Text(stringResource(R.string.record_initial_opinion),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary)
              Text(opinion.summary, style = MaterialTheme.typography.titleSmall)
              RecordMarkdown(opinion.initialProposal)
              Text(stringResource(R.string.record_final_proposal),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary)
              Text(opinion.finalTitle, style = MaterialTheme.typography.titleSmall)
              RecordMarkdown(opinion.finalProposal)
            }
          }
          state.preview.voting?.let { RecordVotingPanel(it) }
          Text(stringResource(R.string.record_winner), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          Text(state.preview.winnerName, style = MaterialTheme.typography.titleMediumEmphasized)
          state.preview.victoryMessage?.let { message ->
            Text(stringResource(R.string.record_victory_message),
              style = MaterialTheme.typography.labelLarge,
              color = MaterialTheme.colorScheme.primary)
            Text(message, style = MaterialTheme.typography.bodyLarge)
          }
          Text(stringResource(R.string.record_decision), style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.primary)
          RecordMarkdown(state.preview.decision)
          if (state.preview.actions.isNotEmpty()) {
            Text(stringResource(R.string.record_actions),
              style = MaterialTheme.typography.titleMediumEmphasized,
              modifier = Modifier.semantics { heading() })
            state.preview.actions.forEach { Text(stringResource(R.string.record_list_item, it)) }
          }
          if (state.preview.caveats.isNotEmpty()) {
            Text(stringResource(R.string.record_caveats),
              style = MaterialTheme.typography.titleMediumEmphasized,
              modifier = Modifier.semantics { heading() })
            state.preview.caveats.forEach { Text(stringResource(R.string.record_list_item, it)) }
          }
          RecordAffectionPanel(state.preview.affection)
        }
      }
    }
  }
}
