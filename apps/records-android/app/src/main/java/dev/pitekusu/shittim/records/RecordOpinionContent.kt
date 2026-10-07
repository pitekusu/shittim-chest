package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.participantVisualSlot
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import com.mikepenz.markdown.model.MarkdownState

/** Floating selectors stay outside the scrolling answer, without an opaque shared panel. */
@Composable
@OptIn(ExperimentalMaterial3Api::class)
internal fun RecordOpinionControls(opinions: List<RecordOpinion>, selected: Int, final: Boolean,
  onPerson: (Int) -> Unit) {
  val finalLabel = stringResource(R.string.record_final_proposal)
  val initialLabel = stringResource(R.string.record_initial_opinion)
  FlowRow(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp).selectableGroup(),
    horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
    opinions.forEachIndexed { index, opinion ->
      val accent = shittimParticipantColor(opinion.participantName, opinion.participantSlot)
      val stageLabel = if (index == selected && final) finalLabel else initialLabel
      FilterChip(selected = index == selected, onClick = { onPerson(index) },
        label = { Column {
          Text(opinion.participantName, style = MaterialTheme.typography.labelLarge)
          Text(stageLabel, style = MaterialTheme.typography.labelSmall)
        } },
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
        modifier = Modifier.heightIn(min = 48.dp).testTag("opinion-person-$index")
          .semantics { stateDescription = stageLabel })
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
