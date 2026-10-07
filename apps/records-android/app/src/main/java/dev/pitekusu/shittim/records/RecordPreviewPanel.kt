package dev.pitekusu.shittim.records

import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimProgress
import com.mikepenz.markdown.model.MarkdownState

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
  section: RecordDetailSection = RecordDetailSection.Result, motionActive: Boolean = true,
  opinion: RecordOpinion? = null, finalOpinion: Boolean = false, opinionMarkdown: MarkdownState? = null,
  affectionIndex: Int = 0,
  minimumVisibleTop: () -> Float = { Float.NEGATIVE_INFINITY },
  decisionMarkdown: MarkdownState = com.mikepenz.markdown.model.rememberMarkdownState(
    (state as? RecordPreviewState.Ready)?.preview?.decision.orEmpty())) {
  ShittimPanel {
    when (state) {
      RecordPreviewState.Idle, RecordPreviewState.Loading ->
        ShittimProgress(stringResource(R.string.record_loading))
      RecordPreviewState.Empty -> Text(stringResource(R.string.record_not_saved))
      is RecordPreviewState.Error -> {
        Text(stringResource(if (state.reason == RecordReadFailure.NOT_FOUND)
          R.string.record_not_found else R.string.record_error))
        Button(onClick = { onEvent(BootstrapScreen.Event.RetryRecord) }) {
          Text(stringResource(R.string.record_retry))
        }
      }
      is RecordPreviewState.Ready -> {
        if (state.updating) Text(stringResource(R.string.record_refreshing))
        if (state.refreshFailure != null) Text(stringResource(if (state.refreshFailure == RecordReadFailure.STORAGE_UNAVAILABLE)
          R.string.record_save_failed else R.string.record_refresh_failed))
        when (section) {
          RecordDetailSection.Result ->
            RecordResultContent(state.preview, recordId, motionActive, playedSections, onSectionSeen, decisionMarkdown)
          RecordDetailSection.Opinions ->
            if (opinion != null && opinionMarkdown != null) RecordOpinionContent(opinion, finalOpinion, opinionMarkdown)
            else Text(stringResource(R.string.detail_opinions_missing))
          RecordDetailSection.Voting ->
            state.preview.voting?.let { RecordVotingPanel(it, state.preview.winnerName,
              state.preview.winnerSlot, motionActive) }
              ?: Text(stringResource(R.string.detail_voting_missing))
          RecordDetailSection.Affection ->
            RecordAffectionPanel(state.preview.affection, recordId?.let { "affection:$it" },
              playedSections, onSectionSeen = onSectionSeen, motionActive = motionActive,
              selectedIndex = affectionIndex, minimumVisibleTop = minimumVisibleTop)
        }
      }
    }
  }
}
