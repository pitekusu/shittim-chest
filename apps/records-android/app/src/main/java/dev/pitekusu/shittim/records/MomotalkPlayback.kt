package dev.pitekusu.shittim.records

import android.icu.text.BreakIterator
import android.view.accessibility.AccessibilityManager
import androidx.compose.animation.core.Animatable
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.tween
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.listSaver
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import java.util.Locale
import kotlin.math.roundToInt

internal fun momotalkGraphemeEnds(text: String): List<Int> {
  val iterator = BreakIterator.getCharacterInstance(Locale.ROOT).apply { setText(text) }
  return buildList {
    var end = iterator.next()
    while (end != BreakIterator.DONE) { add(end); end = iterator.next() }
  }
}

@Stable
internal class MomotalkPlaybackState(completed: Int = 0, skipped: Boolean = false,
  progress: Float = 0f, following: Boolean = true) {
  var completed by mutableIntStateOf(completed)
  var skipped by mutableStateOf(skipped)
  val turn = Animatable(progress)
  var following by mutableStateOf(following)

  companion object {
    // Rotation retains only numeric progress and the skip choice, never conversation content.
    val Saver = listSaver<MomotalkPlaybackState, Any>(
      save = { listOf(it.completed, it.skipped, it.turn.value, it.following) },
      restore = { MomotalkPlaybackState((it[0] as Int).coerceAtLeast(0), it[1] as Boolean,
        (it[2] as Float).coerceIn(0f, 1f), it[3] as Boolean) })
  }
}

internal data class MomotalkPlayback(
  val state: MomotalkPlaybackState,
  val shown: Int,
  val currentText: String,
  val typing: Boolean,
  val done: Boolean,
  val motionAllowed: Boolean,
) {
  fun skip() { state.skipped = true }
}

@Composable
internal fun rememberMomotalkPlayback(messages: List<MomotalkMessage>, active: Boolean,
  motionAllowed: Boolean, replay: Int): MomotalkPlayback {
  // Only this visit owns playback. Refreshed media must not restart the conversation,
  // and neither text nor image references enter rememberSaveable or persistent storage.
  val state = rememberSaveable(replay, saver = MomotalkPlaybackState.Saver) { MomotalkPlaybackState() }
  val boundaries = remember(messages) { messages.map { momotalkGraphemeEnds(it.text) } }
  LaunchedEffect(messages, active, motionAllowed, replay, state.skipped) {
    if (!motionAllowed) state.skipped = true
    else if (active && !state.skipped) {
      while (state.completed < messages.size) {
        val duration = 700 + boundaries[state.completed].size * 35 + 600
        // Animatable shares Compose's frame clock and the system duration scale.
        // Cancellation on backgrounding retains progress; resume takes only the remaining time.
        state.turn.animateTo(1f, tween(((1f - state.turn.value) * duration).roundToInt(), easing = LinearEasing))
        state.completed++
        state.turn.snapTo(0f)
      }
    }
  }
  val done = !motionAllowed || state.skipped || state.completed >= messages.size
  val ends = boundaries.getOrNull(state.completed).orEmpty()
  val elapsed = state.turn.value * (700 + ends.size * 35 + 600)
  val letters = ((elapsed - 700) / 35).toInt().coerceIn(0, ends.size)
  val current = messages.getOrNull(state.completed)?.text.orEmpty()
  return MomotalkPlayback(state, if (done) messages.size else (state.completed + 1).coerceAtMost(messages.size),
    current.substring(0, ends.getOrNull(letters - 1) ?: 0), !done && letters == 0, done, motionAllowed)
}

@Composable
internal fun rememberMomotalkTouchExploration(): Boolean {
  val manager = LocalContext.current.getSystemService(AccessibilityManager::class.java)
  var enabled by remember(manager) { mutableStateOf(manager?.isTouchExplorationEnabled == true) }
  DisposableEffect(manager) {
    val listener = AccessibilityManager.TouchExplorationStateChangeListener { enabled = it }
    manager?.addTouchExplorationStateChangeListener(listener)
    onDispose { manager?.removeTouchExplorationStateChangeListener(listener) }
  }
  return enabled
}
