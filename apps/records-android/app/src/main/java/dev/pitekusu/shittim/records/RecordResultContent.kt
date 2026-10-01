package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor

@Composable
internal fun RecordResultContent(preview: RecordPreview, recordId: String?,
  motionActive: Boolean, playedSections: Set<String>, onSectionSeen: (String) -> Unit) {
  val motionKey = recordId?.let { "result:$it" }
  val played = motionKey == null || motionKey in playedSections
  val animated = motionActive && ValueAnimator.areAnimatorsEnabled()
  val scale by animateFloatAsState(if (played || !animated) 1f else 0.96f,
    animationSpec = if (animated) MaterialTheme.motionScheme.defaultSpatialSpec() else snap(),
    label = "winner arrival")
  val accent = shittimParticipantColor(preview.winnerName, preview.winnerSlot)
  Surface(Modifier.fillMaxWidth().graphicsLayer { scaleX = scale; scaleY = scale }
    .markRecordSectionSeen(motionKey, played || !animated, onSectionSeen).testTag("detail-winner"),
    color = accent.copy(alpha = 0.12f), border = BorderStroke(1.dp, accent.copy(alpha = 0.4f)),
    shape = MaterialTheme.shapes.extraLarge) {
    Row(Modifier.padding(ShittimSpacing.Medium), verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
      ShittimParticipantAvatar(preview.winnerName, preview.winnerSlot, size = 76.dp, crowned = true)
      Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(stringResource(R.string.record_winner), color = accent,
          style = MaterialTheme.typography.labelLarge)
        Text(preview.winnerName, color = accent, style = MaterialTheme.typography.headlineSmallEmphasized)
      }
    }
  }
  Text(stringResource(R.string.record_decision), style = MaterialTheme.typography.titleMediumEmphasized,
    color = MaterialTheme.colorScheme.onSurface)
  RecordMarkdown(preview.decision)
  preview.victoryMessage?.let { message ->
    Surface(color = accent.copy(alpha = 0.1f), shape = MaterialTheme.shapes.large) {
      Column(Modifier.padding(ShittimSpacing.Medium),
        verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        Text(stringResource(R.string.record_victory_message), color = accent,
          style = MaterialTheme.typography.labelLarge)
        Text(message, color = MaterialTheme.colorScheme.onSurface,
          style = MaterialTheme.typography.bodyLarge)
      }
    }
  }
  ResultDisclosure(stringResource(R.string.record_actions), preview.actions, "actions")
  ResultDisclosure(stringResource(R.string.record_caveats), preview.caveats, "caveats")
}

@Composable
private fun ResultDisclosure(title: String, items: List<String>, tag: String) {
  if (items.isEmpty()) return
  var expanded by rememberSaveable { mutableStateOf(false) }
  Column(Modifier.fillMaxWidth()) {
    TextButton(onClick = { expanded = !expanded },
      modifier = Modifier.fillMaxWidth().testTag("detail-$tag-expand")) {
      Text(stringResource(R.string.detail_disclosure, title, items.size,
        stringResource(if (expanded) R.string.detail_collapse else R.string.detail_expand)),
        modifier = Modifier.fillMaxWidth(), style = MaterialTheme.typography.titleSmall)
    }
    AnimatedVisibility(expanded) {
      Column(Modifier.padding(horizontal = ShittimSpacing.Small),
        verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        items.forEach { Text(stringResource(R.string.record_list_item, it),
          color = MaterialTheme.colorScheme.onSurface) }
      }
    }
  }
}
