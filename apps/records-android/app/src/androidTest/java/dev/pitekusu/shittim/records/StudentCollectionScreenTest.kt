package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Shader
import androidx.activity.compose.setContent
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.assert
import androidx.compose.ui.test.assertHasNoClickAction
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasAnyAncestor
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextClearance
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.ByteArrayOutputStream
import java.io.File
import java.time.Instant
import java.util.concurrent.CopyOnWriteArrayList
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class StudentCollectionScreenTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun oldestCoverOpensDuplicatesAndOnlySelectedOriginalLoads() {
    val gallery = FixtureGallery()
    show(gallery)
    waitFor("gallery-entry-uniform")
    compose.waitUntil(5_000) { gallery.thumbnailIds.isNotEmpty() }
    assertEquals(listOf("oldest"), gallery.thumbnailIds.toList())
    if (captureEnabled()) {
      val cover = compose.onNodeWithTag("gallery-entry-uniform")
      // Wait for the blue fixture card to replace the thumbnail placeholder.
      compose.waitUntil(5_000) {
        val bitmap = cover.captureToImage().asAndroidBitmap()
        val pixel = bitmap.getPixel(bitmap.width / 4, bitmap.height / 8)
        Color.red(pixel) < 100 && Color.blue(pixel) > 100
      }
      capture("student-collection")
    }
    scrollTo("gallery-grid", "gallery-entry-secret")
    compose.onNodeWithText(compose.activity.getString(R.string.gallery_secret)).assertIsDisplayed()
    compose.onNodeWithTag("gallery-entry-secret").assertHasNoClickAction()
    scrollTo("gallery-grid", "gallery-entry-uniform")
    compose.onNodeWithTag("gallery-entry-uniform").performClick()
    scrollTo("gallery-group-grid", "gallery-card-oldest")
    compose.onNodeWithTag("gallery-card-oldest").performClick()
    waitFor("gallery-original-image")
    compose.onNodeWithTag("gallery-viewer-previous").assertIsNotEnabled()
    capture("student-collection-card", "gallery-viewer")
    compose.onNodeWithTag("gallery-viewer-next").performClick()
    compose.waitUntil(5_000) { gallery.originalIds.size == 2 }
    assertEquals(listOf("oldest", "newer"), gallery.originalIds.toList())
    compose.onNodeWithTag("gallery-viewer-next").assertIsNotEnabled()
    compose.onNodeWithTag("gallery-viewer-back").performClick()
    compose.onNodeWithTag("gallery-group-grid").assertIsDisplayed()
    compose.onNodeWithTag("gallery-group-back").performClick()
    compose.onNodeWithTag("gallery-grid").assertIsDisplayed()
  }

  @Test fun retrySearchAndAcquiredCardsKeepOlderRosterEntriesAccessible() {
    val gallery = FixtureGallery(failFirstLoad = true)
    show(gallery)
    waitFor("gallery-retry")
    compose.onNodeWithTag("gallery-retry").performClick()
    waitFor("gallery-search")
    compose.onNodeWithTag("gallery-search").performTextInput("サンプル(制服)")
    compose.onNodeWithTag("gallery-search").performImeAction()
    compose.onNodeWithTag("gallery-entry-uniform").assertIsDisplayed()
    compose.onNodeWithTag("gallery-entry-summer").assertDoesNotExist()
    compose.onNodeWithTag("gallery-entry-secret").assertDoesNotExist()
    compose.onNodeWithTag("gallery-cards-tab").performClick()
    compose.onNodeWithTag("gallery-search").performClick()
    compose.onNodeWithTag("gallery-search").performTextClearance()
    compose.onNodeWithTag("gallery-search").performImeAction()
    scrollTo("gallery-grid", "gallery-card-legacy")
    compose.onNodeWithTag("gallery-card-legacy").assertIsDisplayed()
  }

  @Test fun inlineFiltersClearSearchAndConditionsWhileKeepingSort() {
    show(FixtureGallery())
    waitFor("gallery-search")
    compose.onNodeWithTag("gallery-sort")
      .assert(hasText(compose.activity.getString(R.string.gallery_common)))
      .performClick()
    compose.onNodeWithText(compose.activity.getString(R.string.gallery_first_oldest)).performClick()
    compose.onNodeWithTag("gallery-school-filter").performClick()
    compose.onNode(hasText("架空学園") and hasAnyAncestor(hasTestTag("gallery-school-filter-menu")))
      .performClick()
    compose.onNodeWithTag("gallery-rarity-filter").performClick()
    compose.onNode(hasContentDescription(compose.activity.getString(R.string.gallery_stars, 2)) and
      hasAnyAncestor(hasTestTag("gallery-rarity-filter-menu"))).performClick()
    compose.onNodeWithTag("gallery-acquired-only").performClick().assertIsOn()
    compose.onNodeWithTag("gallery-search").performTextInput("サンプル(制服)")
    compose.onNodeWithTag("gallery-search").performImeAction()
    compose.onNodeWithTag("gallery-entry-secret").assertDoesNotExist()
    compose.onNodeWithTag("gallery-clear-filters").performClick()
    compose.onNodeWithTag("gallery-clear-filters").assertDoesNotExist()
    compose.onNodeWithTag("gallery-sort")
      .assert(hasText(compose.activity.getString(R.string.gallery_first_oldest)))
    scrollTo("gallery-grid", "gallery-entry-secret")
    compose.onNodeWithTag("gallery-entry-secret").assertIsDisplayed()
  }

  @Test fun originalsUseSavedImagesOfflineAndRetryMissesWhenConnectionReturns() {
    val online = mutableStateOf(true)
    val gallery = FixtureGallery(isOnline = { online.value })
    show(gallery, isOnline = { online.value })
    waitFor("gallery-entry-uniform")
    compose.runOnIdle { online.value = false }
    scrollTo("gallery-grid", "gallery-entry-uniform")
    compose.onNodeWithTag("gallery-entry-uniform").performClick()
    compose.waitUntil(5_000) { "newer" in gallery.thumbnailIds }
    scrollTo("gallery-group-grid", "gallery-card-oldest")
    compose.onNodeWithTag("gallery-card-oldest").performClick()
    waitFor("gallery-original-image")
    assertEquals(listOf("oldest"), gallery.originalIds.toList())

    compose.onNodeWithTag("gallery-viewer-next").performClick()
    val offlineMessage = hasText(compose.activity.getString(R.string.gallery_offline)) and
      hasAnyAncestor(hasTestTag("gallery-viewer"))
    compose.waitUntil(5_000) { compose.onAllNodes(offlineMessage).fetchSemanticsNodes().isNotEmpty() }
    compose.onNode(offlineMessage).assertIsDisplayed()
    compose.onNodeWithTag("gallery-retry").assertDoesNotExist()
    compose.runOnIdle { online.value = true }
    waitFor("gallery-original-image")
    assertEquals(listOf("oldest", "newer", "newer"), gallery.originalIds.toList())
  }

  private fun show(gallery: StudentGallery, isOnline: () -> Boolean = { true }) {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        BootstrapUi(BootstrapScreen.State(ThemeChoice.Light, canReadRecords = true, debateOnline = isOnline(),
          recordOwner = "gallery-fixture", backStack = listOf(RecordsList, StudentGalleryRoute),
          studentGallery = gallery, eventSink = {}))
      }
    }
  }

  private fun waitFor(tag: String) {
    compose.waitUntil(5_000) { compose.onAllNodes(hasTestTag(tag)).fetchSemanticsNodes().isNotEmpty() }
  }

  private fun scrollTo(grid: String, tag: String) {
    compose.onNodeWithTag(grid).performScrollToNode(hasTestTag(tag))
  }

  private fun captureEnabled() =
    InstrumentationRegistry.getArguments().getString("shittimCaptureStudentCollection") == "true"

  private fun capture(fileName: String, tag: String? = null) {
    if (!captureEnabled()) return
    val bitmap = (if (tag == null) compose.onRoot() else compose.onNodeWithTag(tag)).captureToImage().asAndroidBitmap()
    File(requireNotNull(compose.activity.getExternalFilesDir(null)), "$fileName.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }

  private class FixtureGallery(
    private var failFirstLoad: Boolean = false,
    private val isOnline: () -> Boolean = { true },
  ) : StudentGallery {
    private val oldest = GalleryCard("oldest", "uniform", "サンプル（制服）", "架空学園", 2,
      Instant.parse("2026-11-11T13:00:00Z"), "participant-a", "架空の獲得者", false, true)
    private val newer = oldest.copy(cardId = "newer", postedAt = Instant.parse("2026-11-12T13:00:00Z"),
      performer = "participant-b")
    private val legacy = oldest.copy(cardId = "legacy", studentId = "removed", studentName = "旧名簿の生徒",
      schoolName = "旧学園", rarity = null, postedAt = Instant.parse("2026-11-10T13:00:00Z"), thumbnailReady = false)
    private val collection = StudentCollection(
      listOf(StudentCollectionEntry("uniform", oldest.studentName, oldest.schoolName, 2, listOf(oldest, newer), 0),
        StudentCollectionEntry("summer", "サンプル（水着）", "架空学園", 3, emptyList(), null),
        StudentCollectionEntry("secret", null, null, 4, emptyList(), null)),
      listOf(newer, oldest, legacy),
    )
    val thumbnailIds = CopyOnWriteArrayList<String>()
    val originalIds = CopyOnWriteArrayList<String>()
    private val sampleImage = fixtureImage()

    override suspend fun loadCollection(): StudentCollection {
      if (failFirstLoad) {
        failFirstLoad = false
        throw GalleryReadException(GalleryReadFailure.UNAVAILABLE)
      }
      return collection
    }

    override suspend fun loadThumbnails(ids: List<String>): Map<String, ByteArray> {
      thumbnailIds.addAll(ids)
      return ids.filter { isOnline() || it == oldest.cardId }.associateWith { sampleImage }
    }

    override suspend fun loadOriginal(id: String): ByteArray {
      originalIds.add(id)
      if (!isOnline() && id != oldest.cardId) throw GalleryReadException(GalleryReadFailure.UNAVAILABLE)
      return sampleImage
    }

    override fun close() = Unit
  }
}

private fun fixtureImage(): ByteArray {
  val bitmap = Bitmap.createBitmap(768, 355, Bitmap.Config.ARGB_8888)
  val canvas = Canvas(bitmap)
  val paint = Paint(Paint.ANTI_ALIAS_FLAG)
  paint.shader = LinearGradient(0f, 0f, 768f, 355f, Color.rgb(28, 67, 122),
    Color.rgb(103, 154, 190), Shader.TileMode.CLAMP)
  canvas.drawRect(0f, 0f, 768f, 355f, paint)
  paint.shader = null
  paint.color = Color.rgb(225, 238, 248)
  canvas.drawCircle(540f, 137f, 70f, paint)
  canvas.drawOval(405f, 230f, 675f, 480f, paint)
  paint.color = Color.WHITE
  paint.textSize = 48f
  canvas.drawText("SAMPLE", 64f, 156f, paint)
  paint.textSize = 30f
  canvas.drawText("サンプル（制服）", 64f, 215f, paint)
  return ByteArrayOutputStream().use { buffer ->
    bitmap.compress(Bitmap.CompressFormat.PNG, 100, buffer)
    bitmap.recycle()
    buffer.toByteArray()
  }
}
