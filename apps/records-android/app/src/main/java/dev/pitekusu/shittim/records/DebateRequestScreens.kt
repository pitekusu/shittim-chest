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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
  onResult: (String) -> Unit, onReauth: () -> Unit, onNew: () -> Unit, modifier: Modifier = Modifier,
  demo: Boolean = false, onControl: (String) -> Unit = {}, onCheck: () -> Unit = {}, onResend: () -> Unit = {}) {
  val receipt = debateReceipt(id, state, submission)
  var confirmation by remember(id, receipt?.attemptToken) { mutableStateOf<String?>(null) }
  val ownWorkspace = submission.workspace?.takeIf { it.requestId == id }
  val failure = state.failure?.takeIf { it != DebateFailure.NOT_FOUND }
    ?: submission.failure.takeIf { ownWorkspace != null }
  Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(ShittimSpacing.Medium)
    .testTag("debate-status"), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    Text(stringResource(R.string.debate_status_title), style = MaterialTheme.typography.headlineMediumEmphasized)
    val question = receipt?.question ?: ownWorkspace?.frozenQuestion
    if (question != null) ShittimPanel { Text(question) }
    DebateProgressCard(receipt, motionAllowed = online && state.failure == null && failure == null)
    ShittimPanel {
      if (receipt == null && (state.loading || submission.busy)) CircularProgressIndicator()
      if (!online) Text(stringResource(R.string.debate_requests_online))
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
      if (!demo) {
        if (submission.workspace?.pendingControl?.let { it.requestId != id } == true) {
          Text(stringResource(R.string.debate_control_other_pending))
        }
        if (state.controlBusy) CircularProgressIndicator()
        if (state.controlPending) {
          Text(stringResource(R.string.debate_control_pending))
          TextButton(onClick = onCheck, enabled = online && !state.loading && !state.controlBusy) {
            Text(stringResource(R.string.debate_control_check))
          }
          if (state.controlCanResend) TextButton(onClick = { confirmation = "resend" },
            enabled = online && !state.loading && !state.controlBusy,
            modifier = Modifier.testTag("debate-control-resend")) { Text(stringResource(R.string.debate_control_resend)) }
        }
        if (state.controlFailure != null) {
          Text(stringResource(when (state.controlFailure) {
            DebateFailure.REAUTH_REQUIRED -> R.string.debate_reauth
            DebateFailure.FORBIDDEN -> R.string.debate_forbidden
            DebateFailure.CONFLICT -> R.string.debate_control_conflict
            DebateFailure.STORAGE -> R.string.debate_storage_error
            else -> R.string.debate_control_failed
          }), color = MaterialTheme.colorScheme.error)
          if (state.controlFailure == DebateFailure.REAUTH_REQUIRED) TextButton(onClick = onReauth,
            enabled = online && !state.controlBusy) { Text(stringResource(R.string.session_login)) }
        }
        val controlEnabled = online && !state.loading && !state.controlBusy && !state.controlPending &&
          !submission.busy && state.failure == null && submission.workspace?.pendingControl == null
        if (receipt?.canCancel == true) TextButton(onClick = { confirmation = "cancel" }, enabled = controlEnabled,
          modifier = Modifier.testTag("debate-cancel")) { Text(stringResource(R.string.debate_cancel)) }
        if (receipt?.canRetry == true) Button(onClick = { confirmation = "retry" }, enabled = controlEnabled,
          modifier = Modifier.testTag("debate-restart")) { Text(stringResource(R.string.debate_restart)) }
      }
      if (receipt?.terminal != true) Text(stringResource(if (demo) R.string.review_demo_progress_hint else R.string.debate_no_cancel),
        style = MaterialTheme.typography.bodySmall)
      if (receipt?.terminal == true) TextButton(onClick = onNew) { Text(stringResource(R.string.debate_new)) }
    }
  }
  val action = confirmation
  if (action != null && online && !state.controlBusy &&
    (if (action == "resend") state.controlCanResend else !state.controlPending &&
      (if (action == "cancel") receipt?.canCancel == true else receipt?.canRetry == true))) {
    AlertDialog(onDismissRequest = { confirmation = null },
      title = { Text(stringResource(when (action) {
        "cancel" -> R.string.debate_cancel_title
        "resend" -> R.string.debate_control_resend_title
        else -> R.string.debate_restart_title
      })) },
      text = { Text(stringResource(when (action) {
        "cancel" -> R.string.debate_cancel_message
        "resend" -> R.string.debate_control_resend_message
        else -> R.string.debate_restart_message
      })) },
      confirmButton = { TextButton(onClick = { confirmation = null; if (action == "resend") onResend() else onControl(action) },
        modifier = Modifier.testTag("debate-control-confirm")) {
        Text(stringResource(when (action) {
          "cancel" -> R.string.debate_cancel
          "resend" -> R.string.debate_control_resend
          else -> R.string.debate_restart
        }))
      } },
      dismissButton = { TextButton(onClick = { confirmation = null }) { Text(stringResource(R.string.review_demo_cancel)) } })
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
