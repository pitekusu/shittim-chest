package dev.pitekusu.shittim.records

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import dev.pitekusu.shittim.records.ui.ShittimInset
import dev.pitekusu.shittim.records.ui.ShittimParticipantLabel
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading

@Composable
internal fun RecordAffectionPanel(affection: RecordAffection?) {
  ShittimSectionHeading(stringResource(R.string.record_affection))
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
    ShittimInset {
      ShittimParticipantLabel(change.participantName)
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

private fun signedScore(value: Int): String = if (value > 0) "+$value" else value.toString()
