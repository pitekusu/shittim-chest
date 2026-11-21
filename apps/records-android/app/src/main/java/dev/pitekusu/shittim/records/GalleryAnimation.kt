package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.drawable.Animatable
import android.graphics.drawable.Drawable
import android.os.Build
import android.view.View
import android.widget.ImageView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.MotionDurationScale
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalWindowInfo
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import coil3.SingletonImageLoader
import coil3.asDrawable
import coil3.decode.BitmapFactoryDecoder
import coil3.decode.DecodeUtils
import coil3.decode.Decoder
import coil3.gif.AnimatedImageDecoder
import coil3.gif.GifDecoder
import coil3.gif.isGif
import coil3.request.CachePolicy
import coil3.request.ImageRequest
import coil3.request.SuccessResult
import com.github.penfeizhou.animation.apng.APNGDrawable
import com.github.penfeizhou.animation.loader.ByteBufferLoader
import dev.pitekusu.shittim.records.ui.ShittimProgress
import java.nio.ByteBuffer
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import me.saket.telephoto.zoomable.ZoomableContentLocation
import me.saket.telephoto.zoomable.rememberZoomableState
import me.saket.telephoto.zoomable.zoomable
import okio.Buffer

private enum class GalleryAnimationFormat { Apng, Gif }
private data class GalleryPoster(val drawable: Drawable, val animationFormat: GalleryAnimationFormat?)

@Composable
internal fun GalleryOriginalImage(bytes: ByteArray, onError: () -> Unit,
  animationsEnabled: Boolean = ValueAnimator.areAnimatorsEnabled()) {
  val context = LocalContext.current
  val reportError by rememberUpdatedState(onError)
  val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
  val active = lifecycle.isAtLeast(Lifecycle.State.RESUMED) && LocalWindowInfo.current.isWindowFocused
  val motionScale = rememberCoroutineScope().coroutineContext[MotionDurationScale]?.scaleFactor ?: 1f
  var autoplayOverride by remember(bytes) { mutableStateOf<Boolean?>(null) }
  val wantsAnimation = autoplayOverride ?: (animationsEnabled && motionScale > 0f)

  var poster by remember(bytes) { mutableStateOf<GalleryPoster?>(null) }
  var animation by remember(bytes) { mutableStateOf<Drawable?>(null) }
  var animationFailed by remember(bytes) { mutableStateOf(false) }
  LaunchedEffect(context, bytes) {
    val image = decodeGalleryDrawable(context, bytes, BitmapFactoryDecoder.Factory())
    if (image == null) {
      reportError()
    } else {
      poster = GalleryPoster(image, withContext(Dispatchers.Default) { galleryAnimationFormat(bytes) })
    }
  }

  val still = poster
  val format = still?.animationFormat
  LaunchedEffect(context, bytes, format, active, wantsAnimation) {
    if (format != null && active && wantsAnimation && animation == null && !animationFailed) {
      animation = decodeGalleryAnimation(context, bytes, format)
      animationFailed = animation == null
    }
  }
  if (still == null) {
    ShittimProgress(stringResource(R.string.gallery_original_loading), Modifier.widthIn(max = 360.dp))
    return
  }

  val drawable = animation.takeIf { active && wantsAnimation } ?: still.drawable
  val zoom = rememberZoomableState().apply {
    setContentLocation(ZoomableContentLocation.scaledInsideAndCenterAligned(
      Size(still.drawable.intrinsicWidth.toFloat(), still.drawable.intrinsicHeight.toFloat())))
  }
  Column(Modifier.fillMaxSize(), verticalArrangement = Arrangement.spacedBy(4.dp),
    horizontalAlignment = Alignment.CenterHorizontally) {
    Box(Modifier.weight(1f).fillMaxWidth(), contentAlignment = Alignment.Center) {
      val imageDescription = stringResource(R.string.gallery_card_image)
      // ImageView owns the drawable callback used to schedule and invalidate animation frames.
      AndroidView(factory = { viewContext ->
        ImageView(viewContext).apply {
          scaleType = ImageView.ScaleType.CENTER_INSIDE
          importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
      }, modifier = Modifier.fillMaxSize().zoomable(zoom).testTag("gallery-original-image")
        .semantics { contentDescription = imageDescription },
        update = { view ->
          if (view.drawable !== drawable) {
            (view.drawable as? Animatable)?.stop()
            view.setImageDrawable(drawable)
            (drawable as? Animatable)?.start()
          }
        },
        onRelease = { view ->
          (view.drawable as? Animatable)?.stop()
          view.setImageDrawable(null)
        })
    }
    if (animationFailed) Text(stringResource(R.string.gallery_animation_failed),
      color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
    else if (format != null) TextButton(onClick = { autoplayOverride = !wantsAnimation },
      modifier = Modifier.testTag("gallery-animation-toggle")) {
      Text(stringResource(if (wantsAnimation) R.string.gallery_animation_pause else R.string.gallery_animation_play))
    }
  }
}

private suspend fun decodeGalleryAnimation(context: Context, bytes: ByteArray,
  format: GalleryAnimationFormat): Drawable? {
  return when (format) {
    GalleryAnimationFormat.Gif -> {
      val factory = if (Build.VERSION.SDK_INT >= 28) AnimatedImageDecoder.Factory() else GifDecoder.Factory()
      decodeGalleryDrawable(context, bytes, factory)?.takeIf { it is Animatable }
    }
    GalleryAnimationFormat.Apng -> withContext(Dispatchers.Default) {
      try {
        APNGDrawable(object : ByteBufferLoader() {
          override fun getByteBuffer(): ByteBuffer = ByteBuffer.wrap(bytes)
        }).apply {
          setAutoPlay(false)
          val decoder = frameSeqDecoder
          check(!decoder.bounds.isEmpty && decoder.frameCount > 1)
        }
      } catch (error: CancellationException) {
        throw error
      } catch (_: Exception) {
        null
      }
    }
  }
}

private suspend fun decodeGalleryDrawable(context: Context, bytes: ByteArray, factory: Decoder.Factory): Drawable? {
  val request = ImageRequest.Builder(context).data(bytes).size(coil3.size.Size.ORIGINAL).decoderFactory(factory)
    .memoryCachePolicy(CachePolicy.DISABLED).diskCachePolicy(CachePolicy.DISABLED).build()
  return (SingletonImageLoader.get(context).execute(request) as? SuccessResult)?.image?.asDrawable(context.resources)
}

private fun galleryAnimationFormat(bytes: ByteArray): GalleryAnimationFormat? {
  if (bytes.size >= 6 && DecodeUtils.isGif(Buffer().write(bytes, 0, 6))) {
    return GalleryAnimationFormat.Gif
  }
  val chunks = ByteBuffer.wrap(bytes)
  if (chunks.remaining() < 8 || chunks.int != 0x89504E47.toInt() || chunks.int != 0x0D0A1A0A) return null
  // acTL precedes IDAT. Stop at IDAT so checking a still PNG does not parse its image data.
  while (chunks.remaining() >= 12) {
    val length = chunks.int
    val type = chunks.int
    if (length < 0 || length > chunks.remaining() - 4) return null
    when (type) {
      0x6163544C -> return GalleryAnimationFormat.Apng // acTL
      0x49444154, 0x49454E44 -> return null // IDAT, IEND
    }
    chunks.position(chunks.position() + length + 4)
  }
  return null
}
