package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import androidx.compose.foundation.layout.Row

@Composable
internal fun DebateComposeScreen(state: DebateSubmissionState, online: Boolean,
  onEdit: (String) -> Unit, onSubmit: () -> Unit, onCheck: () -> Unit,
  onRetry: () -> Unit, onReauth: () -> Unit, onNew: () -> Unit,
  modifier: Modifier = Modifier) {
  Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(ShittimSpacing.Medium)
    .testTag("debate-compose"), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
      ShittimParticipantAvatar("アロナ", "participant-a", 48.dp)
      ShittimParticipantAvatar("プラナ", "participant-b", 48.dp)
      ShittimParticipantAvatar("安倍晋三AI", "participant-c", 48.dp)
    }
    Text(stringResource(R.string.debate_start), style = MaterialTheme.typography.headlineMediumEmphasized)
    Text(stringResource(R.string.debate_publish_notice), style = MaterialTheme.typography.bodyMedium)
    val workspace = state.workspace
    if (workspace == null) {
      if (state.failure == null) CircularProgressIndicator()
      else Text(stringResource(R.string.debate_storage_error), color = MaterialTheme.colorScheme.error)
      return@Column
    }
    val frozen = workspace.requestId != null
    OutlinedTextField(value = workspace.frozenQuestion ?: workspace.draft, onValueChange = onEdit,
      readOnly = frozen, enabled = !state.busy, label = { Text(stringResource(R.string.debate_question)) },
      supportingText = { Text(stringResource(R.string.debate_characters,
        (workspace.frozenQuestion ?: workspace.draft).codePointCount(0, (workspace.frozenQuestion ?: workspace.draft).length))) },
      minLines = 6, modifier = Modifier.fillMaxWidth().heightIn(min = 180.dp).testTag("debate-question"))
    if (state.saving) Text(stringResource(R.string.debate_saving), style = MaterialTheme.typography.labelSmall)
    if (!online) Text(stringResource(R.string.debate_offline))
    if (state.failure != null) {
      Text(stringResource(when (state.failure) {
        DebateFailure.STORAGE -> R.string.debate_storage_error
        DebateFailure.REAUTH_REQUIRED -> R.string.debate_reauth
        DebateFailure.FORBIDDEN -> R.string.debate_forbidden
        DebateFailure.QUEUE_FULL -> R.string.debate_queue_full
        else -> R.string.debate_unavailable
      }), color = MaterialTheme.colorScheme.error)
      if (state.failure == DebateFailure.REAUTH_REQUIRED) Button(onClick = onReauth,
        enabled = !state.busy && online) { Text(stringResource(R.string.session_login)) }
    }
    if (state.busy) CircularProgressIndicator()
    if (!frozen) Button(onClick = onSubmit,
      enabled = online && !state.busy && validDebateQuestion(workspace.draft),
      modifier = Modifier.fillMaxWidth().testTag("debate-submit")) { Text(stringResource(R.string.debate_submit)) }
    else {
      ShittimPanel {
        val receipt = workspace.receipt
        Text(stringResource(if (receipt == null) R.string.debate_unknown else debateStatusLabel(receipt.status)),
          style = MaterialTheme.typography.titleMediumEmphasized)
        Text(stringResource(R.string.debate_no_cancel))
        if (receipt == null) TextButton(onClick = onCheck, enabled = online && !state.busy) {
          Text(stringResource(R.string.debate_check))
        }
        if (state.confirmedMissing && receipt == null) Button(onClick = onRetry,
          enabled = online && !state.busy, modifier = Modifier.testTag("debate-retry-same-id")) {
          Text(stringResource(R.string.debate_retry))
        }
        if (receipt?.terminal == true) TextButton(onClick = onNew, enabled = !state.busy) {
          Text(stringResource(R.string.debate_new))
        }
      }
    }
  }
}

internal fun debateStatusLabel(status: String): Int = when (status) {
  "accepted" -> R.string.debate_accepted
  "queued" -> R.string.debate_queued
  "starting" -> R.string.debate_starting
  "running" -> R.string.debate_running
  "publishing" -> R.string.debate_publishing
  "published" -> R.string.debate_published
  "cancelled" -> R.string.debate_cancelled
  else -> R.string.debate_failed
}
