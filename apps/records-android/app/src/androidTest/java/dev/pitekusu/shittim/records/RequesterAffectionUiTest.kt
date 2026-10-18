package dev.pitekusu.shittim.records

import android.graphics.Bitmap
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.width
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertContentDescriptionEquals
import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasTestTag
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.Density
import androidx.compose.ui.unit.dp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import dev.pitekusu.shittim.records.ui.ShittimTheme
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RequesterAffectionUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun threePersonaComparisonKeepsTiesExactScoresAndEachPersonasCrowns() {
    val original = requireNotNull(insightsSampleState().requesters.data).first().requesters.first()
    // Input order deliberately differs from score order. Two top scores share first place.
    val requester = original.copy(participants = original.participants.mapIndexed { index, person ->
      person.copy(score = if (index == 0) 0 else 1000, resetCount = index + 1)
    })
    show(requesterState(listOf(requester)))
    scrollToCard()
    val sorted = listOf(requester.participants[1], requester.participants[2], requester.participants[0])
    val positions = sorted.mapIndexed { index, person ->
      val node = compose.onNodeWithTag(personTag(person))
      node.assertIsDisplayed().assertContentDescriptionEquals(summary(person, if (index < 2) 1 else 3))
      node.fetchSemanticsNode().boundsInRoot.top
    }
    assertTrue(positions.zipWithNext().all { (first, second) -> first < second })
  }

  @Test fun narrowLargeTextPreservesLongNamesAndLargeCrownCounts() {
    val original = requireNotNull(insightsSampleState().requesters.data).first().requesters.first()
    val requester = original.copy(displayName = "架空のとても長い名前を使う依頼者01",
      participants = original.participants.mapIndexed { index, person ->
        person.copy(displayName = "とても長い名前の${person.displayName}",
          score = 1000 - index * 500, resetCount = 123456789 + index)
      })
    show(requesterState(listOf(requester)), width = 320, fontScale = 2f)
    scrollToCard()
    val nameLayouts = mutableListOf<TextLayoutResult>()
    compose.onNodeWithText(requester.displayName, useUnmergedTree = true)
      .performSemanticsAction(SemanticsActions.GetTextLayoutResult) { it(nameLayouts) }
    assertEquals(1, nameLayouts.size)
    assertFalse(nameLayouts.single().hasVisualOverflow)
    requester.participants.forEachIndexed { index, person ->
      compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag(personTag(person)))
      compose.onNodeWithTag(personTag(person)).assertIsDisplayed()
        .assertContentDescriptionEquals(summary(person, index + 1))
      val row = compose.onNodeWithTag(personTag(person)).fetchSemanticsNode().boundsInRoot
      val viewport = compose.onNodeWithTag("insights-content").fetchSemanticsNode().boundsInRoot
      assertTrue(row.width > 0 && row.left >= viewport.left && row.right <= viewport.right)
    }
  }

  @Test fun identicalRequesterNamesRemainSeparateCardsInTheWideComparison() {
    val original = requireNotNull(insightsSampleState().requesters.data).first().requesters.first()
    val other = original.copy(participants = original.participants.map { it.copy(score = 0, resetCount = 7) })
    show(requesterState(listOf(original, other)), width = 900)
    scrollToCard()
    compose.onAllNodesWithText(original.displayName).assertCountEquals(2)
    val cards = compose.onAllNodesWithTag("insights-requester-card").assertCountEquals(2)
    cards[0].assertIsDisplayed()
    cards[1].assertIsDisplayed()
    val left = cards[0].fetchSemanticsNode().boundsInRoot
    val right = cards[1].fetchSemanticsNode().boundsInRoot
    assertEquals(left.top, right.top, 1f)
    assertTrue(left.right <= right.left)
    val arona = compose.onAllNodesWithTag(personTag(original.participants.first()))
    arona[0].assertContentDescriptionEquals(summary(original.participants.first(), 1))
    arona[1].assertContentDescriptionEquals(summary(other.participants.first(), 1))
  }

  /** Opt-in visual evidence; normal CI performs no bitmap capture or image comparison. */
  @Test fun captureCompactRequesterLayouts() {
    if (InstrumentationRegistry.getArguments().getString("shittimCaptureRequester") != "true") return
    data class Layout(val name: String, val width: Int, val font: Float, val dark: Boolean)
    val layouts = listOf(Layout("light", 360, 1f, false), Layout("dark", 360, 1f, true),
      Layout("320dp-2x", 320, 2f, false), Layout("wide", 900, 1f, false))
    val requesters = requireNotNull(insightsSampleState().requesters.data).first().requesters
    layouts.forEach { layout ->
      show(requesterState(requesters), layout.width, layout.font, layout.dark)
      scrollToCard()
      val bitmap = compose.onRoot().captureToImage().asAndroidBitmap()
      File(requireNotNull(compose.activity.getExternalFilesDir(null)), "requester-compact-${layout.name}.png")
        .outputStream().use { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }
    }
  }

  private fun requesterState(requesters: List<InsightsRequester>): InsightsUiState {
    val sample = insightsSampleState()
    val page = requireNotNull(sample.requesters.data).first().copy(requesters = requesters, nextCursor = null)
    return sample.copy(view = InsightsAffectionView.Requesters, requesters = InsightsResource(listOf(page)))
  }

  private fun personTag(person: InsightsRequesterParticipant) = "insights-requester-person-${person.participant}"

  private fun summary(person: InsightsRequesterParticipant, rank: Int) = compose.activity.getString(
    R.string.insights_requester_person_summary, person.displayName, rank, person.score, person.score / 100,
    person.resetCount)

  private fun scrollToCard() {
    compose.onNodeWithTag("insights-content").performScrollToNode(hasTestTag("insights-requester-card"))
  }

  private fun show(state: InsightsUiState, width: Int = 360, fontScale: Float = 1f, dark: Boolean = false) {
    compose.activityRule.scenario.onActivity { activity ->
      activity.setContent {
        val density = LocalDensity.current
        val viewportDensity = density.density * minOf(1f, LocalConfiguration.current.screenWidthDp.toFloat() / width)
        CompositionLocalProvider(LocalDensity provides Density(viewportDensity, fontScale)) {
          ShittimTheme(dark) {
            Box(Modifier.width(width.dp).fillMaxHeight()) { InsightsScreen(state, {}) }
          }
        }
      }
    }
  }
}
