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
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.result.PickVisualMediaRequest
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.setValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimSpacing

@Composable
internal fun DebateComposeScreen(state: DebateSubmissionState, online: Boolean,
  onEdit: (String) -> Unit, onSubmit: () -> Unit, onCheck: () -> Unit,
  onRetry: () -> Unit, onReauth: () -> Unit, onNew: () -> Unit,
  modifier: Modifier = Modifier, images: List<RecordImagePresentation> = emptyList(),
  onImagesSelected: (List<Uri>) -> Unit = {}, onRemoveImage: (String) -> Unit = {},
  onDiscardMissing: () -> Unit = {}) {
  val singlePicker = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
    if (uri != null) onImagesSelected(listOf(uri))
  }
  val multiplePicker = rememberLauncherForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(MAX_ATTACHMENT_COUNT)) {
    if (it.isNotEmpty()) onImagesSelected(it)
  }
  var openedImage by remember { mutableStateOf<Int?>(null) }
  // Previews are separate transient state; only the current durable draft decides attachment membership.
  val selectedIds = state.workspace?.attachments.orEmpty().map { it.attachmentId }.toSet()
  val selectedImages = images.filter { it.image.attachmentId in selectedIds }
  if (openedImage != null && selectedImages.isNotEmpty()) AttachmentViewer(selectedImages, requireNotNull(openedImage), { openedImage = null })
  Column(modifier.fillMaxSize().verticalScroll(rememberScrollState()).padding(ShittimSpacing.Medium)
    .testTag("debate-compose"), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    DebateVersusHeader()
    Text(stringResource(R.string.debate_start), style = MaterialTheme.typography.headlineMediumEmphasized)
    val workspace = state.workspace
    if (workspace == null) {
      if (state.failure == null) CircularProgressIndicator()
      else {
        Text(stringResource(R.string.debate_storage_error), color = MaterialTheme.colorScheme.error)
        TextButton(onClick = onRetry) { Text(stringResource(R.string.session_retry)) }
      }
      return@Column
    }
    val frozen = workspace.requestId != null
    OutlinedTextField(value = workspace.frozenQuestion ?: workspace.draft, onValueChange = onEdit,
      readOnly = frozen, enabled = !state.busy, label = { Text(stringResource(R.string.debate_question)) },
      supportingText = { Text(stringResource(R.string.debate_characters,
        (workspace.frozenQuestion ?: workspace.draft).codePointCount(0, (workspace.frozenQuestion ?: workspace.draft).length))) },
      minLines = 6, modifier = Modifier.fillMaxWidth().heightIn(min = 180.dp).testTag("debate-question"))
    if (selectedImages.isNotEmpty()) AttachmentThumbnails(selectedImages, { openedImage = it },
      onRemove = if (!frozen && !state.busy) onRemoveImage else null)
    if (!frozen) TextButton(onClick = {
      val request = PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
      if (workspace.attachments.size == MAX_ATTACHMENT_COUNT - 1) singlePicker.launch(request)
      else multiplePicker.launch(request)
    }, enabled = !state.busy && workspace.attachments.size < MAX_ATTACHMENT_COUNT,
      modifier = Modifier.testTag("debate-add-images")) { Text(stringResource(R.string.attachment_add)) }
    Text(stringResource(R.string.attachment_limits), style = MaterialTheme.typography.labelSmall)
    if (state.saving) Text(stringResource(R.string.debate_saving), style = MaterialTheme.typography.labelSmall)
    if (!online) Text(stringResource(R.string.debate_offline))
    if (state.failure != null) {
      Text(stringResource(when (state.failure) {
        DebateFailure.STORAGE -> R.string.debate_storage_error
        DebateFailure.REAUTH_REQUIRED -> R.string.debate_reauth
        DebateFailure.FORBIDDEN -> R.string.debate_forbidden
        DebateFailure.QUEUE_FULL -> R.string.debate_queue_full
        DebateFailure.ATTACHMENT_INVALID -> R.string.attachment_invalid
        DebateFailure.ATTACHMENT_EXPIRED -> R.string.attachment_expired
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
        if (state.confirmedMissing && receipt == null) TextButton(onClick = onDiscardMissing, enabled = !state.busy && online) {
          Text(stringResource(R.string.attachment_discard_unsent))
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
