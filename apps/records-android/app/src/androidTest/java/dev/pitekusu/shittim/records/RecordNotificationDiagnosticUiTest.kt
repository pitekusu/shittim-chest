package dev.pitekusu.shittim.records

import android.content.Context
import androidx.activity.compose.setContent
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.work.WorkManager
import com.google.firebase.FirebaseApp
import com.google.firebase.FirebaseOptions
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.util.concurrent.TimeUnit
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordNotificationDiagnosticUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun menuShowsOnlyClassifiedFailureAndExplicitRetryClearsIt() {
    val context = compose.activity.applicationContext
    val preferences = context.getSharedPreferences("record-notification-v1", Context.MODE_PRIVATE)
    val firebase = FirebaseApp.initializeApp(context, FirebaseOptions.Builder()
      .setApplicationId("1:000000000000:android:0123456789abcdef012345")
      .setProjectId("synthetic-record-diagnostic").setApiKey("synthetic-not-a-real-key").build(),
      "record-notification-diagnostic-test")
    check(preferences.edit().clear().commit())
    RecordNotifications.locallyRevoked = false
    RecordNotificationSettings(context).failure(NotificationRegistrationStage.REGISTER,
      NotificationRegistrationFailure.RESPONSE, 1, terminal = true)
    try {
      compose.activityRule.scenario.onActivity { activity ->
        activity.setContent { ShittimTheme(darkTheme = false) { RecordNotificationControls() } }
      }
      compose.onNodeWithText("通知を登録できませんでした").assertIsDisplayed()
      compose.onNodeWithText("登録の診断：サーバーへの登録／応答を確認できませんでした（試行1）").assertIsDisplayed()
      // Unknown stored values must never become UI text or a log payload.
      compose.runOnIdle {
        check(preferences.edit().putString("failureCategory", "untrusted-value").commit())
        RecordNotifications.changed()
      }
      compose.onNodeWithTag("record-notification-diagnostic").assertDoesNotExist()
      compose.onNodeWithText("再試行").performClick()
      compose.onNodeWithTag("record-notification-diagnostic").assertDoesNotExist()
    } finally {
      compose.activityRule.scenario.onActivity { it.setContent {} }
      WorkManager.getInstance(context).cancelUniqueWork("records-notification-registration-v1")
        .result.get(10, TimeUnit.SECONDS)
      firebase.delete()
      check(preferences.edit().clear().commit())
    }
  }
}
