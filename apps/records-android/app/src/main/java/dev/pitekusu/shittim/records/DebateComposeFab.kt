package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.snap
import androidx.compose.animation.core.tween
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.FloatingActionButtonDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlin.math.max

@OptIn(ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun DebateComposeFab(active: Boolean, onClick: () -> Unit, modifier: Modifier = Modifier,
  animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled()) {
  val interactions = remember { MutableInteractionSource() }
  val pressed by interactions.collectIsPressedAsState()
  var activating by remember { mutableStateOf(false) }
  val activation = remember { Animatable(0f) }
  val latestActive = rememberUpdatedState(active)
  val latestClick = rememberUpdatedState(onClick)
  val lifecycle = LocalLifecycleOwner.current.lifecycle
  // A short visual confirmation is not permission to navigate after the app has left foreground.
  LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { activating = false }
  LaunchedEffect(activating, active, animationsEnabled, lifecycle) {
    if (!activating || !active) {
      activating = false
      activation.snapTo(0f)
      return@LaunchedEffect
    }
    if (animationsEnabled) activation.animateTo(1f, tween(160))
    if (latestActive.value && lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) latestClick.value()
    activating = false
    activation.snapTo(0f)
  }
  val press by animateFloatAsState(if (pressed && active) 1f else 0f,
    animationSpec = if (animationsEnabled) tween(80) else snap(), label = "debate fab press")
  val background by animateColorAsState(
    if (active && (pressed || activating)) lerp(MaterialTheme.colorScheme.primary,
      MaterialTheme.colorScheme.scrim, .22f) else MaterialTheme.colorScheme.primary,
    animationSpec = if (animationsEnabled) MaterialTheme.motionScheme.fastEffectsSpec() else snap(),
    label = "debate fab feedback")
  FloatingActionButton(onClick = {
    if (active && !activating && lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) activating = true
  }, modifier = modifier.size(64.dp).testTag("debate-compose-open"), interactionSource = interactions,
    shape = CircleShape, containerColor = background, contentColor = MaterialTheme.colorScheme.onPrimary,
    elevation = FloatingActionButtonDefaults.elevation(
      defaultElevation = if (activating) 0.dp else 6.dp, pressedElevation = 0.dp,
    )) {
    // The 64dp touch target stays fixed while the shadow drops and the symbol sinks into the face.
    Icon(painterResource(R.drawable.ic_add_debate), stringResource(R.string.debate_start),
      Modifier.size(32.dp).graphicsLayer {
        val amount = if (animationsEnabled) max(press, activation.value) else 0f
        scaleX = 1f - .28f * amount
        scaleY = scaleX
        translationY = 5.dp.toPx() * amount
      })
  }
}
