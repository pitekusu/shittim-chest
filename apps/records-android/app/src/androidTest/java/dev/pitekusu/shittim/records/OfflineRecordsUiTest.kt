package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.hasClickAction
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OfflineRecordsUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun savedDetailAndUpdateFailureRemainReadableWithoutSyncControls() {
    val dark = mutableStateOf(false)
    val large = mutableStateOf(false)
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, if (large.value) 2f else 1f)) {
          ShittimTheme(dark.value) {
            LazyColumn(Modifier.width(if (large.value) 320.dp else 360.dp)) {
              item {
                RecordPreviewPanel(RecordPreviewState.Ready(
                  RecordPreview("秋の休日をどう過ごす？", "近所の公園へ、散歩に出かけましょう。", "アロナ"),
                  saved = true, refreshFailure = RecordReadFailure.UNAVAILABLE), events::add)
              }
            }
          }
        }
      }
    }
    for ((name, isDark, isLarge) in listOf(Triple("offline-light", false, false),
      Triple("offline-dark", true, false), Triple("offline-large-text", true, true))) {
      compose.runOnIdle { dark.value = isDark; large.value = isLarge }
      compose.onNodeWithText(label(R.string.record_saved)).assertIsDisplayed()
      compose.onNodeWithText(label(R.string.record_refresh_failed)).assertIsDisplayed()
      compose.onAllNodes(hasClickAction()).assertCountEquals(1) // Only the return-to-list action.
      capture(name)
    }
    compose.onNodeWithText(label(R.string.record_close)).performClick()
    assertEquals(listOf(BootstrapScreen.Event.CloseRecord), events)
  }

  @Test fun authorizedOfflineDetailClosesOnBackAndDisappearsWhenLocked() {
    val events = mutableListOf<BootstrapScreen.Event>()
    val state = mutableStateOf(BootstrapScreen.State(ThemeChoice.Dark, SessionState.Unavailable,
      record = RecordPreviewState.Ready(RecordPreview("保存済みの架空議題", "架空の結論", "プラナ"), saved = true),
      selectedRecordId = "r".repeat(43), canReadRecords = true, eventSink = events::add))
    compose.activityRule.scenario.onActivity { it.setContent { BootstrapUi(state.value) } }
    compose.waitForIdle()
    compose.activityRule.scenario.onActivity { it.onBackPressedDispatcher.onBackPressed() }
    assertEquals(listOf(BootstrapScreen.Event.CloseRecord), events)
    compose.runOnIdle { state.value = BootstrapScreen.State(ThemeChoice.Dark) {} }
    compose.onNodeWithText("保存済みの架空議題").assertDoesNotExist()
    compose.onNodeWithText(label(R.string.record_saved)).assertDoesNotExist()
  }

  private fun label(id: Int) = compose.activity.getString(id)

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureOffline") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
