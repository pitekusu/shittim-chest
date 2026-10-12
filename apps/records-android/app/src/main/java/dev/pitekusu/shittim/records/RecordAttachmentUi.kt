package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import coil3.compose.AsyncImage
import coil3.request.ImageRequest
import coil3.request.CachePolicy
import kotlinx.coroutines.launch
import me.saket.telephoto.zoomable.ZoomableContentLocation
import me.saket.telephoto.zoomable.rememberZoomableState
import me.saket.telephoto.zoomable.zoomable

@Composable
internal fun AttachmentThumbnails(images: List<RecordImagePresentation>,
  onOpen: (Int) -> Unit, modifier: Modifier = Modifier, onRemove: ((String) -> Unit)? = null) {
  Row(modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
    images.forEachIndexed { index, image ->
      Column(Modifier.weight(1f), horizontalAlignment = Alignment.CenterHorizontally) {
        Surface(onClick = { onOpen(index) }, shape = MaterialTheme.shapes.medium,
          modifier = Modifier.fillMaxWidth().height(80.dp).testTag("attachment-$index")) {
          if (image.bytes != null) AttachmentImage(image, Modifier.fillMaxSize(), maximum = 256)
          else Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            if (image.loading) CircularProgressIndicator()
            else Text(stringResource(R.string.attachment_number, index + 1))
          }
        }
        if (onRemove != null) TextButton(onClick = { onRemove(image.image.attachmentId) }) {
          Text(stringResource(R.string.attachment_remove))
        }
      }
    }
  }
}

@Composable
internal fun AttachmentViewer(images: List<RecordImagePresentation>, initial: Int,
  onClose: () -> Unit, onRetry: () -> Unit = {}) {
  if (images.isEmpty()) return
  val pager = rememberPagerState(initialPage = initial.coerceIn(images.indices), pageCount = { images.size })
  val selected = pager.currentPage.coerceIn(images.indices)
  val scope = rememberCoroutineScope()
  Dialog(onDismissRequest = onClose, properties = DialogProperties(usePlatformDefaultWidth = false,
    decorFitsSystemWindows = false)) {
    Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surface) {
      Column(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp)) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
          Text(stringResource(R.string.attachment_position, selected + 1, images.size), Modifier.weight(1f))
          TextButton(onClick = onClose, modifier = Modifier.testTag("attachment-close")) {
            Text(stringResource(R.string.detail_close))
          }
        }
        HorizontalPager(pager, Modifier.weight(1f).fillMaxWidth().testTag("attachment-viewer"),
          key = { images[it].image.attachmentId }) { page ->
          val image = images[page]
          Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            if (image.bytes != null) {
              var contentSize by remember { mutableStateOf(Size(image.image.width.toFloat(), image.image.height.toFloat())) }
              val zoom = rememberZoomableState().apply {
                setContentLocation(ZoomableContentLocation.scaledInsideAndCenterAligned(contentSize))
              }
              // Match the drawn bounds to Telephoto's content location. Its default Fit
              // performs the initial upscaling; a second Fit here would crop small images.
              AttachmentImage(image, Modifier.fillMaxSize().zoomable(zoom), maximum = 4096,
                contentScale = ContentScale.Inside, onSize = { contentSize = it })
            } else if (image.loading) CircularProgressIndicator()
            else Column(horizontalAlignment = Alignment.CenterHorizontally,
              verticalArrangement = Arrangement.spacedBy(12.dp)) {
              Text(stringResource(when (image.failure) {
                RecordImageFailure.OFFLINE -> R.string.attachment_offline
                RecordImageFailure.STORAGE -> R.string.attachment_storage
                else -> R.string.attachment_unavailable
              }))
              if (image.failure != RecordImageFailure.OFFLINE) Button(onClick = onRetry) {
                Text(stringResource(R.string.session_retry))
              }
            }
          }
        }
        if (images.size > 1) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
          TextButton(onClick = { scope.launch { pager.animateScrollToPage(selected - 1) } },
            enabled = selected > 0, modifier = Modifier.testTag("attachment-previous")) {
            Text(stringResource(R.string.attachment_previous))
          }
          TextButton(onClick = { scope.launch { pager.animateScrollToPage(selected + 1) } },
            enabled = selected < images.lastIndex, modifier = Modifier.testTag("attachment-next")) {
            Text(stringResource(R.string.attachment_next))
          }
        }
      }
    }
  }
}

@Composable
private fun AttachmentImage(image: RecordImagePresentation, modifier: Modifier, maximum: Int,
  contentScale: ContentScale = ContentScale.Fit, onSize: (Size) -> Unit = {}) {
  val context = LocalContext.current
  var failed by remember(image.bytes, maximum) { mutableStateOf(false) }
  var attempt by remember(image.bytes, maximum) { mutableIntStateOf(0) }
  val request = remember(image.bytes, maximum, attempt) { ImageRequest.Builder(context)
    .data(image.bytes).size(maximum, maximum).diskCachePolicy(CachePolicy.DISABLED).build() }
  Box(modifier, contentAlignment = Alignment.Center) {
    if (failed) Column(horizontalAlignment = Alignment.CenterHorizontally) {
      Text(stringResource(R.string.attachment_unavailable))
      TextButton(onClick = { attempt++; failed = false }) { Text(stringResource(R.string.session_retry)) }
    } else AsyncImage(request, contentDescription = stringResource(R.string.attachment_image),
      modifier = Modifier.fillMaxSize(), contentScale = contentScale,
      onSuccess = { success -> onSize(Size(success.result.image.width.toFloat(), success.result.image.height.toFloat())) },
      onError = { failed = true })
  }
}
