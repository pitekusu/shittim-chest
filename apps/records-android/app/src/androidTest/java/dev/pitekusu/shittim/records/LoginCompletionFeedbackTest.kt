package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.material3.Button
import androidx.compose.material3.Text
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class LoginCompletionFeedbackTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun successIsNonBlockingDoesNotReplayOnRestoreAndClearsOnSignOut() {
    val completion = mutableIntStateOf(0)
    val signedIn = mutableStateOf(true)
    var opened = 0
    val restoration = StateRestorationTester(compose)
    // Replace the Activity's initial ComposeView with tester-owned, restorable content.
    compose.activityRule.scenario.onActivity { activity ->
      activity.findViewById<ViewGroup>(android.R.id.content).removeAllViews()
    }
    restoration.setContent { ShittimTheme { ShittimBackdrop {
      Button(onClick = { opened++ }, modifier = Modifier.align(Alignment.TopCenter)) { Text("記録を開く") }
      LoginCompletionFeedback(completion.intValue, signedIn.value, Modifier.align(Alignment.BottomCenter))
    } } }
    compose.onNodeWithTag("login-complete").assertDoesNotExist() // Already-restored login is silent.
    compose.mainClock.autoAdvance = false
    compose.runOnIdle { completion.intValue = 1 }
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertIsDisplayed()
    compose.onNodeWithText("記録を開く").performClick()
    compose.runOnIdle { assertEquals(1, opened) }
    // The restoration tester must render both the disposal and restoration frames.
    compose.mainClock.autoAdvance = true
    restoration.emulateSavedInstanceStateRestore()
    compose.mainClock.autoAdvance = false
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertDoesNotExist()

    // Process restart resets the event count, but can restore the UI's consumed value.
    compose.runOnIdle { completion.intValue = 0 }
    compose.mainClock.advanceTimeByFrame()
    compose.runOnIdle { completion.intValue = 1 }
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertIsDisplayed()
    compose.runOnIdle { signedIn.value = false }
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertDoesNotExist()
    compose.runOnIdle { signedIn.value = true }
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertDoesNotExist()
  }

  @Test fun feedbackEndsWithoutDelayingSavedRecords() {
    val completion = mutableIntStateOf(0)
    val events = mutableListOf<BootstrapScreen.Event>()
    val user = MobileSessionUser("動作確認用", MobileAvatar("placeholder", "確認用", "cyan"))
    val session = SessionState.SignedIn(user, "u".repeat(43), Instant.parse("2027-01-01T00:00:00Z"), "/")
    val records = RecordListState.Ready.fromSaved(listOf(RecordListEntry("a".repeat(43),
      "架空の相談：休日の楽しみは？", "動作確認用", RecordAvatar(null, "cyan"),
      Instant.parse("2026-09-27T00:00:00Z"), "アロナ")))
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { BootstrapUi(BootstrapScreen.State(ThemeChoice.Dark, session,
        records = records, loginCompletion = completion.intValue, eventSink = events::add)) }
    }
    compose.onNodeWithTag("login-complete").assertDoesNotExist()
    compose.mainClock.autoAdvance = false
    compose.runOnIdle { completion.intValue = 1 }
    compose.mainClock.advanceTimeBy(500)
    compose.onNodeWithTag("login-complete").assertIsDisplayed()
    compose.onNodeWithText("ログアウト").performClick()
    compose.runOnIdle { assertEquals(BootstrapScreen.Event.Logout, events.last()) }
    capture()
    compose.mainClock.advanceTimeBy(2_100)
    compose.onNodeWithTag("login-complete").assertDoesNotExist()
  }

  private fun capture() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureMotion") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(compose.activity.cacheDir, "motion-login-complete.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
