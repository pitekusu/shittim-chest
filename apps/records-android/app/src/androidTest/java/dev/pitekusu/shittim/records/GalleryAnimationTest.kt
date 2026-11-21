package dev.pitekusu.shittim.records

import android.graphics.Color
import android.os.SystemClock
import androidx.activity.compose.setContent
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.util.Base64
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class GalleryAnimationTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun apngPlaysAutomaticallyAndCanBePaused() = verifyPlayback(syntheticApng, autoplay = true)

  @Test fun gifShowsPosterAndPlaysOnDemand() = verifyPlayback(syntheticGif, autoplay = false)

  private fun verifyPlayback(encoded: String, autoplay: Boolean) {
    val bytes = Base64.getDecoder().decode(encoded)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        ShittimTheme {
          GalleryOriginalImage(bytes, onError = { error("Synthetic image failed to decode") }, animationsEnabled = autoplay)
        }
      }
    }
    val playLabel = compose.activity.getString(R.string.gallery_animation_play)
    val play = compose.onNodeWithText(playLabel)
    val pauseLabel = compose.activity.getString(R.string.gallery_animation_pause)
    val pause = compose.onNodeWithText(pauseLabel)
    val image = compose.onNodeWithTag("gallery-original-image")

    fun frameColor(): Int {
      val bitmap = image.captureToImage().asAndroidBitmap()
      return try { bitmap.getPixel(bitmap.width / 2, bitmap.height / 2) }
      finally { bitmap.recycle() }
    }

    fun waitForFrame(color: Int) = compose.waitUntil(7_000) { frameColor() == color }

    val initialLabel = if (autoplay) pauseLabel else playLabel
    compose.waitUntil(5_000) { compose.onAllNodes(hasText(initialLabel)).fetchSemanticsNodes().isNotEmpty() }
    if (!autoplay) {
      waitForFrame(Color.RED)
      play.performClick()
    }
    waitForFrame(Color.BLUE)
    pause.performClick()
    waitForFrame(Color.RED)
    val start = SystemClock.uptimeMillis()
    var stayedStill = true
    compose.waitUntil(3_000) {
      stayedStill = stayedStill && frameColor() == Color.RED
      SystemClock.uptimeMillis() - start >= 800
    }
    assertTrue("Paused animation should stay on its red poster throughout a full loop", stayedStill)
    play.performClick()
    waitForFrame(Color.BLUE)
  }
}

// Two 80 x 40 frames, solid red then solid blue, 400 ms per frame and an infinite loop.
private const val syntheticApng = "iVBORw0KGgoAAAANSUhEUgAAAFAAAAAoCAIAAADmAupWAAAACGFjVEwAAAACAAAAAPON" +
  "k3AAAAAaZmNUTAAAAAAAAABQAAAAKAAAAAAAAAAAAAIABQAAYZxztgAAAEhJREFUeJztz4ENABAAwDD8/zNfkNR6wTb3+Mt6HXBb" +
  "w7qGdQ3rGtY1rGtY17CuYV3DuoZ1Desa1jWsa1jXsK5hXcO6hnUN6w6LxgFPm+Z7SQAAABpmY1RMAAAAAQAAAFAAAAAoAAAAAA" +
  "AAAAAAAgAFAAD675liAAAAT2ZkQVQAAAACeJztzwENACEQwLDj/XsGF3wyWgXbmtnzku/vgNsM1xmuM1xnuM5wneE6w3WG6wzX" +
  "Ga4zXGe4znCd4TrDdYbrDNcZrjNcdwCJyAFPZsTgvAAAAABJRU5ErkJggg=="
internal const val syntheticGif = "R0lGODlhUAAoAIEAAP8AAAAAAAAAAAAAACH/C05FVFNDQVBFMi4wAwEAAAAh+QQAKAAAACwA" +
  "AAAAUAAoAAAIXQABCBxIsKDBgwgTKlzIsKHDhxAjSpxIsaLFixgzatzIsaPHjyBDihxJsqTJkyhTqlzJsqXLlzBjypxJs6bNmz" +
  "hz6tzJs6fPn0CDCh1KtKjRo0iTKl3KtKnTpygDAgAh+QQBKAABACwAAAAAUAAoAIEAAP8AAAAAAAAAAAAIXQABCBxIsKDBgwgTK" +
  "lzIsKHDhxAjSpxIsaLFixgzatzIsaPHjyBDihxJsqTJkyhTqlzJsqXLlzBjypxJs6bNmzhz6tzJs6fPn0CDCh1KtKjRo0iTKl3" +
  "KtKnTpygDAgA7"
