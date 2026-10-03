package dev.pitekusu.shittim.records

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordNotificationControlsTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun absentFirebaseConfigurationDoesNotOfferAnUnusablePermissionRequest() {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(darkTheme = false) { RecordNotificationControls() } }
    }
    compose.onNodeWithTag("record-notification-settings").assertIsDisplayed().assertIsNotEnabled()
    compose.onNodeWithText("通知設定の準備中").assertIsDisplayed()
  }
}
