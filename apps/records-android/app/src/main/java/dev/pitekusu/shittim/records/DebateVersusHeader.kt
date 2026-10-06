package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.core.Animatable
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.CutCornerShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.shittimParticipantColor

/** One short entrance, never an input delay or a repeating animation beside the draft. */
@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun DebateVersusHeader(animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled()) {
  var entered by rememberSaveable { mutableStateOf(false) }
  val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
  val resumed = lifecycle.isAtLeast(Lifecycle.State.RESUMED)
  val entrance = remember { Animatable(if (entered || !animationsEnabled) 1f else 0f) }
  val motion = MaterialTheme.motionScheme.slowSpatialSpec<Float>()
  LaunchedEffect(resumed, animationsEnabled) {
    if (!animationsEnabled || entered) entrance.snapTo(1f)
    else if (resumed) {
      entered = true
      entrance.animateTo(1f, motion)
    }
  }
  val label = stringResource(R.string.debate_versus_label)
  BoxWithConstraints(Modifier.fillMaxWidth().padding(vertical = 12.dp)
    .testTag("debate-versus").semantics { contentDescription = label }) {
    val size = if (maxWidth < 360.dp || LocalDensity.current.fontScale > 1.5f) 56.dp else 72.dp
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween,
      verticalAlignment = Alignment.CenterVertically) {
      for ((index, name) in listOf("アロナ", "プラナ", "安倍晋三AI").withIndex()) {
        val slot = "participant-${'a' + index}"
        val accent = shittimParticipantColor(name, slot)
        val progress = (entrance.value * 1.3f - index * .13f).coerceIn(0f, 1f)
        ShittimParticipantAvatar(name, slot, size, modifier = Modifier.graphicsLayer {
          alpha = .3f + .7f * progress
          scaleX = .82f + .18f * progress
          scaleY = scaleX
          translationX = (index - 1) * 16.dp.toPx() * (1f - progress)
          rotationZ = (index - 1) * 8f * (1f - progress)
        })
        if (index < 2) VersusBadge(accent, entrance.value.coerceIn(0f, 1f))
      }
    }
  }
}

@Composable
private fun VersusBadge(accent: Color, progress: Float) {
  Surface(shape = CutCornerShape(8.dp), color = accent.copy(alpha = .13f),
    border = BorderStroke(1.dp, accent.copy(alpha = .45f)),
    modifier = Modifier.graphicsLayer {
      rotationZ = -10f
      alpha = .2f + .8f * progress
      scaleX = .65f + .35f * progress
      scaleY = scaleX
    }) {
    Text(stringResource(R.string.debate_versus), color = accent,
      fontStyle = FontStyle.Italic, fontWeight = FontWeight.Black,
      style = MaterialTheme.typography.titleMediumEmphasized,
      modifier = Modifier.padding(horizontal = 6.dp, vertical = 4.dp))
  }
}
