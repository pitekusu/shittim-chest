package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.animateIntAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import kotlinx.coroutines.delay

@Composable
internal fun RecordAffectionPanel(affection: RecordAffection?, motionKey: String? = null,
  playedSections: Set<String> = emptySet(),
  animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled(),
  onSectionSeen: (String) -> Unit = {}) {
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
  BoxWithConstraints(Modifier.fillMaxWidth()) {
    val wide = maxWidth >= 720.dp && LocalDensity.current.fontScale < 1.5f &&
      affection.changes.size == 3
    if (wide) {
      Row(horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        affection.changes.forEachIndexed { index, change ->
          val cardKey = motionKey?.let { "$it:$index" }
          AffectionCard(change, cardKey, playedSections, animationsEnabled, onSectionSeen,
            Modifier.weight(1f).testTag("affection-card-$index"))
        }
      }
    } else {
      Column(verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        affection.changes.forEachIndexed { index, change ->
          val cardKey = motionKey?.let { "$it:$index" }
          AffectionCard(change, cardKey, playedSections, animationsEnabled, onSectionSeen,
            Modifier.testTag("affection-card-$index"))
        }
      }
    }
  }
}

@Composable
private fun AffectionCard(change: RecordAffectionChange, cardKey: String?,
  playedSections: Set<String>, animationsEnabled: Boolean,
  onSectionSeen: (String) -> Unit, modifier: Modifier = Modifier) {
  var cannotFit by remember(cardKey) { mutableStateOf(false) }
  val played = cardKey == null || cardKey in playedSections || cannotFit || !animationsEnabled
  val accent = shittimParticipantColor(change.participantName, change.participantSlot)
  val current by animateIntAsState(if (played) change.after else change.before,
    animationSpec = if (cannotFit) snap() else tween(1_100), label = "affection points")
  val shouldPulse = remember(change, cardKey) { cardKey != null && !played } && !cannotFit
  var pulse by remember(change, cardKey) { mutableStateOf(false) }
  LaunchedEffect(played) {
    if (shouldPulse && played && change.appliedDelta > 0) {
      pulse = true
      delay(200)
      pulse = false
    }
  }
  val scale by animateFloatAsState(if (pulse) 1.06f else 1f,
    animationSpec = tween(160), label = "affection increase")
  Surface(modifier.fillMaxWidth().heightIn(min = 208.dp)
    .onRecordSectionCannotFit {
      if (!cannotFit) {
        cannotFit = true
        cardKey?.let(onSectionSeen)
      }
    }
    .markRecordSectionSeen(cardKey, played, onSectionSeen),
    shape = MaterialTheme.shapes.medium,
    color = MaterialTheme.colorScheme.surfaceContainerHigh) {
    Column(Modifier.padding(ShittimSpacing.Medium),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        ShittimParticipantAvatar(change.participantName, change.participantSlot)
        Text(change.participantName, style = MaterialTheme.typography.titleSmallEmphasized,
          color = accent)
      }
      val filled = (current.coerceIn(0, 1_000) / 100).coerceIn(0, 10)
      Column {
        repeat(2) { row ->
          Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            repeat(5) { column ->
              val index = row * 5 + column
              Text(if (index < filled) "♥" else "♡", color = if (index < filled) accent
                else MaterialTheme.colorScheme.onSurfaceVariant,
                style = MaterialTheme.typography.titleMedium)
            }
          }
        }
      }
      Text(stringResource(R.string.record_affection_transition, change.before, current),
        modifier = Modifier.scale(scale), style = MaterialTheme.typography.titleMediumEmphasized)
      val direction = when {
        change.appliedDelta > 0 -> "↑ "
        change.appliedDelta < 0 -> "↓ "
        else -> "→ "
      }
      val deltaColor = when {
        change.appliedDelta > 0 -> accent
        change.appliedDelta < 0 -> MaterialTheme.colorScheme.error
        else -> MaterialTheme.colorScheme.onSurfaceVariant
      }
      Surface(shape = MaterialTheme.shapes.small,
        color = deltaColor.copy(alpha = 0.15f)) {
        Row(Modifier.padding(horizontal = 8.dp, vertical = 4.dp),
          horizontalArrangement = Arrangement.spacedBy(4.dp)) {
          Text(direction, color = deltaColor, style = MaterialTheme.typography.labelMedium)
          Text(stringResource(R.string.record_affection_applied_delta,
            signedScore(change.appliedDelta)), color = deltaColor,
            style = MaterialTheme.typography.labelMedium)
        }
      }
    }
  }
}

private fun signedScore(value: Int): String = if (value > 0) "+$value" else value.toString()
