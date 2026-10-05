package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleOwner
import androidx.lifecycle.LifecycleRegistry
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class DebateComposeFabTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun activationShowsFeedbackBeforeOpeningOnceWithoutShrinkingTheTouchTarget() {
    val calls = AtomicInteger()
    show { DebateComposeFab(active = true, onClick = { calls.incrementAndGet() }, animationsEnabled = true) }
    val bounds = compose.onNodeWithTag("debate-compose-open").assertIsDisplayed().fetchSemanticsNode().boundsInRoot
    compose.mainClock.autoAdvance = false
    try {
      compose.onNodeWithTag("debate-compose-open").performClick()
      compose.mainClock.advanceTimeByFrame()
      compose.onNodeWithTag("debate-compose-open").performClick()
      compose.mainClock.advanceTimeBy(64)
      assertEquals(0, calls.get())
      assertEquals(bounds, compose.onNodeWithTag("debate-compose-open").fetchSemanticsNode().boundsInRoot)
      compose.mainClock.advanceTimeBy(160)
      assertEquals(1, calls.get())
    } finally { compose.mainClock.autoAdvance = true }
  }

  @Test fun hidingTheActionOrLeavingForegroundCancelsPendingNavigation() {
    val calls = AtomicInteger()
    val active = mutableStateOf(true)
    lateinit var owner: Owner
    compose.runOnUiThread { owner = Owner().apply { lifecycle.currentState = Lifecycle.State.RESUMED } }
    show {
      CompositionLocalProvider(LocalLifecycleOwner provides owner) {
        DebateComposeFab(active.value, { calls.incrementAndGet() }, animationsEnabled = true)
      }
    }
    compose.mainClock.autoAdvance = false
    try {
      compose.onNodeWithTag("debate-compose-open").performClick()
      compose.mainClock.advanceTimeByFrame()
      compose.runOnIdle { active.value = false }
      compose.mainClock.advanceTimeBy(160)
      compose.runOnIdle { active.value = true }
      compose.mainClock.advanceTimeBy(160)
      assertEquals(0, calls.get())
      compose.onNodeWithTag("debate-compose-open").performClick()
      compose.mainClock.advanceTimeByFrame()
      compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.STARTED }
      compose.mainClock.advanceTimeBy(160)
      compose.runOnUiThread { owner.lifecycle.currentState = Lifecycle.State.RESUMED }
      compose.mainClock.advanceTimeBy(160)
      assertEquals(0, calls.get())
    } finally { compose.mainClock.autoAdvance = true }
  }

  @Test fun disabledAnimationsDoNotDelayOpening() {
    val calls = AtomicInteger()
    show { DebateComposeFab(active = true, onClick = { calls.incrementAndGet() }, animationsEnabled = false) }
    compose.mainClock.autoAdvance = false
    try {
      compose.onNodeWithTag("debate-compose-open").performClick()
      compose.mainClock.advanceTimeByFrame()
      assertEquals(1, calls.get())
    } finally { compose.mainClock.autoAdvance = true }
  }

  private fun show(content: @androidx.compose.runtime.Composable () -> Unit) {
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) { content() } } }
    compose.waitForIdle()
  }

  private class Owner : LifecycleOwner {
    override val lifecycle = LifecycleRegistry(this)
  }
}
