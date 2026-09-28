package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import android.view.ViewGroup
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BrandIntroOverlayTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun loginIntroOnlyAfterSuccessCanBeSkippedAndDoesNotReplayOnRestore() {
    compose.mainClock.autoAdvance = false
    val completion = mutableIntStateOf(0)
    val signedIn = mutableStateOf(false)
    val restoration = StateRestorationTester(compose)
    compose.activityRule.scenario.onActivity { activity ->
      activity.findViewById<ViewGroup>(android.R.id.content).removeAllViews()
    }
    restoration.setContent {
      ShittimTheme {
        BrandIntroOverlay(completion.intValue, signedIn.value, initialLaunch = false,
          animationsEnabled = true)
      }
    }
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()
    compose.runOnIdle { signedIn.value = true }
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()

    compose.runOnIdle { completion.intValue = 1 }
    compose.mainClock.advanceTimeBy(100)
    compose.onNodeWithTag("brand-intro").assertIsDisplayed().performClick()
    compose.mainClock.advanceTimeByFrame()
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()

    compose.mainClock.autoAdvance = true
    restoration.emulateSavedInstanceStateRestore()
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()
  }

  @Test fun launchIntroEndsWithoutWaitingForNetwork() {
    compose.mainClock.autoAdvance = false
    compose.activityRule.scenario.onActivity { activity ->
      activity.findViewById<ViewGroup>(android.R.id.content).removeAllViews()
    }
    compose.setContent { ShittimTheme {
      BrandIntroOverlay(0, false, initialLaunch = true, animationsEnabled = true)
    } }
    compose.mainClock.advanceTimeBy(100)
    compose.onNodeWithTag("brand-intro").assertIsDisplayed()
    compose.mainClock.advanceTimeBy(1_100)
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()
  }

  @Test fun disabledAnimationsDoNotBlockTheScreen() {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme {
        BrandIntroOverlay(1, true, initialLaunch = true, animationsEnabled = false)
      } }
    }
    compose.onNodeWithTag("brand-intro").assertDoesNotExist()
  }
}
