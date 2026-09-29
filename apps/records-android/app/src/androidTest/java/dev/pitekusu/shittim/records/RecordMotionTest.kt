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
import androidx.compose.ui.test.performScrollTo
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
          RecordAffectionPanel(affection, "affection:sample", played.value) { key ->
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
}
