package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasContentDescription
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import java.math.BigDecimal
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class InsightsScreenUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun standardTabsAndPersonaControlsExposeSelectionAndTypedEvents() {
    val state = mutableStateOf(insightsSampleState())
    val events = mutableListOf<InsightsEvent>()
    show(state, events::add)
    compose.onNodeWithTag("insights-tab-Affection").assertIsSelected()
    compose.onNodeWithTag("insights-persona-participant-a").assertIsSelected()
    compose.onNodeWithTag("insights-persona-participant-b").performClick()
    compose.runOnIdle { state.value = state.value.copy(participant = "participant-b") }
    compose.onNodeWithTag("insights-persona-participant-b").assertIsSelected()
    compose.onNodeWithTag("insights-view-Requesters").performClick()
    compose.onNodeWithTag("insights-tab-Rankings").performClick()
    compose.onNodeWithTag("insights-tab-Costs").performClick()
    compose.onNodeWithTag("insights-refresh").performClick()
    compose.onNodeWithTag("insights-back").performClick()
    compose.runOnIdle {
      assertEquals(listOf(InsightsEvent.Participant("participant-b"),
        InsightsEvent.View(InsightsAffectionView.Requesters), InsightsEvent.Tab(InsightsTab.Rankings),
        InsightsEvent.Tab(InsightsTab.Costs), InsightsEvent.Refresh, InsightsEvent.Back), events)
    }
    capture("insights-affection-light")
  }

  @Test fun narrowLargeTextUsesAccessiblePersonaMenuAndReadableScoreSummary() {
    val state = mutableStateOf(insightsSampleState())
    val events = mutableListOf<InsightsEvent>()
    show(state, events::add, width = 320, fontScale = 2f, dark = true)
    compose.onNodeWithTag("insights-persona-picker").assertIsDisplayed().performClick()
    compose.onNodeWithTag("insights-persona-participant-c").assertIsDisplayed().performClick()
    compose.runOnIdle { assertEquals(listOf(InsightsEvent.Participant("participant-c")), events) }
    val hearts = compose.activity.getString(R.string.insights_hearts, 10)
    compose.onNodeWithTag("insights-content").performScrollToNode(hasContentDescription(hearts))
    compose.onNode(hasContentDescription(hearts)).assertIsDisplayed()
    compose.onNodeWithText("1000 / 1000").assertIsDisplayed()
    capture("insights-affection-large-text")
    compose.onNodeWithTag("insights-content").performScrollToNode(hasText("架空のとても長い名前を使う依頼者09"))
    compose.onNodeWithText("架空のとても長い名前を使う依頼者09").assertIsDisplayed()
  }

  @Test fun cachedRequesterPagesStayReachableOfflineAndCannotFetchUnsavedPages() {
    val sample = insightsSampleState()
    val state = mutableStateOf(sample.copy(view = InsightsAffectionView.Requesters, online = false,
      requesters = sample.requesters.copy(saved = true)))
    val events = mutableListOf<InsightsEvent>()
    show(state, events::add)
    compose.onNodeWithTag("insights-saved").assertIsDisplayed()
    compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag("insights-requester-next"))
    compose.onNodeWithTag("insights-requester-previous").assertIsNotEnabled()
    compose.onNodeWithTag("insights-requester-next").assertIsEnabled().performClick()
    compose.runOnIdle { state.value = state.value.copy(requesterPage = 1) }
    compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag("insights-requester-next"))
    compose.onNodeWithTag("insights-requester-previous").assertIsEnabled().performClick()
    compose.onNodeWithTag("insights-requester-next").assertIsNotEnabled()
    compose.runOnIdle { assertEquals(listOf(InsightsEvent.NextRequester, InsightsEvent.PreviousRequester), events) }
  }

  @Test fun costAmountsRoundUpAndUnavailableNeverLooksLikeZeroYen() {
    val sample = insightsSampleState()
    val costs = requireNotNull(sample.costs[InsightsPeriod.Today]?.data)
    val state = mutableStateOf(sample.copy(tab = InsightsTab.Costs, period = InsightsPeriod.Today))
    show(state)
    compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag("insights-cost-total"))
    compose.onNodeWithText("131 円").assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.insights_cost_partial)).assertIsDisplayed()
    capture("insights-costs-light")
    compose.runOnIdle {
      assertEquals("0", formatInsightsYen(BigDecimal("0.000000")))
      assertEquals("1", formatInsightsYen(BigDecimal("0.000001")))
      assertEquals("1,001", formatInsightsYen(BigDecimal("1000.000001")))
      state.value = state.value.copy(costs = mapOf(InsightsPeriod.Today to InsightsResource(
        data = costs.copy(status = "unavailable", total = BigDecimal.ZERO))))
    }
    compose.onNodeWithTag("insights-cost-total").assertDoesNotExist()
    compose.onNodeWithTag("insights-content").performScrollToNode(hasText(compose.activity.getString(R.string.insights_cost_unavailable)))
    compose.onNodeWithText(compose.activity.getString(R.string.insights_cost_unavailable)).assertIsDisplayed()
    compose.onNodeWithText("0 円").assertDoesNotExist()
  }

  @Test fun cursorExpiryRetainsRowsAndOffersExplicitGenerationRefresh() {
    val sample = insightsSampleState()
    val pages = requireNotNull(sample.participants.data).map { it.copy(nextCursor = "fictional-expired-cursor") }
    val state = mutableStateOf(sample.copy(participants = InsightsResource(pages,
      error = InsightsFailure.CURSOR_INVALID, saved = true)))
    val events = mutableListOf<InsightsEvent>()
    show(state, events::add, dark = true)
    compose.onNodeWithTag("insights-content").performScrollToNode(hasText(compose.activity.getString(R.string.insights_reload)))
    compose.onNodeWithText(compose.activity.getString(R.string.insights_reload)).performClick()
    compose.onNodeWithTag("insights-content").performScrollToNode(hasText("架空の依頼者01"))
    compose.onNodeWithText("架空の依頼者01").assertIsDisplayed()
    capture("insights-affection-dark")
    compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag("insights-more"))
    compose.onNodeWithTag("insights-more").assertIsNotEnabled()
    compose.runOnIdle { assertEquals(listOf(InsightsEvent.Refresh), events) }
  }

  @Test fun wideRankingsShowBothSectionsAndExplainTheirDifferentDenominators() {
    val sample = insightsSampleState()
    val state = mutableStateOf(sample.copy(tab = InsightsTab.Rankings))
    show(state, width = 900)
    compose.onNodeWithText(compose.activity.getString(R.string.insights_wins_share)).assertIsDisplayed()
    compose.onNodeWithText(compose.activity.getString(R.string.insights_requests_share)).assertIsDisplayed()
    capture("insights-rankings-wide")
  }

  /** Opt-in visual inspection only; no screenshot comparisons or extra normal-CI work. */
  @Test fun captureNativeInsightsLayouts() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureInsights") != "true") return
    val sample = insightsSampleState()
    data class Layout(val name: String, val width: Int, val font: Float, val dark: Boolean)
    val layouts = listOf(Layout("light", 360, 1f, false), Layout("dark", 360, 1f, true),
      Layout("320dp-2x", 320, 2f, false), Layout("wide", 900, 1f, false))
    for (layout in layouts) {
      for (tab in InsightsTab.entries) {
        val state = mutableStateOf(sample.copy(tab = tab))
        show(state, width = layout.width, fontScale = layout.font, dark = layout.dark)
        compose.waitForIdle()
        capture("insights-${layout.name}-${tab.name.lowercase()}-controls")
        when (tab) {
          InsightsTab.Affection -> compose.onNodeWithTag("insights-content").performScrollToNode(
            hasText("架空のとても長い名前を使う依頼者09"))
          InsightsTab.Rankings -> compose.onNodeWithTag("insights-content").performScrollToNode(
            hasText(compose.activity.getString(R.string.insights_wins)))
          InsightsTab.Costs -> compose.onNodeWithTag("insights-content").performScrollToNode(
            hasTestTag("insights-cost-total"))
        }
        capture("insights-${layout.name}-${tab.name.lowercase()}")
        if (tab == InsightsTab.Affection) {
          compose.runOnIdle { state.value = state.value.copy(view = InsightsAffectionView.Requesters) }
          compose.onNodeWithTag("insights-content").performScrollToNode(hasText("架空の依頼者01"))
          capture("insights-${layout.name}-requesters")
        }
        if (tab == InsightsTab.Costs) {
          compose.onNodeWithTag("insights-content").performScrollToNode(
            hasText(compose.activity.getString(R.string.insights_cost_breakdown)))
          capture("insights-${layout.name}-cost-breakdown")
        }
      }
    }
  }

  private fun show(state: MutableState<InsightsUiState>, onEvent: (InsightsEvent) -> Unit = {},
    width: Int = 360, fontScale: Float = 1f, dark: Boolean = false) {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        // Fit a tablet-sized logical viewport inside the test display without clipping its second column.
        val viewportDensity = density.density * minOf(1f, LocalConfiguration.current.screenWidthDp.toFloat() / width)
        CompositionLocalProvider(LocalDensity provides Density(viewportDensity, fontScale)) {
          ShittimTheme(dark) {
            Box(Modifier.width(width.dp).fillMaxHeight()) { InsightsScreen(state.value, onEvent) }
          }
        }
      }
    }
  }

  private fun capture(name: String) {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureInsights") != "true") return
    val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
    File(requireNotNull(compose.activity.getExternalFilesDir(null)), "$name.png").outputStream().use {
      bitmap.compress(Bitmap.CompressFormat.PNG, 100, it)
    }
  }
}
