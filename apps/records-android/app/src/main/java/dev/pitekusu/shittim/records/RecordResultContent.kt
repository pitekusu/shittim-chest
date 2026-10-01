package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.expandVertically
import androidx.compose.animation.shrinkVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.heightIn
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
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
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import com.mikepenz.markdown.model.MarkdownState
import com.mikepenz.markdown.model.rememberMarkdownState

@Composable
internal fun RecordResultContent(preview: RecordPreview, recordId: String?,
  motionActive: Boolean, playedSections: Set<String>, onSectionSeen: (String) -> Unit,
  decisionMarkdown: MarkdownState = rememberMarkdownState(preview.decision)) {
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
  RecordMarkdown(preview.decision, markdownState = decisionMarkdown)
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
  ResultDisclosure(stringResource(R.string.record_actions), preview.actions, "actions", animated)
  ResultDisclosure(stringResource(R.string.record_caveats), preview.caveats, "caveats", animated)
}

@Composable
private fun ResultDisclosure(title: String, items: List<String>, tag: String, animated: Boolean) {
  if (items.isEmpty()) return
  var expanded by rememberSaveable { mutableStateOf(false) }
  val accent = if (tag == "actions") MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.tertiary
  val rotation by animateFloatAsState(if (expanded) 180f else 0f,
    animationSpec = if (animated) MaterialTheme.motionScheme.defaultSpatialSpec() else snap(),
    label = "disclosure arrow")
  val background by animateColorAsState(accent.copy(alpha = if (expanded) .12f else .05f),
    animationSpec = if (animated) MaterialTheme.motionScheme.defaultEffectsSpec() else snap(),
    label = "disclosure surface")
  val action = stringResource(if (expanded) R.string.detail_collapse else R.string.detail_expand)
  Surface(Modifier.fillMaxWidth(), shape = MaterialTheme.shapes.large, color = background,
    border = BorderStroke(1.dp, accent.copy(alpha = .3f))) {
    Column {
      Surface(onClick = { expanded = !expanded }, color = androidx.compose.ui.graphics.Color.Transparent,
        modifier = Modifier.fillMaxWidth().heightIn(min = 64.dp).testTag("detail-$tag-expand")
          .semantics { stateDescription = action }) {
        Row(Modifier.padding(ShittimSpacing.Medium), verticalAlignment = Alignment.CenterVertically,
          horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
          Icon(painterResource(if (tag == "actions") R.drawable.ic_check else R.drawable.ic_warning),
            null, Modifier.size(24.dp), tint = accent)
          Text(title, modifier = Modifier.weight(1f), color = accent,
            style = MaterialTheme.typography.titleSmallEmphasized)
          Surface(color = accent.copy(alpha = .15f), shape = MaterialTheme.shapes.extraLarge) {
            Text(stringResource(R.string.detail_item_count, items.size),
              Modifier.padding(horizontal = 10.dp, vertical = 4.dp), color = accent,
              style = MaterialTheme.typography.labelMedium)
          }
          Icon(painterResource(R.drawable.ic_expand_more), null,
            Modifier.size(24.dp).graphicsLayer { rotationZ = rotation }, tint = accent)
        }
      }
      AnimatedVisibility(expanded,
        enter = expandVertically(animationSpec = if (animated) MaterialTheme.motionScheme.defaultSpatialSpec() else snap()) +
          fadeIn(animationSpec = if (animated) MaterialTheme.motionScheme.defaultEffectsSpec() else snap()),
        exit = shrinkVertically(animationSpec = if (animated) MaterialTheme.motionScheme.defaultSpatialSpec() else snap()) +
          fadeOut(animationSpec = if (animated) MaterialTheme.motionScheme.defaultEffectsSpec() else snap())) {
        Column(Modifier.padding(start = ShittimSpacing.Medium, end = ShittimSpacing.Medium,
          bottom = ShittimSpacing.Medium), verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
          items.forEach { Text(stringResource(R.string.record_list_item, it),
            color = MaterialTheme.colorScheme.onSurface) }
        }
      }
    }
  }
}
