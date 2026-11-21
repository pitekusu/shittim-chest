package dev.pitekusu.shittim.records

import android.graphics.drawable.Drawable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalWindowInfo
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import kotlinx.coroutines.CancellationException
import me.saket.telephoto.zoomable.ZoomableContentLocation
import me.saket.telephoto.zoomable.rememberZoomableState
import me.saket.telephoto.zoomable.zoomable

@Composable
internal fun MomotalkAnimatedSelfie(imageKey: String, names: String, online: Boolean, autoPlay: Boolean,
  loadAnimation: suspend (String) -> ByteArray?, poster: @Composable () -> Unit) {
  val context = LocalContext.current
  val load by rememberUpdatedState(loadAnimation)
  val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
  val active = lifecycle.isAtLeast(Lifecycle.State.RESUMED) && LocalWindowInfo.current.isWindowFocused
  var playingOverride by remember(imageKey) { mutableStateOf<Boolean?>(null) }
  var drawable by remember(imageKey) { mutableStateOf<Drawable?>(null) }
  var failed by remember(imageKey) { mutableStateOf(false) }
  var attempt by remember(imageKey) { mutableIntStateOf(0) }
  val wantsPlayback = playingOverride ?: autoPlay
  val canPlay = online && active && wantsPlayback

  LaunchedEffect(imageKey, canPlay, attempt) {
    if (!canPlay || drawable != null || failed) return@LaunchedEffect
    drawable = try { load(imageKey)?.let { decodeRecordGif(context, it) } }
    catch (error: CancellationException) { throw error }
    catch (_: Exception) { null }
    failed = drawable == null
  }

  Column(Modifier.fillMaxSize(), horizontalAlignment = Alignment.CenterHorizontally) {
    Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
      val animation = drawable.takeIf { canPlay }
      if (animation == null) {
        poster()
      } else {
        val zoom = rememberZoomableState().apply {
          setContentLocation(ZoomableContentLocation.scaledInsideAndCenterAligned(
            Size(animation.intrinsicWidth.toFloat(), animation.intrinsicHeight.toFloat())))
        }
        val description = stringResource(R.string.momotalk_selfie_description, names)
        RecordMediaDrawable(animation, Modifier.fillMaxSize().zoomable(zoom)
          .testTag("momotalk-selfie-animation").semantics { contentDescription = description })
      }
    }
    if (online) {
      if (failed) Text(stringResource(R.string.momotalk_animation_failed),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      else if (canPlay && drawable == null) Text(stringResource(R.string.momotalk_animation_loading),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      TextButton(onClick = {
        if (failed) { failed = false; attempt++ }
        else playingOverride = !wantsPlayback
      }, modifier = Modifier.testTag("momotalk-animation-toggle")) {
        Text(stringResource(when {
          failed -> R.string.momotalk_selfie_retry
          wantsPlayback -> R.string.momotalk_animation_pause
          else -> R.string.momotalk_animation_play
        }))
      }
    }
  }
}
