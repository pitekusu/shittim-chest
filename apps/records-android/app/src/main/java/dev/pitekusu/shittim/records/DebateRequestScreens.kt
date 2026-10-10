package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

private val debateRequestDateTime = DateTimeFormatter.ofPattern("uuuu年M月d日 HH:mm", Locale.JAPAN)
  .withZone(ZoneId.of("Asia/Tokyo"))

internal fun formatDebateRequestDateTime(createdAt: String): String =
  debateRequestDateTime.format(Instant.parse(createdAt))

@Composable
internal fun DebateRequestsScreen(state: DebateHistoryState, online: Boolean,
  onOpen: (String) -> Unit, onMore: () -> Unit, onRetry: () -> Unit, modifier: Modifier = Modifier) {
  LazyColumn(modifier.fillMaxSize().padding(ShittimSpacing.Medium).testTag("debate-requests"),
    verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    item { Text(stringResource(R.string.debate_requests), style = MaterialTheme.typography.headlineMediumEmphasized) }
    items(state.items, key = { it.requestId }) { request ->
      Card(onClick = { onOpen(request.requestId) }, modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(ShittimSpacing.Medium), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
          Text(stringResource(debateStatusLabel(request.status)), color = MaterialTheme.colorScheme.primary,
            style = MaterialTheme.typography.labelLarge)
          Text(request.question, maxLines = 4, overflow = TextOverflow.Ellipsis)
          Text(stringResource(R.string.debate_request_created_at, formatDebateRequestDateTime(request.createdAt)),
            style = MaterialTheme.typography.labelSmall)
        }
      }
    }
    if (state.items.isEmpty() && state.loaded && state.failure == null) item { Text(stringResource(R.string.debate_requests_empty)) }
    if (!online) item { Text(stringResource(R.string.debate_requests_online)) }
    if (state.loading) item { CircularProgressIndicator() }
    if (state.failure != null) item {
      Text(stringResource(R.string.debate_unavailable), color = MaterialTheme.colorScheme.error)
      TextButton(onClick = onRetry, enabled = online && !state.loading) { Text(stringResource(R.string.session_retry)) }
    }
    if (state.nextCursor != null && state.failure == null) item {
      TextButton(onClick = onMore, enabled = online && !state.loading) { Text(stringResource(R.string.debate_requests_more)) }
    }
  }
}

@Composable
internal fun DebateRequestStatusScreen(id: String, state: DebateStatusState,
  submission: DebateSubmissionState, online: Boolean, onRetry: () -> Unit,
  onResult: (String) -> Unit, onReauth: () -> Unit, onNew: () -> Unit, modifier: Modifier = Modifier) {
  val receipt = debateReceipt(id, state, submission)
  val ownWorkspace = submission.workspace?.takeIf { it.requestId == id }
  Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(ShittimSpacing.Medium)
    .testTag("debate-status"), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    Text(stringResource(R.string.debate_status_title), style = MaterialTheme.typography.headlineMediumEmphasized)
    val question = receipt?.question ?: ownWorkspace?.frozenQuestion
    if (question != null) ShittimPanel { Text(question) }
    ShittimPanel {
      Text(stringResource(if (receipt == null) R.string.debate_unknown else debateStatusLabel(receipt.status)),
        style = MaterialTheme.typography.titleLargeEmphasized)
      receipt?.takeIf { !it.terminal }?.phase?.let {
        Text(stringResource(debatePhaseLabel(it)), color = MaterialTheme.colorScheme.primary)
      }
      if (state.loading || submission.busy) CircularProgressIndicator()
      if (!online) Text(stringResource(R.string.debate_requests_online))
      val failure = state.failure?.takeIf { it != DebateFailure.NOT_FOUND }
        ?: submission.failure.takeIf { ownWorkspace != null }
      if (failure != null) {
        Text(stringResource(when (failure) {
          DebateFailure.REAUTH_REQUIRED -> R.string.debate_reauth
          DebateFailure.FORBIDDEN -> R.string.debate_forbidden
          DebateFailure.QUEUE_FULL -> R.string.debate_queue_full
          DebateFailure.STORAGE -> R.string.debate_storage_error
          else -> R.string.debate_unavailable
        }), color = MaterialTheme.colorScheme.error)
        if (failure == DebateFailure.REAUTH_REQUIRED) Button(onClick = onReauth,
          enabled = online && !submission.busy) { Text(stringResource(R.string.session_login)) }
      }
      if (ownWorkspace != null && submission.confirmedMissing && receipt == null) {
        Text(stringResource(R.string.debate_not_accepted))
        Button(onClick = onRetry, enabled = online && !submission.busy && !state.loading &&
          failure != DebateFailure.REAUTH_REQUIRED,
          modifier = Modifier.testTag("debate-retry-same-id")) { Text(stringResource(R.string.debate_retry)) }
      } else if (receipt == null && state.failure == DebateFailure.NOT_FOUND) Text(stringResource(R.string.debate_request_missing))
      if (receipt?.status == "published") receipt.recordId?.let { recordId ->
        Button(onClick = { onResult(recordId) }, modifier = Modifier.testTag("debate-open-result")) {
          Text(stringResource(R.string.debate_open_result))
        }
      }
      if (receipt?.terminal != true) Text(stringResource(R.string.debate_no_cancel),
        style = MaterialTheme.typography.bodySmall)
      if (receipt?.terminal == true) TextButton(onClick = onNew) { Text(stringResource(R.string.debate_new)) }
    }
  }
}

internal fun debatePhaseLabel(phase: String): Int = when (phase) {
  "scoring_affection" -> R.string.debate_phase_affection
  "preparing_evidence" -> R.string.debate_phase_evidence
  "forming_preferences", "selecting_candidates" -> R.string.debate_phase_preferences
  "collecting_initial_opinions" -> R.string.debate_phase_initial
  "discussing" -> R.string.debate_phase_discussing
  "collecting_final_proposals" -> R.string.debate_phase_final
  "selecting_winner" -> R.string.debate_phase_voting
  "generating_decision" -> R.string.debate_phase_decision
  else -> R.string.debate_status_title
}
