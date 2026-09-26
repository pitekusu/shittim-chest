package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
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
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordSyncStatusTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun automaticStatusHasNoActionsAndRemainsReadableInBothThemesAndLargeText() {
    val state = mutableStateOf<RecordSyncState>(RecordSyncState.Idle)
    val dark = mutableStateOf(false)
    val largeText = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, if (largeText.value) 2f else 1f)) {
          ShittimTheme(dark.value) {
            Surface(color = MaterialTheme.colorScheme.background) {
              Box((if (largeText.value) Modifier.width(320.dp) else Modifier.fillMaxWidth()).padding(24.dp)) {
                RecordSyncStatus(state.value)
              }
            }
          }
        }
      }
    }
    compose.onAllNodes(hasClickAction()).assertCountEquals(0)
    compose.onNodeWithText(label(R.string.record_sync_running)).assertDoesNotExist()
    compose.runOnIdle { state.value = RecordSyncState.Running }
    compose.onNodeWithText(label(R.string.record_sync_running)).assertIsDisplayed()
    capture("sync-light")
    compose.runOnIdle { dark.value = true }
    compose.onNodeWithText(label(R.string.record_sync_running)).assertIsDisplayed()
    capture("sync-dark")
    compose.runOnIdle { state.value = RecordSyncState.Completed }
    compose.onNodeWithText(label(R.string.record_sync_running)).assertDoesNotExist()
    compose.runOnIdle { state.value = RecordSyncState.Failed(RecordReadFailure.STORAGE_UNAVAILABLE); largeText.value = true }
    compose.onNodeWithText(label(R.string.record_sync_storage_error)).assertIsDisplayed()
    compose.onAllNodes(hasClickAction()).assertCountEquals(0)
    capture("sync-large-text")
    compose.runOnIdle { state.value = RecordSyncState.Completed }
    compose.onNodeWithText(label(R.string.record_sync_storage_error)).assertDoesNotExist()
  }

  private fun label(id: Int): String = compose.activity.getString(id)

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureSync") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
