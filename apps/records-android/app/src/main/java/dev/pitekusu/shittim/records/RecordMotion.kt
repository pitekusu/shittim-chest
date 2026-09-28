package dev.pitekusu.shittim.records

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.layout.positionInWindow
import androidx.compose.ui.platform.LocalView

/** A long record is one LazyColumn item, so a section must check its own window position. */
@Composable
internal fun Modifier.markRecordSectionSeen(key: String?, alreadySeen: Boolean,
  onSeen: (String) -> Unit): Modifier {
  if (key == null || alreadySeen) return this
  val window = LocalView.current
  var visible by remember(key) { mutableStateOf(false) }
  LaunchedEffect(key, visible) { if (visible) onSeen(key) }
  return onGloballyPositioned { coordinates ->
    val top = coordinates.positionInWindow().y
    if (top < window.height && top + coordinates.size.height > 0f) visible = true
  }
}
