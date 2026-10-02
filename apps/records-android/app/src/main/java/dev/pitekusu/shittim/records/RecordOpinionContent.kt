package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.ButtonGroup
import androidx.compose.material3.ButtonGroupDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.participantVisualSlot
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import com.mikepenz.markdown.model.MarkdownState

/** Selectors stay outside the scrolling answer; no competing horizontal person swipe. */
@Composable
@OptIn(ExperimentalMaterial3Api::class)
internal fun RecordOpinionControls(opinions: List<RecordOpinion>, selected: Int, final: Boolean,
  onPerson: (Int) -> Unit, onStage: (Boolean) -> Unit) {
  val finalLabel = stringResource(R.string.record_final_proposal)
  val initialLabel = stringResource(R.string.record_initial_opinion)
  Surface(color = MaterialTheme.colorScheme.surfaceContainer, modifier = Modifier.fillMaxWidth()) {
    Column(Modifier.padding(horizontal = 16.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
      FlowRow(Modifier.selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        opinions.forEachIndexed { index, opinion ->
          val accent = shittimParticipantColor(opinion.participantName, opinion.participantSlot)
          FilterChip(selected = index == selected, onClick = { onPerson(index) },
            label = { Text(opinion.participantName) }, shapes = FilterChipDefaults.shapes(),
            leadingIcon = { ShittimParticipantAvatar(opinion.participantName,
              opinion.participantSlot, size = 24.dp) },
            colors = FilterChipDefaults.filterChipColors(selectedContainerColor = accent.copy(alpha = .15f),
              selectedLabelColor = accent),
            modifier = Modifier.heightIn(min = 48.dp).testTag("opinion-person-$index"))
        }
      }
      ButtonGroup(overflowIndicator = { ButtonGroupDefaults.OverflowIndicator(it) },
        modifier = Modifier.selectableGroup().padding(bottom = 8.dp)) {
        toggleableItem(checked = !final, label = initialLabel,
          onCheckedChange = { if (it) onStage(false) })
        toggleableItem(checked = final, label = finalLabel,
          onCheckedChange = { if (it) onStage(true) })
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
