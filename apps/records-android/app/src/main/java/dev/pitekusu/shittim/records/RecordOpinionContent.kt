package dev.pitekusu.shittim.records

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.ToggleButton
import androidx.compose.material3.ToggleButtonDefaults
import androidx.compose.material3.ToggleButtonShapes
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.participantVisualSlot
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import com.mikepenz.markdown.model.MarkdownState

/** Floating selectors stay outside the scrolling answer, without an opaque shared panel. */
@Composable
@OptIn(ExperimentalMaterial3Api::class)
internal fun RecordOpinionControls(opinions: List<RecordOpinion>, selected: Int, final: Boolean,
  onPerson: (Int) -> Unit, onStage: (Boolean) -> Unit) {
  val finalLabel = stringResource(R.string.record_final_proposal)
  val initialLabel = stringResource(R.string.record_initial_opinion)
  val selectedOpinion = opinions.getOrNull(selected)
  val stageAccent = selectedOpinion?.let {
    shittimParticipantColor(it.participantName, it.participantSlot)
  } ?: MaterialTheme.colorScheme.primary
  Column(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
    verticalArrangement = Arrangement.spacedBy(4.dp)) {
    FlowRow(Modifier.selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp),
      verticalArrangement = Arrangement.spacedBy(4.dp)) {
      opinions.forEachIndexed { index, opinion ->
        val accent = shittimParticipantColor(opinion.participantName, opinion.participantSlot)
        FilterChip(selected = index == selected, onClick = { onPerson(index) },
          label = { Text(opinion.participantName) },
          shapes = FilterChipDefaults.shapes(shape = CircleShape,
            selectedShape = RoundedCornerShape(percent = 40), pressedShape = RoundedCornerShape(20.dp)),
          leadingIcon = { ShittimParticipantAvatar(opinion.participantName,
            opinion.participantSlot, size = 28.dp) },
          colors = FilterChipDefaults.filterChipColors(
            containerColor = MaterialTheme.colorScheme.surfaceContainerHigh.copy(alpha = .65f),
            labelColor = MaterialTheme.colorScheme.onSurface,
            selectedContainerColor = accent.copy(alpha = .22f),
            selectedLabelColor = accent),
          border = FilterChipDefaults.filterChipBorder(enabled = true, selected = index == selected,
            borderColor = accent.copy(alpha = .45f), selectedBorderColor = accent.copy(alpha = .9f),
            selectedBorderWidth = 1.5.dp),
          modifier = Modifier.heightIn(min = 48.dp).testTag("opinion-person-$index"))
      }
    }
    ButtonGroup(overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
      horizontalArrangement = Arrangement.spacedBy(8.dp),
      modifier = Modifier.fillMaxWidth().selectableGroup()) {
      listOf(false to initialLabel, true to finalLabel).forEach { (isFinal, label) ->
        customItem(buttonGroupContent = {
          val interactions = remember { MutableInteractionSource() }
          ToggleButton(checked = final == isFinal,
            onCheckedChange = { if (it) onStage(isFinal) },
            modifier = Modifier.weight(1f).animateWidth(interactions).heightIn(min = 48.dp),
            interactionSource = interactions,
            shapes = ToggleButtonShapes(CircleShape, RoundedCornerShape(20.dp),
              RoundedCornerShape(percent = 40)),
            colors = ToggleButtonDefaults.colors(
              containerColor = MaterialTheme.colorScheme.surfaceContainerHigh.copy(alpha = .65f),
              contentColor = MaterialTheme.colorScheme.onSurface,
              checkedContainerColor = stageAccent.copy(alpha = .22f), checkedContentColor = stageAccent),
            border = BorderStroke(if (final == isFinal) 1.5.dp else 1.dp,
              stageAccent.copy(alpha = if (final == isFinal) .9f else .45f)),
            contentPadding = PaddingValues(horizontal = 12.dp, vertical = 12.dp)) {
            Text(label)
          }
        }, menuContent = { menu ->
          DropdownMenuItem(text = { Text(label) },
            onClick = { onStage(isFinal); menu.dismiss() })
        })
      }
    }
  }
}

@Composable
internal fun RecordOpinionContent(opinion: RecordOpinion, final: Boolean, markdownState: MarkdownState) {
  Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
    ShittimParticipantAvatar(opinion.participantName, opinion.participantSlot, size = 96.dp,
      modifier = Modifier.testTag("opinion-avatar-${participantVisualSlot(opinion.participantName,
        opinion.participantSlot) ?: "unknown"}"))
  }
  Text(if (final) opinion.finalTitle else opinion.summary,
    style = MaterialTheme.typography.titleLargeEmphasized,
    color = shittimParticipantColor(opinion.participantName, opinion.participantSlot))
  RecordMarkdown(if (final) opinion.finalProposal else opinion.initialProposal, markdownState = markdownState)
}
