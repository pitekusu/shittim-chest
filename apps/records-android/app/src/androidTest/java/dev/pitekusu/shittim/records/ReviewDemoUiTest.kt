package dev.pitekusu.shittim.records

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithTag
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import dev.pitekusu.shittim.records.auth.KeystoreTokenStore
import dev.pitekusu.shittim.records.storage.RecordCacheAccount
import dev.pitekusu.shittim.records.storage.StorageNamespace
import kotlinx.coroutines.runBlocking
import org.junit.Rule
import org.junit.Test
import org.junit.rules.ExternalResource
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
@ScreenTest
class ReviewDemoUiTest {
  @get:Rule(order = 0) val isolatedData = object : ExternalResource() {
    override fun before() = resetStorage()
    override fun after() = resetStorage()
  }
  @get:Rule(order = 1) val compose = createAndroidComposeRule<MainActivity>()

  @Test fun reviewerCanBrowseImagesAndCreateResultAfterLeavingProgress() {
    enter()
    val question = DemoFixtures.records.first().entry.questionPreview
    compose.onNodeWithText(question).performScrollTo().performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("detail-images-open").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("detail-images-open").performClick()
    compose.onNodeWithTag("attachment-viewer").assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    back()
    compose.onNodeWithTag("debate-compose-open").performClick()
    val input = "架空の審査用相談：紙で小さな庭を作る手順を考えてください。"
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("debate-question").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("debate-question").performTextInput(input)
    compose.onNodeWithTag("review-demo-sample-image").performScrollTo().performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("attachment-0").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("debate-submit").performScrollTo().performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("debate-status").fetchSemanticsNodes().isNotEmpty() }
    back()
    // No progress screen polling: the retained local sandbox must still publish a record.
    compose.waitUntil(20_000) { compose.onAllNodesWithText(input).fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithText(input).performScrollTo().performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("detail-images-open").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("detail-images-open").performClick()
    compose.onNodeWithTag("attachment-viewer").assertIsDisplayed()
    compose.onNodeWithTag("attachment-close").performClick()
    compose.onNodeWithTag("review-demo-banner").assertIsDisplayed()
  }

  @Test fun draftSurvivesRecreationAndResetExitReturnToReusableEntry() {
    enter()
    compose.onNodeWithTag("debate-compose-open").performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("debate-question").fetchSemanticsNodes().isNotEmpty() }
    val draft = "架空の保存確認：絵本を読む順番を考えてください。"
    compose.onNodeWithTag("debate-question").performTextInput(draft)
    compose.onNodeWithTag("review-demo-sample-image").performScrollTo().performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("attachment-0").fetchSemanticsNodes().isNotEmpty() }
    compose.activityRule.scenario.recreate()
    compose.onNodeWithTag("debate-question").assertTextContains(draft)
    compose.onNodeWithTag("records-menu-open").performClick()
    compose.onNodeWithTag("review-demo-reset").performScrollTo().performClick()
    compose.onNodeWithTag("review-demo-confirm").performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-demo-banner").fetchSemanticsNodes().isNotEmpty() }
    compose.onAllNodesWithText(draft).fetchSemanticsNodes().let { check(it.isEmpty()) }
    compose.onNodeWithTag("records-menu-open").performClick()
    compose.onNodeWithTag("review-demo-exit").performScrollTo().performClick()
    compose.onNodeWithTag("review-demo-confirm").performClick()
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-demo-enter").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("review-demo-banner").assertDoesNotExist()
    enter()
  }

  private fun enter() {
    compose.waitUntil(10_000) { compose.onAllNodesWithTag("review-demo-enter").fetchSemanticsNodes().isNotEmpty() }
    compose.onNodeWithTag("review-demo-enter").performScrollTo().performClick()
    compose.waitUntil(15_000) { compose.onAllNodesWithTag("review-demo-banner").fetchSemanticsNodes().isNotEmpty() }
  }

  private fun back() = compose.runOnUiThread { compose.activity.onBackPressedDispatcher.onBackPressed() }

  private fun resetStorage() = runBlocking {
    val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    RecordSyncScheduler.cancelAndAwait(context)
    RecordNotificationRegistration.cancelAndAwait(context)
    val store = KeystoreTokenStore(context)
    store.beginLogout()
    RecordCacheAccount.clear(context)
    store.clear()
    store.completeLogout()
    RecordCacheAccount.clear(context, StorageNamespace.DEMO)
    DemoNotifications.clear(context)
    ReviewDemoModeStore(context).write(ReviewDemoMode(ReviewDemoPhase.NORMAL))
  }
}
