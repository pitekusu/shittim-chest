package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordPreviewPanelTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test
  fun oneRecordAndRetryAreVisibleWithoutShowingOldBodyInErrorState() {
    val state = mutableStateOf<RecordPreviewState>(RecordPreviewState.Ready(
      RecordPreview("架空の議題", "架空の結論", "アロナ")))
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) { RecordPreviewPanel(state.value, events::add) } }
    }
    compose.onNodeWithText("架空の議題").assertIsDisplayed()
    compose.onNodeWithText("アロナ").assertIsDisplayed()
    compose.onNodeWithText("架空の結論").assertIsDisplayed()
    compose.runOnIdle { state.value = RecordPreviewState.Error(RecordReadFailure.UNAVAILABLE) }
    compose.onNodeWithText("架空の議題").assertDoesNotExist()
    compose.onNodeWithText(label(R.string.record_retry)).performClick()
    assertEquals(BootstrapScreen.Event.RetryRecord, events.single())
  }

  private fun label(id: Int): String = compose.activity.getString(id)
}
