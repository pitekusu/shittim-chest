package dev.pitekusu.shittim.records

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.LayoutCoordinates
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import kotlin.math.abs

/** A long record is one LazyColumn item, so each animated part checks its clipped bounds. */
internal fun Modifier.onRecordSectionVisibilityChanged(onVisibilityChanged: (Boolean) -> Unit): Modifier =
  onGloballyPositioned { onVisibilityChanged(it.isFullyVisibleInWindow()) }

/** A section larger than any containing viewport can never satisfy full visibility. */
internal fun Modifier.onRecordSectionCannotFit(onCannotFit: () -> Unit): Modifier =
  onGloballyPositioned { coordinates ->
    var parent = coordinates.parentLayoutCoordinates
    while (parent != null) {
      if (coordinates.size.width > parent.size.width + 1 ||
        coordinates.size.height > parent.size.height + 1) {
        onCannotFit()
        break
      }
      parent = parent.parentLayoutCoordinates
    }
  }

private fun LayoutCoordinates.isFullyVisibleInWindow(): Boolean {
  if (!isAttached) return false
  val bounds = boundsInWindow(clipBounds = false)
  val visibleBounds = boundsInWindow(clipBounds = true)
  val tolerance = 1f // Fractional pixels must not prevent a fully visible section from playing.
  return bounds.width > 0f && bounds.height > 0f &&
    abs(bounds.left - visibleBounds.left) <= tolerance &&
    abs(bounds.top - visibleBounds.top) <= tolerance &&
    abs(bounds.right - visibleBounds.right) <= tolerance &&
    abs(bounds.bottom - visibleBounds.bottom) <= tolerance
}

@Composable
internal fun Modifier.markRecordSectionSeen(key: String?, alreadySeen: Boolean,
  onSeen: (String) -> Unit): Modifier {
  if (key == null || alreadySeen) return this
  var visible by remember(key) { mutableStateOf(false) }
  LaunchedEffect(key, visible) { if (visible) onSeen(key) }
  return onRecordSectionVisibilityChanged { visible = it }
}
