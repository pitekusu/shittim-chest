package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.LoadingIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import dev.pitekusu.shittim.records.ui.ShittimPanel
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import kotlin.math.PI
import kotlin.math.sin

/** Stages describe the latest receipt, never elapsed time or inferred completed work. */
private fun debateProgressStage(receipt: DebateRequest?): Int? = when (receipt?.status) {
  "accepted", "queued" -> 0
  "starting" -> 1
  "publishing", "published" -> 3
  "running" -> when (receipt.phase) {
    "scoring_affection", "preparing_evidence", "forming_preferences", "selecting_candidates" -> 1
    "collecting_initial_opinions", "discussing" -> 2
    "collecting_final_proposals", "selecting_winner", "generating_decision" -> 3
    else -> null
  }
  else -> null
}

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun DebateProgressCard(receipt: DebateRequest?, motionAllowed: Boolean,
  modifier: Modifier = Modifier, animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled()) {
  val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
  val active = receipt != null && !receipt.terminal && motionAllowed && animationsEnabled &&
    lifecycle.isAtLeast(Lifecycle.State.RESUMED)
  val cycle = if (active) {
    val transition = rememberInfiniteTransition(label = "debate waiting")
    transition.animateFloat(0f, 1f,
      infiniteRepeatable(tween(2_400, easing = LinearEasing)), label = "debate waiting cycle")
  } else rememberUpdatedState(0f)
  val stage = debateProgressStage(receipt)
  val failed = receipt?.status in setOf("failed", "cancelled")
  val accent = if (failed) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary
  ShittimPanel(modifier.testTag("debate-progress-card")) {
    BoxWithConstraints(Modifier.fillMaxWidth()) {
      val compact = maxWidth < 300.dp || LocalDensity.current.fontScale > 1.5f
      Row(Modifier.fillMaxWidth().padding(top = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small),
        verticalAlignment = Alignment.Top) {
        for ((index, name) in listOf("アロナ", "プラナ", "安倍晋三AI").withIndex()) {
          val slot = "participant-${'a' + index}"
          val color = shittimParticipantColor(name, slot)
          Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
            // Decorative breathing and dots do not claim a speaker, a turn, or model output.
            ShittimParticipantAvatar(name, slot, size = if (compact) 48.dp else 64.dp,
              modifier = Modifier.testTag("debate-progress-avatar-$index").graphicsLayer {
                val wave = if (active) sin((cycle.value + index / 3f) * 2f * PI).toFloat() else 0f
                translationY = -3.dp.toPx() * wave
                scaleX = 1f + .025f * wave
                scaleY = scaleX
              })
            Surface(shape = MaterialTheme.shapes.medium, color = color.copy(alpha = .12f),
              modifier = Modifier.clearAndSetSemantics {}) {
              Row(Modifier.padding(horizontal = 10.dp, vertical = 7.dp),
                horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                repeat(3) { dot ->
                  Surface(Modifier.size(4.dp).graphicsLayer {
                    alpha = if (active) .55f + .45f * sin((cycle.value + (index + dot) / 3f) * PI).toFloat() else .65f
                  }, shape = CircleShape, color = color) {}
                }
              }
            }
            Text(name, style = MaterialTheme.typography.labelMedium, color = color,
              textAlign = TextAlign.Center)
          }
        }
      }
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
      if (active) LoadingIndicator(Modifier.size(40.dp).testTag("debate-progress-motion")
        .clearAndSetSemantics {}, color = accent)
      else Icon(painterResource(when {
        receipt?.status == "published" -> R.drawable.ic_check
        failed -> R.drawable.ic_warning
        else -> R.drawable.ic_debate_history
      }), contentDescription = null, tint = accent, modifier = Modifier.size(32.dp))
      Column(Modifier.weight(1f).semantics(mergeDescendants = true) { liveRegion = LiveRegionMode.Polite },
        verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(stringResource(if (receipt == null) R.string.debate_unknown else debateStatusLabel(receipt.status)),
          style = MaterialTheme.typography.titleLargeEmphasized, color = accent)
        receipt?.takeIf { !it.terminal }?.phase?.takeIf { debatePhaseLabel(it) != R.string.debate_status_title }?.let {
          Text(stringResource(debatePhaseLabel(it)), style = MaterialTheme.typography.bodyMedium)
        }
      }
    }
    if (receipt != null && !failed) {
      Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Small)) {
        for ((index, label) in listOf(R.string.debate_stage_receipt, R.string.debate_stage_preparing,
          R.string.debate_stage_discussion, R.string.debate_stage_result).withIndex()) {
          val current = index == stage
          Column(Modifier.weight(1f).testTag("debate-progress-stage-$index")
            .semantics(mergeDescendants = true) { selected = current },
            verticalArrangement = Arrangement.spacedBy(6.dp), horizontalAlignment = Alignment.CenterHorizontally) {
            Surface(Modifier.fillMaxWidth().height(4.dp),
              shape = CircleShape,
              color = if (current) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.outlineVariant) {}
            Text(stringResource(label), textAlign = TextAlign.Center,
              style = if (current) MaterialTheme.typography.labelLarge else MaterialTheme.typography.labelMedium,
              color = if (current) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
          }
        }
      }
    }
  }
}
