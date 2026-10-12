package dev.pitekusu.shittim.records

import androidx.activity.compose.setContent
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToKey
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class RecordsPublicLinksUiTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  @Test fun loginFooterMakesBothPublicPagesAvailableBeforeAuthentication() {
    val events = mutableListOf<BootstrapScreen.Event>()
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      BootstrapUi(BootstrapScreen.State(ThemeChoice.System, eventSink = events::add))
    } }
    compose.onNodeWithTag("bootstrap-content").performScrollToKey("public-information")
    RecordsPublicPage.entries.forEach { page ->
      compose.onNodeWithTag("public-page-${page.name}").performScrollTo().assertIsDisplayed().performClick()
    }
    compose.runOnIdle { assertEquals(RecordsPublicPage.entries.map { BootstrapScreen.Event.OpenPublicPage(it) }, events) }
  }

  @Test fun normalAndDemoMenusOfferOnlyThePublicPagesAndKeepFeatureWebLinksRemoved() {
    for (demo in listOf(false, true)) {
      val events = mutableListOf<BootstrapScreen.Event>()
      compose.activityRule.scenario.onActivity { activity -> activity.setContent {
        BootstrapUi(BootstrapScreen.State(ThemeChoice.System,
          records = RecordListState.Ready.fromSaved(emptyList()), canReadRecords = true,
          demoEntryId = "11111111-2222-4333-8444-abcdefabcdef".takeIf { demo }, eventSink = events::add))
      } }
      compose.onNodeWithTag("records-menu-open").performClick()
      RecordsPublicPage.entries.forEach { page ->
        compose.onNodeWithTag("public-page-${page.name}").performScrollTo().assertIsDisplayed().performClick()
      }
      for (label in listOf(R.string.menu_insights, R.string.menu_momotalk, R.string.menu_memorial,
        R.string.menu_status, R.string.menu_prompts)) {
        compose.onNodeWithText(compose.activity.getString(label)).assertDoesNotExist()
      }
      compose.runOnIdle { assertEquals(RecordsPublicPage.entries.map { BootstrapScreen.Event.OpenPublicPage(it) }, events) }
    }
  }
}
