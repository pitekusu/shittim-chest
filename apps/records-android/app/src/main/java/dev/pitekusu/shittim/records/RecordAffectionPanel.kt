package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.FilterChipDefaults
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.scale
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import kotlinx.coroutines.delay
import kotlin.math.roundToInt

/** Compact totals stay reachable while the selected person's card scrolls independently. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun RecordAffectionControls(changes: List<RecordAffectionChange>, selected: Int,
  modifier: Modifier = Modifier, onPerson: (Int) -> Unit) {
  FlowRow(modifier.fillMaxWidth().padding(horizontal = ShittimSpacing.Medium, vertical = 8.dp)
    .selectableGroup(), horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small),
    verticalArrangement = Arrangement.spacedBy(4.dp)) {
    changes.forEachIndexed { index, change ->
      val accent = shittimParticipantColor(change.participantName, change.participantSlot)
      FilterChip(selected = index == selected, onClick = { onPerson(index) },
        label = { Column {
          Text(change.participantName)
          Text(signedScore(change.appliedDelta), style = MaterialTheme.typography.labelSmall,
            color = if (change.appliedDelta < 0) MaterialTheme.colorScheme.error else accent)
        } },
        leadingIcon = { ShittimParticipantAvatar(change.participantName,
          change.participantSlot, size = 28.dp) },
        shapes = FilterChipDefaults.shapes(shape = CircleShape,
          selectedShape = RoundedCornerShape(percent = 40), pressedShape = RoundedCornerShape(20.dp)),
        colors = FilterChipDefaults.filterChipColors(
          containerColor = MaterialTheme.colorScheme.surfaceContainerHigh.copy(alpha = .65f),
          labelColor = MaterialTheme.colorScheme.onSurface,
          selectedContainerColor = accent.copy(alpha = .22f), selectedLabelColor = accent),
        border = FilterChipDefaults.filterChipBorder(enabled = true, selected = index == selected,
          borderColor = accent.copy(alpha = .45f), selectedBorderColor = accent.copy(alpha = .9f),
          selectedBorderWidth = 1.5.dp),
        modifier = Modifier.heightIn(min = 56.dp).testTag("affection-person-$index"))
    }
  }
}

@Composable
internal fun RecordAffectionPanel(affection: RecordAffection?, motionKey: String? = null,
  playedSections: Set<String> = emptySet(),
  animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled(),
  motionActive: Boolean = true, selectedIndex: Int = 0,
  minimumVisibleTop: () -> Float = { Float.NEGATIVE_INFINITY }, onSectionSeen: (String) -> Unit = {}) {
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
  val change = affection.changes.getOrNull(selectedIndex) ?: return
  val cardKey = motionKey?.let { "$it:$selectedIndex" }
  key(cardKey, selectedIndex) {
    AffectionCard(change, cardKey, playedSections, animationsEnabled, motionActive, minimumVisibleTop, onSectionSeen,
      Modifier.testTag("affection-card-$selectedIndex"))
  }
}

@Composable
private fun AffectionCard(change: RecordAffectionChange, cardKey: String?,
  playedSections: Set<String>, animationsEnabled: Boolean, motionActive: Boolean,
  minimumVisibleTop: () -> Float, onSectionSeen: (String) -> Unit, modifier: Modifier = Modifier) {
  var cannotFit by remember(cardKey) { mutableStateOf(false) }
  var fullyVisible by remember(cardKey) { mutableStateOf(false) }
  val progress = remember(cardKey) { Animatable(1f) }
  val played = cardKey == null || cardKey in playedSections
  val accent = shittimParticipantColor(change.participantName, change.participantSlot)
  var pulse by remember(cardKey) { mutableStateOf(false) }
  // Capture eligibility in composition. Reading mutable geometry inside the effect can start
  // before its visibility key updates; the next composition then cancels the just-started tween.
  val canPlay = motionActive && animationsEnabled && fullyVisible && !cannotFit
  val skipMotion = motionActive && !animationsEnabled
  // Do not key this effect on playedSections: saving "seen" must not cancel the animation.
  // Mark before playback so scroll/refresh/rotation cannot replay this selected visit.
  LaunchedEffect(cardKey, canPlay, skipMotion) {
    pulse = false
    if (skipMotion && !played) {
      onSectionSeen(cardKey)
    }
    if (canPlay && !played) {
      onSectionSeen(cardKey)
      progress.snapTo(0f)
      progress.animateTo(1f, tween(1_100))
      if (change.appliedDelta > 0) {
        pulse = true
        delay(200)
        pulse = false
      }
    } else {
      progress.snapTo(1f)
    }
  }
  val current = (change.before + (change.after - change.before) * progress.value).roundToInt()
  val scale by animateFloatAsState(if (pulse && motionActive && fullyVisible) 1.04f else 1f,
    animationSpec = if (motionActive && animationsEnabled) tween(160) else snap(),
    label = "affection increase")
  Surface(modifier.fillMaxWidth().heightIn(min = 240.dp)
    .onRecordSectionCannotFit { cannotFit = it }
    .onRecordSectionVisibilityChanged(minimumVisibleTop) { fullyVisible = it },
    shape = MaterialTheme.shapes.medium,
    color = MaterialTheme.colorScheme.surfaceContainerHigh) {
    Column(Modifier.padding(ShittimSpacing.Medium),
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        ShittimParticipantAvatar(change.participantName, change.participantSlot, size = 64.dp)
        Text(change.participantName, style = MaterialTheme.typography.titleSmallEmphasized,
          color = accent)
      }
      val filled = (current.coerceIn(0, 1_000) / 100).coerceIn(0, 10)
      FlowRow(maxItemsInEachRow = 5, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        repeat(10) { index ->
          Text(if (index < filled) "♥" else "♡", color = if (index < filled) accent
            else MaterialTheme.colorScheme.onSurfaceVariant,
            style = MaterialTheme.typography.titleLarge)
        }
      }
      Box(Modifier.fillMaxWidth()) {
        // Reserve the maximum score's text height; moving digits never shift the reading position.
        Text(stringResource(R.string.record_affection_transition, change.before, 1_000),
          color = Color.Transparent, style = MaterialTheme.typography.headlineSmallEmphasized,
          modifier = Modifier.clearAndSetSemantics {})
        Text(stringResource(R.string.record_affection_transition, change.before, current),
          modifier = Modifier.scale(scale).testTag("affection-score"),
          style = MaterialTheme.typography.headlineSmallEmphasized)
      }
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
