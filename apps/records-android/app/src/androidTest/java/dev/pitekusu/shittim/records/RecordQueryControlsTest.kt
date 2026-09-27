package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performImeAction
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordQueryControlsTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun searchWinnerOrderAndResetAreImmediateAndHaveSelectionSemantics() {
    val query = mutableStateOf(RecordListQuery())
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column(Modifier.verticalScroll(rememberScrollState()).padding(24.dp)) {
          RecordQueryControls(query.value) { event ->
            query.value = when (event) {
              is BootstrapScreen.Event.SearchRecords -> query.value.copy(text = event.text)
              is BootstrapScreen.Event.SelectWinner -> query.value.copy(winner = event.winner)
              is BootstrapScreen.Event.SelectOrder -> query.value.copy(order = event.order)
              BootstrapScreen.Event.ClearRecordQuery -> RecordListQuery()
              else -> error("unexpected_control_event")
            }
          }
        }
      } }
    }
    compose.onNodeWithTag("record-search").performTextInput("架空の相談")
    compose.onNodeWithTag("record-search").performImeAction()
    compose.onNodeWithTag("winner-Plana").performScrollTo().performClick().assertIsSelected()
    compose.onNodeWithText(label(R.string.record_sort_oldest)).performScrollTo().performClick()
    compose.runOnIdle {
      assertEquals(RecordListQuery("架空の相談", RecordWinner.Plana, RecordOrder.Oldest), query.value)
    }
    compose.onNodeWithText(label(R.string.record_search_reset)).performScrollTo().performClick()
    compose.runOnIdle { assertEquals(RecordListQuery(), query.value) }
    compose.onNodeWithTag("winner-All").performScrollTo().assertIsSelected()
  }

  @Test fun controlsRemainReadableInBothThemesAndAt320dpWithDoubleText() {
    val dark = mutableStateOf(false)
    val large = mutableStateOf(false)
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, if (large.value) 2f else 1f)) {
          ShittimTheme(dark.value) { ShittimBackdrop {
            Column(Modifier.width(if (large.value) 320.dp else 360.dp)
              .verticalScroll(rememberScrollState()).padding(24.dp).testTag("query-preview")) {
              RecordQueryControls(RecordListQuery(winner = RecordWinner.Plana)) {}
            }
          } }
        }
      }
    }
    compose.onNodeWithTag("winner-Plana").assertIsSelected()
    capture("search-light")
    compose.runOnIdle { dark.value = true }
    capture("search-dark")
    compose.runOnIdle { large.value = true }
    compose.onNodeWithTag("winner-Abe").performScrollTo().assertIsDisplayed()
    capture("search-large-text")
    compose.onNodeWithText(label(R.string.record_sort_label)).performScrollTo().assertIsDisplayed()
    compose.onNodeWithText(label(R.string.record_search_reset)).performScrollTo().assertIsDisplayed()
  }

  private fun label(id: Int): String = compose.activity.getString(id)

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureSearch") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(compose.activity.cacheDir, "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
