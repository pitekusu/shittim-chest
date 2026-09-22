package dev.pitekusu.shittim.records

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BootstrapCircuitTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun graphRendersScreenAndRoutesThemeEvents() {
    compose.onNodeWithText(label(R.string.bootstrap_title)).assertIsDisplayed()
    compose.onNodeWithText(label(R.string.session_login)).performScrollTo().assertIsDisplayed()
    compose.onNodeWithText(label(R.string.preview_system)).performScrollTo()
    compose.onNodeWithText(label(R.string.preview_system)).assertIsOn()
    compose.onNodeWithText(label(R.string.preview_dark)).performClick().assertIsOn()
    compose.onNodeWithText(label(R.string.preview_system)).assertIsOff()
    compose.onNodeWithText(label(R.string.preview_light)).performClick().assertIsOn()
    compose.onNodeWithText(label(R.string.preview_dark)).assertIsOff()
    compose.onNodeWithText(label(R.string.preview_system)).performClick().assertIsOn()
  }

  @Test
  fun themeChoiceSurvivesActivityRecreation() {
    val dark = label(R.string.preview_dark)
    val system = label(R.string.preview_system)
    compose.onNodeWithText(dark).performScrollTo().performClick().assertIsOn()
    compose.activityRule.scenario.recreate()
    compose.onNodeWithText(dark).assertIsOn()
    compose.onNodeWithText(system).assertIsOff()
    // Clicking the checked control cannot clear the single selection.
    compose.onNodeWithText(dark).performClick().assertIsOn()
  }

  private fun label(id: Int): String = compose.activity.getString(id)
}
