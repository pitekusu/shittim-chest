package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordAttachmentUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val image = DraftAttachment("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "image/png", 1, "a".repeat(64), 1, 1)

  @Test fun displayedDraftImagesAlwaysMatchCurrentWorkspaceAfterSaveFailure() {
    val state = mutableStateOf(DebateSubmissionState(DebateWorkspace(draft = "架空の議題", attachments = listOf(image))))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme { DebateComposeScreen(state.value, false, {}, {}, {}, {}, {}, {},
        images = listOf(RecordImagePresentation(image.descriptor(), failure = RecordImageFailure.STORAGE))) }
    } }
    compose.onNodeWithTag("attachment-0").assertIsDisplayed()
    compose.runOnIdle { state.value = DebateSubmissionState(DebateWorkspace(draft = "架空の議題"), failure = DebateFailure.STORAGE) }
    compose.onNodeWithTag("attachment-0").assertDoesNotExist()
  }

  @Test fun uncachedImageIsExplicitlyUnavailableOfflineAndViewerCanClose() {
    val descriptor = image.descriptor()
    val preview = RecordPreview("架空の画像付き議題", "架空の結論", "架空の参加者", attachments = listOf(descriptor))
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme { RecordDetailScreen(RecordPreviewState.Ready(preview, saved = true,
        images = listOf(RecordImagePresentation(descriptor, failure = RecordImageFailure.OFFLINE))), "r".repeat(43), {}) }
    } }
    compose.onNodeWithTag("detail-images-open").performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_offline)).assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("detail-pager").assertIsDisplayed()
  }
}
