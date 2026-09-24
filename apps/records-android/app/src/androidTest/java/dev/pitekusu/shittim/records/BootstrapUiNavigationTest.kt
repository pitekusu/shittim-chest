package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.auth.MobileAvatar
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import java.time.Instant
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class BootstrapUiNavigationTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun systemBackClosesSelectedRecordInsteadOfFinishingActivity() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val user = MobileSessionUser("架空の依頼者", MobileAvatar("placeholder", "依頼者", "cyan"))
    val state = BootstrapScreen.State(
      ThemeChoice.System,
      SessionState.SignedIn(user, Instant.parse("2027-01-01T00:00:00Z"), "/records/${"a".repeat(43)}"),
      record = RecordPreviewState.Ready(RecordPreview("架空の議題", "架空の結論", "アロナ")),
      selectedRecordId = "a".repeat(43),
      eventSink = events::add,
    )
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state) } }
    compose.waitForIdle()
    compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
    compose.runOnIdle { assertEquals(listOf(BootstrapScreen.Event.CloseRecord), events) }
  }
}
