package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

@Composable
internal fun RecordVotingPanel(voting: RecordVoting) {
  Text(stringResource(R.string.record_votes), style = MaterialTheme.typography.titleMediumEmphasized,
    modifier = Modifier.semantics { heading() })
  voting.counts.forEach { count ->
    Text(stringResource(R.string.record_vote_count, count.participantName, count.count),
      style = MaterialTheme.typography.bodyMedium)
  }
  voting.votes.forEach { vote ->
    OutlinedCard(Modifier.fillMaxWidth()) {
      Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text(stringResource(R.string.record_vote_route, vote.voterName, vote.candidateName),
          style = MaterialTheme.typography.titleSmallEmphasized,
          modifier = Modifier.semantics { heading() })
        Text(vote.reason, style = MaterialTheme.typography.bodyMedium)
        if (vote.assessments != null) {
          var expanded by remember(vote) { mutableStateOf(false) }
          TextButton(onClick = { expanded = !expanded }) {
            Text(stringResource(if (expanded) R.string.record_assessments_hide
              else R.string.record_assessments_show))
          }
          if (expanded) vote.assessments.forEach { assessment ->
            Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
              Text(stringResource(R.string.record_assessment_total,
                assessment.candidateName, assessment.total),
                style = MaterialTheme.typography.titleSmall)
              Text(stringResource(R.string.record_assessment_entertainment, assessment.entertainment))
              Text(stringResource(R.string.record_assessment_character, assessment.character))
              Text(stringResource(R.string.record_assessment_originality, assessment.originality))
              Text(stringResource(R.string.record_assessment_responsiveness, assessment.responsiveness))
              Text(stringResource(R.string.record_assessment_interaction, assessment.interaction))
              Text(assessment.reason, style = MaterialTheme.typography.bodyMedium)
            }
          }
        }
      }
    }
  }
  val explanation = when (voting.decidedBy) {
    VoteDecisionMethod.MAJORITY -> R.string.record_decided_majority
    VoteDecisionMethod.COMPOSITE_SCORE -> R.string.record_decided_composite
    VoteDecisionMethod.TIE_LOTTERY -> R.string.record_decided_lottery
    null -> if (voting.legacyTieBreakApplied) R.string.record_decided_legacy_tie else null
  }
  if (explanation != null) Text(stringResource(explanation), style = MaterialTheme.typography.bodyMedium)
}
