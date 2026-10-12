package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.doubleClick
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeRight
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.ByteArrayOutputStream
import kotlin.math.min
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
    compose.onNodeWithTag("detail-question-open").performClick()
    compose.onNodeWithTag("attachment-0").performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_offline)).assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("detail-question-sheet").assertIsDisplayed()
  }

  @Test fun smallAndLargePortraitImagesInitiallyShowEveryEdge() {
    val images = listOf(edgeImage(80, 40, 1), edgeImage(1600, 3200, 2))
    showViewer(images)
    waitForAllEdges(80, 40)
    compose.onNodeWithTag("attachment-next").performClick()
    assertPosition(2, 2)
    waitForAllEdges(1600, 3200)
  }

  @Test fun tappedImageOpensAndSwipesAndButtonsStopAtBothEnds() {
    val images = (1..3).map { edgeImage(80, 40, it) }
    showViewer(images, initial = 1)
    assertPosition(2, 3)
    waitForAllEdges(80, 40)
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeLeft() }
    assertPosition(3, 3)
    compose.onNodeWithTag("attachment-next").assertIsNotEnabled()
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeLeft() }
    assertPosition(3, 3)
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeRight() }
    assertPosition(2, 3)
    compose.onNodeWithTag("attachment-previous").performClick()
    assertPosition(1, 3)
    compose.onNodeWithTag("attachment-previous").assertIsNotEnabled()
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeRight() }
    assertPosition(1, 3)
    compose.onNodeWithTag("attachment-next").performClick()
    assertPosition(2, 3)
  }

  @Test fun zoomedImagePansBeforeThePagerHandlesASwipe() {
    showViewer((1..2).map { edgeImage(80, 40, it) })
    waitForAllEdges(80, 40)
    compose.onNodeWithTag("attachment-viewer").performTouchInput { doubleClick() }
    compose.waitUntil(5_000) { !allEdgesVisible(80, 40) }
    compose.onNodeWithTag("attachment-viewer").performTouchInput {
      swipe(center, center - Offset(width * .15f, 0f), durationMillis = 1_000)
    }
    assertPosition(1, 2)
    compose.onNodeWithTag("attachment-viewer").performTouchInput { doubleClick() }
    waitForAllEdges(80, 40)
    compose.onNodeWithTag("attachment-viewer").performTouchInput { swipeLeft() }
    assertPosition(2, 2)
  }

  private fun showViewer(images: List<RecordImagePresentation>, initial: Int = 0) {
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme { AttachmentViewer(images, initial, {}) }
    } }
  }

  private fun assertPosition(selected: Int, count: Int) {
    compose.onNodeWithText(compose.activity.getString(R.string.attachment_position, selected, count)).assertIsDisplayed()
  }

  private fun waitForAllEdges(width: Int, height: Int) {
    // Check actual rendered pixels: matching counters alone cannot catch double scaling
    // that hides the source image's edges inside an otherwise full-screen viewport.
    compose.waitUntil(5_000) { allEdgesVisible(width, height) }
  }

  private fun allEdgesVisible(width: Int, height: Int): Boolean {
    val viewport = compose.onNodeWithTag("attachment-viewer").captureToImage().asAndroidBitmap()
    val scale = min(viewport.width.toFloat() / width, viewport.height.toFloat() / height)
    val drawnWidth = width * scale
    val drawnHeight = height * scale
    val left = (viewport.width - drawnWidth) / 2
    val top = (viewport.height - drawnHeight) / 2
    fun pixel(x: Float, y: Float) = viewport.getPixel(x.toInt(), y.toInt()) and 0x00ffffff
    return pixel(left + drawnWidth * .025f, viewport.height / 2f) == (Color.MAGENTA and 0x00ffffff) &&
      pixel(left + drawnWidth * .975f, viewport.height / 2f) == (Color.CYAN and 0x00ffffff) &&
      pixel(viewport.width / 2f, top + drawnHeight * .025f) == (Color.RED and 0x00ffffff) &&
      pixel(viewport.width / 2f, top + drawnHeight * .975f) == (Color.BLUE and 0x00ffffff)
  }

  private fun edgeImage(width: Int, height: Int, index: Int): RecordImagePresentation {
    val bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
    val paint = Paint()
    Canvas(bitmap).apply {
      drawColor(Color.GREEN)
      paint.color = Color.RED
      drawRect(0f, 0f, width.toFloat(), height / 10f, paint)
      paint.color = Color.BLUE
      drawRect(0f, height * .9f, width.toFloat(), height.toFloat(), paint)
      paint.color = Color.MAGENTA
      drawRect(0f, 0f, width / 10f, height.toFloat(), paint)
      paint.color = Color.CYAN
      drawRect(width * .9f, 0f, width.toFloat(), height.toFloat(), paint)
    }
    val bytes = ByteArrayOutputStream().use { output ->
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)
      output.toByteArray()
    }
    bitmap.recycle()
    return RecordImagePresentation(RecordAttachment("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeee$index",
      "image/png", bytes.size, attachmentDigest(bytes), width, height), bytes)
  }
}
