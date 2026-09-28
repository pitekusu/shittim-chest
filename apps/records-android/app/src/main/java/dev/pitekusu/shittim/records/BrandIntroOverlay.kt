package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.compositionLocalOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.scale
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import dev.pitekusu.shittim.records.ui.ShittimEmblem
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.delay

internal val LocalStartupIntro = compositionLocalOf { false }

private enum class IntroStage(val durationMillis: Int) {
  Launch(1_000), Login(1_500)
}

@Composable
internal fun BrandIntroOverlay(completion: Int, signedIn: Boolean, initialLaunch: Boolean,
  animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled()) {
  var launchConsumed by rememberSaveable { mutableStateOf(false) }
  var loginConsumed by rememberSaveable { mutableIntStateOf(0) }
  var stage by remember { mutableStateOf<IntroStage?>(null) }

  LaunchedEffect(initialLaunch) {
    if (initialLaunch && !launchConsumed) {
      launchConsumed = true
      if (animationsEnabled) stage = IntroStage.Launch
    }
  }
  LaunchedEffect(completion, signedIn) {
    if (completion == 0) {
      loginConsumed = 0
    } else if (signedIn && completion != loginConsumed) {
      loginConsumed = completion
      if (animationsEnabled) stage = IntroStage.Login
    }
    if (!signedIn && stage == IntroStage.Login) stage = null
  }
  var targetProgress by remember(stage) { mutableFloatStateOf(0f) }
  LaunchedEffect(stage) {
    val current = stage ?: return@LaunchedEffect
    targetProgress = 1f
    delay(current.durationMillis.toLong())
    stage = null
  }

  val current = stage ?: return
  val progress by animateFloatAsState(targetProgress,
    animationSpec = tween(current.durationMillis), label = "brand intro progress")
  BackHandler { stage = null }
  Box(Modifier.fillMaxSize().background(
    Brush.linearGradient(listOf(MaterialTheme.colorScheme.background,
      MaterialTheme.colorScheme.primaryContainer, MaterialTheme.colorScheme.background)))
    .clickable(onClickLabel = stringResource(R.string.intro_skip)) { stage = null }
    .testTag("brand-intro"), contentAlignment = Alignment.Center) {
    Column(Modifier.padding(ShittimSpacing.Large).scale(0.78f + 0.22f * progress)
      .alpha(0.15f + 0.85f * progress),
      horizontalAlignment = Alignment.CenterHorizontally,
      verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Large)) {
      ShittimEmblem(Modifier.size(132.dp), ringRotation = progress * 360f)
      Text(stringResource(R.string.brand_title), fontFamily = ShittimDisplayFont,
        style = MaterialTheme.typography.headlineMedium,
        color = MaterialTheme.colorScheme.primary)
      if (current == IntroStage.Login) {
        Text(stringResource(R.string.session_signed_in),
          style = MaterialTheme.typography.titleLargeEmphasized)
      }
    }
  }
}
