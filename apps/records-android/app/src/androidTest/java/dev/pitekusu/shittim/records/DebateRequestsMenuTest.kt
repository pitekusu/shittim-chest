package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertHasClickAction
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
/** Dialog previews use the device's real width/font scale, not content-only overrides. */
class DebateRequestsMenuTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun darkMenuOpensHistoryRatherThanStartingANewDebate() = checkEntry(true, "debate-history-menu-dark.png")
  @Test fun lightMenuOpensHistoryRatherThanStartingANewDebate() = checkEntry(false, "debate-history-menu-light.png")

  private fun checkEntry(dark: Boolean, screenshot: String) {
    val events = mutableListOf<BootstrapScreen.Event>()
    var dismissed = 0
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(darkTheme = dark) {
        RecordsNavigationMenu(BootstrapScreen.State(ThemeChoice.System, eventSink = events::add),
          onDismiss = { dismissed++ })
      }
    } }
    val entry = compose.onNodeWithTag("debate-requests-open").assertIsDisplayed().assertHasClickAction()
    compose.waitForIdle()
    val target = File(requireNotNull(compose.activity.getExternalFilesDir(null)), screenshot)
    target.outputStream().use {
      entry.captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
    entry.performClick()
    compose.runOnIdle {
      assertEquals(1, dismissed)
      assertEquals(listOf(BootstrapScreen.Event.ShowDebateRequests), events)
    }
  }
}
