package dev.pitekusu.shittim.records

import android.view.ViewGroup
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.mutableStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.assertIsSelected
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipe
import androidx.compose.ui.test.swipeLeft
import androidx.compose.ui.test.swipeRight
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class InsightsSwipeUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  private lateinit var state: MutableState<InsightsUiState>
  private val events = mutableListOf<InsightsEvent>()

  @Test fun personasLeadIntoTheNextTabsAndBothEndsStayBounded() {
    show()
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-a")
    swipe()
    assertSelection(InsightsTab.Affection, "participant-b")
    swipe()
    assertSelection(InsightsTab.Affection, "participant-c")
    swipe()
    assertSelection(InsightsTab.Rankings)
    swipe()
    assertSelection(InsightsTab.Costs)
    swipe()
    assertSelection(InsightsTab.Costs)
    swipe(forward = false)
    assertSelection(InsightsTab.Rankings)
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-c")
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-b")
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-a")
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-a")
    compose.runOnIdle {
      assertEquals(listOf(InsightsEvent.Participant("participant-b"),
        InsightsEvent.Participant("participant-c"), InsightsEvent.Tab(InsightsTab.Rankings),
        InsightsEvent.Tab(InsightsTab.Costs), InsightsEvent.Tab(InsightsTab.Rankings),
        InsightsEvent.Tab(InsightsTab.Affection), InsightsEvent.Participant("participant-b"),
        InsightsEvent.Participant("participant-a")), events)
    }
  }

  @Test fun directChoicesSkipIntermediatePagesAndAffectionReturnsToTheLastPersona() {
    show()
    compose.onNodeWithTag("insights-persona-participant-c").performClick()
    assertSelection(InsightsTab.Affection, "participant-c")
    tab(InsightsTab.Costs)
    tab(InsightsTab.Affection)
    assertSelection(InsightsTab.Affection, "participant-c")
    compose.onNodeWithTag("insights-persona-participant-a").performClick()
    assertSelection(InsightsTab.Affection, "participant-a")
    tab(InsightsTab.Rankings)
    tab(InsightsTab.Costs)
    tab(InsightsTab.Affection)
    assertSelection(InsightsTab.Affection, "participant-a")
    compose.runOnIdle {
      assertEquals(listOf(InsightsEvent.Participant("participant-c"),
        InsightsEvent.Tab(InsightsTab.Costs), InsightsEvent.Tab(InsightsTab.Affection),
        InsightsEvent.Participant("participant-a"), InsightsEvent.Tab(InsightsTab.Rankings),
        InsightsEvent.Tab(InsightsTab.Costs), InsightsEvent.Tab(InsightsTab.Affection)), events)
    }
  }

  @Test fun requesterViewHasThreePagesAndChangingItsLayoutCannotSelectAnotherTab() {
    show(insightsSampleState().copy(participant = "participant-b"))
    compose.onNodeWithTag("insights-view-Requesters").performClick()
    assertSelection(InsightsTab.Affection, "participant-b")
    swipe()
    assertSelection(InsightsTab.Rankings)
    swipe()
    assertSelection(InsightsTab.Costs)
    swipe()
    assertSelection(InsightsTab.Costs)
    swipe(forward = false)
    assertSelection(InsightsTab.Rankings)
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-b")
    compose.onNodeWithTag("insights-view-Participants").performClick()
    assertSelection(InsightsTab.Affection, "participant-b")
    compose.onNodeWithTag("insights-persona-participant-b").assertIsSelected()
    for (selectedTab in listOf(InsightsTab.Rankings, InsightsTab.Costs)) {
      tab(selectedTab)
      val eventCount = events.size
      for (view in listOf(InsightsAffectionView.Requesters, InsightsAffectionView.Participants)) {
        compose.runOnIdle { state.value = state.value.copy(view = view) }
        assertSelection(selectedTab, "participant-b")
        compose.runOnIdle { assertEquals(eventCount, events.size) }
      }
    }
    tab(InsightsTab.Affection)
    val rankings = compose.onNodeWithTag("insights-tab-Rankings")
      .fetchSemanticsNode().config[SemanticsActions.OnClick].action!!
    val requesters = compose.onNodeWithTag("insights-view-Requesters")
      .fetchSemanticsNode().config[SemanticsActions.OnClick].action!!
    compose.runOnIdle { rankings(); requesters() }
    assertSelection(InsightsTab.Rankings, "participant-b")
    compose.onNodeWithTag("insights-content").performScrollToNode(
      hasText(compose.activity.getString(R.string.insights_wins)))
    compose.runOnIdle { assertEquals(InsightsAffectionView.Requesters, state.value.view) }
  }

  @Test fun personaReadingPositionsAndTheSelectedCostPeriodSurvivePageChanges() {
    show()
    val aronaPosition = readPartway()
    swipe()
    assertSelection(InsightsTab.Affection, "participant-b")
    assertEquals(0f, readingPosition(), 0.01f)
    val planaPosition = readPartway()
    tab(InsightsTab.Costs)
    compose.onNodeWithTag("insights-period-Month").performClick()
    val costPosition = readPartway()
    swipe(forward = false)
    assertSelection(InsightsTab.Rankings)
    swipe()
    assertSelection(InsightsTab.Costs)
    compose.runOnIdle { assertEquals(InsightsPeriod.Month, state.value.period) }
    assertEquals(costPosition, readingPosition(), 0.01f)
    tab(InsightsTab.Affection)
    assertSelection(InsightsTab.Affection, "participant-b")
    assertEquals(planaPosition, readingPosition(), 0.01f)
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-a")
    assertEquals(aronaPosition, readingPosition(), 0.01f)
  }

  @Test fun prefetchAndCancelledSlowDragsDoNotOpenAnotherPersonaOrTab() {
    show()
    compose.runOnIdle { assertTrue(events.isEmpty()) }
    shortDrag()
    assertSelection(InsightsTab.Affection, "participant-a")
    compose.runOnIdle { assertTrue(events.isEmpty()) }
    compose.onNodeWithTag("insights-persona-participant-c").performClick()
    assertSelection(InsightsTab.Affection, "participant-c")
    compose.runOnIdle { events.clear() }
    shortDrag()
    assertSelection(InsightsTab.Affection, "participant-c")
    compose.runOnIdle { assertTrue(events.isEmpty()) }
  }

  @Test fun savedStateRestoresTheSelectedPersonaAndItsReadingPositionWithoutNewEvents() {
    state = mutableStateOf(insightsSampleState().copy(participant = "participant-b"))
    val restoration = StateRestorationTester(compose)
    // MainActivity installs its own content before this focused restoration host.
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent { ShittimTheme { InsightsScreen(state.value, ::accept) } }
    swipe()
    assertSelection(InsightsTab.Affection, "participant-c")
    val position = readPartway()
    val eventCount = events.size
    restoration.emulateSavedInstanceStateRestore()
    assertSelection(InsightsTab.Affection, "participant-c")
    assertEquals(position, readingPosition(), 0.01f)
    compose.runOnIdle { assertEquals(eventCount, events.size) }
    swipe(forward = false)
    assertSelection(InsightsTab.Affection, "participant-b")
  }

  private fun show(initial: InsightsUiState = insightsSampleState()) {
    state = mutableStateOf(initial)
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme {
        Box(Modifier.width(360.dp).height(640.dp)) { InsightsScreen(state.value, ::accept) }
      }
    } }
    compose.waitForIdle()
  }

  private fun accept(event: InsightsEvent) {
    events += event
    state.value = when (event) {
      is InsightsEvent.Tab -> state.value.copy(tab = event.value)
      is InsightsEvent.Participant -> state.value.copy(participant = event.value)
      is InsightsEvent.View -> state.value.copy(view = event.value)
      is InsightsEvent.Period -> state.value.copy(period = event.value)
      InsightsEvent.PreviousRequester -> state.value.copy(
        requesterPage = (state.value.requesterPage - 1).coerceAtLeast(0))
      InsightsEvent.NextRequester -> state.value.copy(requesterPage =
        (state.value.requesterPage + 1).coerceAtMost(state.value.requesters.data.orEmpty().lastIndex))
      else -> state.value
    }
  }

  private fun tab(tab: InsightsTab) {
    compose.onNodeWithTag("insights-tab-${tab.name}").performClick()
    assertSelection(tab)
  }

  private fun assertSelection(tab: InsightsTab, participant: String? = null) {
    compose.waitForIdle()
    compose.onNodeWithTag("insights-tab-${tab.name}").assertIsSelected()
    compose.runOnIdle {
      assertEquals(tab, state.value.tab)
      if (participant != null) assertEquals(participant, state.value.participant)
    }
  }

  private fun swipe(forward: Boolean = true) {
    compose.onNodeWithTag("insights-pager").performTouchInput {
      if (forward) swipeLeft() else swipeRight()
    }
    compose.waitForIdle()
  }

  private fun shortDrag() {
    compose.onNodeWithTag("insights-pager").performTouchInput {
      swipe(center, center.copy(x = center.x - width * 0.12f), durationMillis = 1_000)
    }
    compose.waitForIdle()
  }

  private fun readingPosition(): Float = compose.onNodeWithTag("insights-content")
    .fetchSemanticsNode().config[SemanticsProperties.VerticalScrollAxisRange].value()

  private fun readPartway(): Float {
    compose.onNodeWithTag("insights-content").performTouchInput {
      swipe(center, center.copy(y = center.y - 120f), durationMillis = 1_000)
    }
    compose.waitForIdle()
    return readingPosition().also { assertTrue("The fictional list must be scrollable", it > 0f) }
  }
}
