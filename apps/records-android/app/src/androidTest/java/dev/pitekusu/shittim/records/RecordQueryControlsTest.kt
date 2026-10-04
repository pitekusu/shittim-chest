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
import androidx.compose.ui.test.assertIsOn
import androidx.compose.ui.test.assertIsOff
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
@ScreenTest
class RecordQueryControlsTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun searchRequesterWinnerOrderAndResetAreImmediateAndHaveSelectionSemantics() {
    val query = mutableStateOf(RecordListQuery())
    val requesters = requesterChoices()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent { ShittimTheme(false) {
        Column(Modifier.verticalScroll(rememberScrollState()).padding(24.dp)) {
          RecordQueryControls(query.value, requesters = requesters) { event ->
            query.value = when (event) {
              is BootstrapScreen.Event.SearchRecords -> query.value.copy(text = event.text)
              is BootstrapScreen.Event.SelectWinner -> query.value.copy(winner = event.winner)
              is BootstrapScreen.Event.SelectRequester -> query.value.copy(requesterName = event.displayName)
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
    compose.onNodeWithTag("requester-0").performScrollTo().performClick().assertIsSelected()
    compose.onNodeWithTag("winner-Plana").performScrollTo().performClick().assertIsSelected()
    compose.onNodeWithTag("order-Oldest").performScrollTo().performClick().assertIsOn()
    compose.onNodeWithTag("order-Newest").assertIsOff()
    compose.runOnIdle {
      assertEquals(RecordListQuery("架空の相談", RecordWinner.Plana, RecordOrder.Oldest,
        requesters.first().displayName), query.value)
    }
    compose.onNodeWithText(label(R.string.record_search_reset)).performScrollTo().performClick()
    compose.runOnIdle { assertEquals(RecordListQuery(), query.value) }
    compose.onNodeWithTag("requester-all").performScrollTo().assertIsSelected()
    compose.onNodeWithTag("winner-All").performScrollTo().assertIsSelected()
  }

  @Test fun controlsRemainReadableInBothThemesAndAt320dpWithDoubleText() {
    val dark = mutableStateOf(false)
    val large = mutableStateOf(false)
    val query = mutableStateOf(RecordListQuery(winner = RecordWinner.Plana))
    val requesters = requesterChoices()
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        CompositionLocalProvider(LocalDensity provides Density(density.density, if (large.value) 2f else 1f)) {
          ShittimTheme(dark.value) { ShittimBackdrop {
            Column(Modifier.width(if (large.value) 320.dp else 360.dp)
              .verticalScroll(rememberScrollState()).padding(24.dp).testTag("query-preview")) {
              RecordQueryControls(query.value, requesters = requesters) { event ->
                query.value = when (event) {
                  is BootstrapScreen.Event.SelectOrder -> query.value.copy(order = event.order)
                  is BootstrapScreen.Event.SelectRequester -> query.value.copy(requesterName = event.displayName)
                  else -> query.value
                }
              }
            }
          } }
        }
      }
    }
    compose.onNodeWithTag("winner-Plana").assertIsSelected()
    compose.onNodeWithTag("order-Newest").performScrollTo().assertIsOn()
    capture("motion-order-light")
    compose.runOnIdle { dark.value = true }
    compose.onNodeWithTag("order-Oldest").performClick().assertIsOn()
    capture("motion-order-dark")
    compose.runOnIdle { large.value = true }
    requesters.forEachIndexed { index, requester ->
      compose.onNodeWithTag("requester-$index").performScrollTo().assertIsDisplayed()
        .performClick().assertIsSelected()
      compose.onNodeWithTag("requester-avatar-$index", useUnmergedTree = true).assertIsDisplayed()
      compose.runOnIdle { assertEquals(requester.displayName, query.value.requesterName) }
    }
    capture("query-requesters-large-text")
    compose.onNodeWithTag("requester-all").performScrollTo().performClick().assertIsSelected()
    compose.onNodeWithTag("winner-Abe").performScrollTo().assertIsDisplayed()
    compose.onNodeWithTag("order-Oldest").performScrollTo().assertIsOn().assertIsDisplayed()
    compose.onNodeWithTag("order-Newest").performClick().assertIsOn()
    capture("motion-order-large-text")
    compose.onNodeWithText(label(R.string.record_sort_label)).performScrollTo().assertIsDisplayed()
    compose.onNodeWithText(label(R.string.record_search_reset)).performScrollTo().assertIsDisplayed()
  }

  private fun requesterChoices(): List<RecordRequesterChoice> {
    val bytes = compose.activity.resources.openRawResource(R.drawable.participant_b).use { it.readBytes() }
    return listOf(
      RecordRequesterChoice("架空の依頼者A", RecordAvatar(null, "cyan", bytes = bytes)),
      RecordRequesterChoice("架空の依頼者B", RecordAvatar(null, "pink")),
      RecordRequesterChoice("架空の依頼者C：表示名が長い場合も選べる確認用プロフィール", RecordAvatar(null, "lavender")),
    )
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
