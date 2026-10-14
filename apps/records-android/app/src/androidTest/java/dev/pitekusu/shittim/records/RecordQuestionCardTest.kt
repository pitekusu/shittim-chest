package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.isDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeLeft
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Rule
import org.junit.Test
import org.junit.Assert.assertTrue
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordQuestionCardTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()
  private val ending = "架空の議題の最後の段落です。"
  private val question = "架空の工作会について、準備と楽しみ方を相談します。\n\n".repeat(35) + ending

  private fun show(dark: Boolean = false, images: Boolean = true) {
    val attachments = if (images) DemoFixtures.sampleImages else emptyList()
    val state = RecordPreviewState.Ready(RecordPreview(question, "架空の結論", "アロナ",
      attachments = attachments), images = attachments.map {
      RecordImagePresentation(it, DemoFixtures.sampleImageBytes(it))
    })
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(dark) { RecordDetailScreen(state, "question-card", {}) }
    } }
  }

  @Test fun wholeCardShowsTextAndThumbnailsAndSelectedImageReturnsToSheet() {
    show()
    compose.onNodeWithTag("detail-question-open").performClick()
    for (index in 0..2) compose.onNodeWithTag("attachment-$index").assertIsDisplayed()
    capture("question-with-thumbnails-light")
    compose.onNodeWithTag("attachment-1").performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_position, 2, 3)).assertIsDisplayed()
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeLeft() }
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_position, 3, 3)).assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("detail-question-sheet").assertIsDisplayed()
    compose.waitUntil(10_000) { compose.onAllNodesWithText(ending).fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithText(ending).performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("detail-question-close").assertIsDisplayed().performClick()
    compose.waitUntil(5_000) { !compose.onNodeWithTag("detail-question-sheet").isDisplayed() }
    compose.onNodeWithTag("detail-section-Opinions").assertIsSelected()
  }

  @Test fun darkSheetKeepsAllThreeThumbnailsAndCloseReachable() {
    show(dark = true)
    compose.onNodeWithTag("detail-question-open").performClick()
    for (index in 0..2) compose.onNodeWithTag("attachment-$index").assertIsDisplayed()
    compose.onNodeWithTag("detail-question-close").assertIsDisplayed()
    capture("question-with-thumbnails-dark")
  }

  @Test fun compactCardThumbnailsOpenSelectedImageWithoutOpeningQuestionSheet() {
    show()
    for (index in 0..2) compose.onNodeWithTag("detail-attachment-$index", useUnmergedTree = true).assertIsDisplayed()
    compose.onNodeWithTag("detail-attachment-2", useUnmergedTree = true).performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_position, 3, 3)).assertIsDisplayed()
    compose.onNodeWithTag("detail-question-sheet").assertDoesNotExist()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("detail-attachment-2", useUnmergedTree = true).assertIsDisplayed()
    compose.onNodeWithTag("detail-question-full", useUnmergedTree = true).assertIsDisplayed()
    capture("question-compact-thumbnails-light")
  }

  @Test fun shortReaderWithLargeTextKeepsImageAndFullTextActionsReachable() {
    val attachments = DemoFixtures.sampleImages
    val state = RecordPreviewState.Ready(RecordPreview(question, "架空の結論", "アロナ",
      attachments = attachments), images = attachments.map { RecordImagePresentation(it, loading = true) })
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      val density = LocalDensity.current
      CompositionLocalProvider(LocalDensity provides Density(density.density, 2f)) {
        ShittimTheme(true) { Box(Modifier.height(320.dp)) { RecordDetailScreen(state, "short-reader", {}) } }
      }
    } }
    val thumbnail = compose.onNodeWithTag("detail-attachment-0", useUnmergedTree = true)
    thumbnail.assertIsDisplayed()
    assertTrue(thumbnail.fetchSemanticsNode().boundsInRoot.height >= 48 * compose.activity.resources.displayMetrics.density - 1)
    compose.onNodeWithTag("detail-question-full", useUnmergedTree = true).assertIsDisplayed()
    capture("question-compact-short-large-dark")
    thumbnail.performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_position, 1, 3)).assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("detail-question-full", useUnmergedTree = true).performClick()
    for (index in 0..2) compose.onNodeWithTag("attachment-$index").assertIsDisplayed()
  }

  @Test fun cardWithoutImagesStillOpensFullText() {
    show(images = false)
    compose.onNodeWithTag("detail-images-open", useUnmergedTree = true).assertDoesNotExist()
    compose.onNodeWithTag("detail-question-open").performClick()
    compose.onNodeWithTag("attachment-0").assertDoesNotExist()
    compose.waitUntil(10_000) { compose.onAllNodesWithText(ending).fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithText(ending).performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("detail-question-close").assertIsDisplayed()
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureTopic") != "true") return
    compose.waitForIdle()
    val folder = File(compose.activity.getExternalFilesDir(null), "topic-ui").apply { mkdirs() }
    File(folder, "$name.png").outputStream().use {
      compose.onRoot().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
