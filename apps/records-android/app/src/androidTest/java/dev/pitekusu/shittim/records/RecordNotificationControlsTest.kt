package dev.pitekusu.shittim.records

import android.content.Context
import android.app.NotificationChannel
import android.app.NotificationManager
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsOff
import androidx.compose.ui.test.performClick
import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkManager
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordNotificationControlsTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun legacyVibrationActionSelectsNewChannelWithoutEnablingAppNotifications() {
    val context = compose.activity.applicationContext
    val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
    val system = context.getSystemService(NotificationManager::class.java)
    val previous = system.getNotificationChannel(RECORD_NOTIFICATION_CHANNEL)
    val newer = system.getNotificationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL)
    check(preferences.edit().clear().putBoolean("enabled", false).commit())
    system.createNotificationChannel(NotificationChannel(RECORD_NOTIFICATION_CHANNEL,
      "架空の議論結果", NotificationManager.IMPORTANCE_DEFAULT).apply { enableVibration(false) })
    RecordNotifications.changed()
    try {
      compose.activityRule.scenario.onActivity { activity -> activity.setContent {
        ShittimTheme(false) { RecordNotificationControls() }
      } }
      compose.onNodeWithTag("record-notification-vibration").assertIsDisplayed().performClick()
      compose.onNodeWithTag("record-notification-vibration").assertDoesNotExist()
      assertTrue(RecordNotificationSettings(context).vibrationChannelSelected)
      assertFalse(RecordNotificationSettings(context).optedIn)
      assertEquals(RECORD_NOTIFICATION_VIBRATION_CHANNEL, activeRecordNotificationChannelId(context))
      assertEquals(newer?.shouldVibrate() ?: true,
        checkNotNull(system.getNotificationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL)).shouldVibrate())
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {} }
      if (previous == null) system.deleteNotificationChannel(RECORD_NOTIFICATION_CHANNEL)
      if (newer == null) system.deleteNotificationChannel(RECORD_NOTIFICATION_VIBRATION_CHANNEL)
      check(preferences.edit().clear().commit())
      RecordNotifications.changed()
    }
  }

  @Test fun absentFirebaseConfigurationDoesNotOfferAnUnusablePermissionRequest() {
    val context = compose.activity.applicationContext
    val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
    check(preferences.edit().clear().commit())
    try {
      compose.activityRule.scenario.onActivity { activity ->
        activity.setContent { ShittimTheme(darkTheme = false) {
          RecordNotificationPermissionEffect(authorized = true)
          RecordNotificationControls()
        } }
      }
      compose.onNodeWithTag("record-notification-settings").assertIsDisplayed().assertIsNotEnabled()
      compose.onNodeWithTag("record-notification-switch", useUnmergedTree = true).assertIsOn().assertIsNotEnabled()
      compose.onNodeWithText("通知設定の準備中").assertIsDisplayed()
      compose.waitForIdle()
      assertFalse(RecordNotificationSettings(context).permissionRequested)
    } finally { check(preferences.edit().clear().commit()) }
  }

  @Test fun osPermissionDenialStillAllowsSwitchingOffAndPersistsTheChoice() {
    val context = compose.activity.applicationContext
    val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
    val firebase = FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
      .setApplicationId("1:000000000000:android:0123456789abcdef012345")
      .setProjectId("synthetic-record-menu").setApiKey("synthetic-not-a-real-key").build(),
      "record-notification-menu-test")
    check(preferences.edit().clear().commit())
    RecordNotificationSettings(context).permissionResult(false)
    RecordNotifications.changed()
    try {
      assertFalse(RecordNotifications.permitted(context))
      compose.activityRule.scenario.onActivity { activity ->
        activity.setContent { ShittimTheme(darkTheme = false) { RecordNotificationControls() } }
      }
      compose.onNodeWithTag("record-notification-os-settings").assertIsDisplayed()
      compose.onNodeWithTag("record-notification-switch", useUnmergedTree = true).assertIsOn().performClick()
      compose.onNodeWithTag("record-notification-switch", useUnmergedTree = true).assertIsOff()
      compose.onNodeWithText("オフ").assertIsDisplayed()
      assertFalse(RecordNotificationSettings(context).optedIn)
      RecordNotifications.revoke(context)
      assertFalse(RecordNotificationSettings(context).optedIn)
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {} }
      WorkManager.getInstance(context).cancelUniqueWork("records-notification-registration-v1")
        .result.get(10, TimeUnit.SECONDS)
      firebase.delete()
      check(preferences.edit().clear().commit())
    }
  }
}
