package dev.pitekusu.shittim.records

import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimParticipantLabel
import dev.pitekusu.shittim.records.ui.ShittimProgress
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading

internal sealed interface RecordPreviewState {
  data object Idle : RecordPreviewState
  data object Loading : RecordPreviewState
  data object Empty : RecordPreviewState
  class Ready(val preview: RecordPreview, val saved: Boolean = false,
    val updating: Boolean = false, val refreshFailure: RecordReadFailure? = null) : RecordPreviewState
  class Error(val reason: RecordReadFailure) : RecordPreviewState
}

@Composable
internal fun RecordPreviewPanel(state: RecordPreviewState, onEvent: (BootstrapScreen.Event) -> Unit,
  recordId: String? = null,
  playedSections: Set<String> = emptySet(), onSectionSeen: (String) -> Unit = {},
  section: RecordDetailSection? = null, motionActive: Boolean = true) {
  ShittimPanel {
    if (section == null) ShittimSectionHeading(stringResource(R.string.record_title), kicker = "DISCUSSION RECORD",
      style = MaterialTheme.typography.titleLargeEmphasized)
    when (state) {
      RecordPreviewState.Idle, RecordPreviewState.Loading -> {
        ShittimProgress(stringResource(R.string.record_loading))
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
        if (section == null) {
        Text(stringResource(R.string.record_question), style = MaterialTheme.typography.labelLarge,
          color = MaterialTheme.colorScheme.primary)
        RecordMarkdown(state.preview.question)
        }
        if (section == null || section == RecordDetailSection.Opinions) {
        if (state.preview.opinions.isNotEmpty()) {
          ShittimSectionHeading(stringResource(R.string.record_opinions))
          state.preview.opinions.forEach { opinion ->
            HorizontalDivider()
            ShittimParticipantLabel(opinion.participantName, opinion.participantSlot)
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
        if (state.preview.opinions.isEmpty()) Text(stringResource(R.string.detail_opinions_missing))
        }
        if (section == null || section == RecordDetailSection.Voting) {
        state.preview.voting?.let { RecordVotingPanel(it, state.preview.winnerName,
          state.preview.winnerSlot, motionActive) }
          ?: Text(stringResource(R.string.detail_voting_missing))
        }
        if (section == null || section == RecordDetailSection.Result) {
        if (section != null) {
          RecordResultContent(state.preview, recordId, motionActive, playedSections, onSectionSeen)
        } else {
        Text(stringResource(R.string.record_winner), style = MaterialTheme.typography.labelLarge,
          color = MaterialTheme.colorScheme.primary)
        ShittimParticipantLabel(state.preview.winnerName, state.preview.winnerSlot, crowned = true)
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
          ShittimSectionHeading(stringResource(R.string.record_actions))
          state.preview.actions.forEach { Text(stringResource(R.string.record_list_item, it)) }
        }
        if (state.preview.caveats.isNotEmpty()) {
          ShittimSectionHeading(stringResource(R.string.record_caveats))
          state.preview.caveats.forEach { Text(stringResource(R.string.record_list_item, it)) }
        }
        }
        }
        if (section == null || section == RecordDetailSection.Affection) RecordAffectionPanel(state.preview.affection,
          recordId?.let { id -> "affection:$id" }, playedSections,
          onSectionSeen = onSectionSeen, motionActive = motionActive)
      }
    }
  }
}
