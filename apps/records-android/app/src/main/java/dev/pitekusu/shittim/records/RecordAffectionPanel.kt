package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedCard
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp

@Composable
internal fun RecordAffectionPanel(affection: RecordAffection?) {
  Text(stringResource(R.string.record_affection), style = MaterialTheme.typography.titleMediumEmphasized,
    modifier = Modifier.semantics { heading() })
  if (affection == null) {
    Text(stringResource(R.string.record_affection_missing),
      color = MaterialTheme.colorScheme.onSurfaceVariant)
    return
  }
  if (affection.status == RecordAffectionStatus.UNAVAILABLE) {
    Text(stringResource(R.string.record_affection_unavailable),
      color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
  affection.changes.forEach { change ->
    OutlinedCard(Modifier.fillMaxWidth()) {
      Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(change.participantName, style = MaterialTheme.typography.titleSmallEmphasized,
          modifier = Modifier.semantics { heading() })
        val questionScore = change.questionScore?.let {
          stringResource(R.string.record_affection_score_points, signedScore(it))
        } ?: stringResource(R.string.record_affection_unrated)
        Text(stringResource(R.string.record_affection_question_score, questionScore))
        Text(stringResource(R.string.record_affection_transition, change.before, change.after))
        Text(stringResource(R.string.record_affection_applied_delta, signedScore(change.appliedDelta)),
          color = when {
            change.appliedDelta > 0 -> MaterialTheme.colorScheme.primary
            change.appliedDelta < 0 -> MaterialTheme.colorScheme.error
            else -> MaterialTheme.colorScheme.onSurfaceVariant
          })
      }
    }
  }
}

private fun signedScore(value: Int): String = if (value > 0) "+$value" else value.toString()
