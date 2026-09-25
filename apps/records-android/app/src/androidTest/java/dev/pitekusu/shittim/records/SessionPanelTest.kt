package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionNotice
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SessionPanelTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun expiryAndLogoutRemoveProfileAndExposeOnlyRelevantActions() {
    val state = mutableStateOf<SessionState>(SessionState.SignedIn(
      MobileSessionUser("架空の利用者", MobileAvatar("placeholder", "架空", "cyan")),
      "u".repeat(43), Instant.parse("2030-01-01T00:00:00Z"), "/"))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { SessionPanel(state.value, events::add) } }
    }
    compose.onNodeWithText("架空の利用者").assertIsDisplayed()
    compose.onNodeWithText(label(R.string.session_logout)).performClick()
    assertEquals(listOf(BootstrapScreen.Event.Logout), events)
    compose.runOnIdle { state.value = SessionState.SigningOut }
    compose.onNodeWithText("架空の利用者").assertDoesNotExist()
    compose.onNodeWithText(label(R.string.session_login)).assertDoesNotExist()
    compose.runOnIdle { state.value = SessionState.SignedOut(SessionNotice.EXPIRED) }
    compose.onNodeWithText(label(R.string.session_expired)).assertIsDisplayed()
    compose.onNodeWithText(label(R.string.session_login)).performClick()
    assertEquals(BootstrapScreen.Event.Login, events.last())
    compose.runOnIdle { state.value = SessionState.Browser }
    compose.onNodeWithText(label(R.string.session_login)).assertDoesNotExist()
    compose.runOnIdle { state.value = SessionState.Unavailable }
    compose.onNodeWithText(label(R.string.session_retry)).performClick()
    assertEquals(BootstrapScreen.Event.Retry, events.last())
  }

  private fun label(id: Int): String = compose.activity.getString(id)
}
