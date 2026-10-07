package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.assertTextEquals
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.util.concurrent.atomic.AtomicInteger
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordMotionTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun sectionIsNotMarkedSeenUntilAllOfItIsVisible() {
    val seen = AtomicInteger()
    val played = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column(Modifier.height(300.dp).verticalScroll(rememberScrollState())) {
          Spacer(Modifier.height(250.dp))
          Box(Modifier.fillMaxWidth().height(100.dp)
            .markRecordSectionSeen("section", played.value) {
              seen.incrementAndGet()
              played.value = true
            }.testTag("section"))
        }
      } }
    }
    compose.runOnIdle { assertEquals(0, seen.get()) }
    compose.onNodeWithTag("section").performScrollTo()
    compose.waitUntil(5_000) { seen.get() == 1 }
    compose.runOnIdle { assertEquals(1, seen.get()) }
  }

  @Test
  fun affectionWaitsForItsOwnCardAndThenPlaysOnlyOnce() {
    val played = mutableStateOf(emptySet<String>())
    val calls = AtomicInteger()
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 500, 10, 10, 510)))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column(Modifier.height(300.dp).verticalScroll(rememberScrollState())) {
          Spacer(Modifier.height(250.dp))
          RecordAffectionPanel(affection, "affection:sample", played.value,
            animationsEnabled = true) { key ->
            calls.incrementAndGet()
            played.value = played.value + key
          }
        }
      } }
    }
    compose.runOnIdle {
      assertTrue(played.value.isEmpty())
      assertEquals(0, calls.get())
    }
    compose.onNodeWithTag("affection-card-0").performScrollTo()
    compose.waitUntil(5_000) { "affection:sample:0" in played.value }
    compose.runOnIdle {
      assertFalse(played.value.isEmpty())
      assertEquals(1, calls.get())
    }
  }

  @Test
  fun affectionTooTallForViewportShowsSavedFinalValueWithoutAnimation() {
    val played = mutableStateOf(emptySet<String>())
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 500, 10, 10, 510)))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column(Modifier.height(120.dp).verticalScroll(rememberScrollState())) {
          RecordAffectionPanel(affection, "affection:oversize", played.value,
            animationsEnabled = true) { key ->
            played.value = played.value + key
          }
        }
      } }
    }
    compose.runOnIdle { assertTrue(played.value.isEmpty()) }
    compose.onNodeWithText("親愛度：500 → 510", useUnmergedTree = true).assertExists()
  }

  @Test
  fun disabledAnimationShowsTheLowerBoundAndDoesNotConsumeAnInactiveCard() {
    val active = mutableStateOf(false)
    val played = mutableStateOf(emptySet<String>())
    val calls = AtomicInteger()
    val affection = RecordAffection(RecordAffectionStatus.APPLIED, listOf(
      RecordAffectionChange("アロナ", 5, -10, -5, 0)))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column {
          RecordAffectionPanel(affection, "affection:disabled", played.value,
            animationsEnabled = false, motionActive = active.value) { key ->
            calls.incrementAndGet()
            played.value = played.value + key
          }
        }
      } }
    }
    compose.onNodeWithText("親愛度：5 → 0").assertExists()
    compose.onNodeWithText("実増減：-5点").assertExists()
    compose.runOnIdle {
      assertTrue(played.value.isEmpty())
      active.value = true
    }
    compose.waitUntil(5_000) { "affection:disabled:0" in played.value }
    compose.runOnIdle { assertEquals(1, calls.get()) }
    compose.onNodeWithText("親愛度：5 → 0").assertExists()
  }

  @Test fun everyPersonAnimatesItsOwnScoreAndCanPlayAfterViewportGrows() {
    val selected = mutableStateOf(0)
    val height = mutableStateOf(120.dp)
    val played = mutableStateOf(emptySet<String>())
    val affection = RecordAffection(RecordAffectionStatus.APPLIED,
      listOf("アロナ", "プラナ", "安倍晋三AI").map { RecordAffectionChange(it, 500, 100, 100, 600) })
    compose.activityRule.scenario.onActivity { it.setContent { ShittimTheme(false) {
      Column(Modifier.height(height.value).verticalScroll(rememberScrollState())) {
        RecordAffectionPanel(affection, "person", played.value,
          animationsEnabled = true, selectedIndex = selected.value) {
          played.value = played.value + it
        }
      }
    } } }
    compose.runOnIdle { assertTrue(played.value.isEmpty()) }
    compose.mainClock.autoAdvance = false
    try {
      compose.runOnIdle { height.value = 500.dp }
      for (index in 0..2) {
        compose.runOnIdle { selected.value = index }
        compose.mainClock.advanceTimeByFrame()
        // Global bounds are delivered by the Android layout pass, not the test animation clock.
        compose.waitForIdle()
        compose.mainClock.advanceTimeBy(96)
        assertTrue("Person $index should not already show the completed score; played=${played.value}",
          compose.onNodeWithTag("affection-score").fetchSemanticsNode()
            .config[androidx.compose.ui.semantics.SemanticsProperties.Text].single().text != "親愛度：500 → 600")
        compose.mainClock.advanceTimeBy(1_200)
        compose.onNodeWithTag("affection-score").assertTextEquals("親愛度：500 → 600")
        compose.runOnIdle { assertTrue("person:$index" in played.value) }
      }
    } finally {
      compose.mainClock.autoAdvance = true
    }
  }
}
