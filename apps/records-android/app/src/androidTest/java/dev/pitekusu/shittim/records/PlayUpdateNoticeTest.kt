package dev.pitekusu.shittim.records

import android.app.Activity
import android.app.PendingIntent
import android.content.Intent
import android.view.ViewGroup
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.activity.compose.setContent
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.ActivityResultRegistryOwner
import androidx.activity.result.IntentSenderRequest
import androidx.activity.result.contract.ActivityResultContract
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Text
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.test.assertIsNotEnabled
import androidx.compose.ui.test.junit4.StateRestorationTester
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.ComposeView
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.lifecycle.Lifecycle
import androidx.core.app.ActivityOptionsCompat
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.google.android.play.core.appupdate.testing.FakeAppUpdateManager
import com.google.android.play.core.appupdate.AppUpdateInfo
import com.google.android.play.core.appupdate.AppUpdateManager
import com.google.android.play.core.appupdate.AppUpdateOptions
import com.google.android.gms.tasks.TaskCompletionSource
import com.google.android.play.core.install.model.AppUpdateType
import com.google.android.play.core.install.model.InstallErrorCode
import dev.pitekusu.shittim.records.ui.ShittimTheme
import org.junit.Assert.assertFalse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/** Official Play fake only: no network, credentials, downloaded APK or actual installation. */
@RunWith(AndroidJUnit4::class)
class PlayUpdateNoticeTest {
  @get:Rule val compose = createAndroidComposeRule<MainActivity>()

  private fun show(manager: AppUpdateManager) {
    compose.activityRule.scenario.onActivity { activity -> activity.setContent {
      ShittimTheme(true) {
        Box(Modifier.fillMaxSize()) {
          Text("架空の記録")
          PlayUpdateNotice(manager)
        }
      }
    } }
    compose.waitForIdle()
  }

  private fun text(resource: Int) = compose.onNodeWithText(compose.activity.getString(resource))

  @Test fun noticeRequiresExplicitConsentAndCompletedDownloadRequiresExplicitRestart() {
    val manager = FakeAppUpdateManager(compose.activity).apply {
      setUpdateAvailable(34, AppUpdateType.FLEXIBLE)
    }
    show(manager)
    text(R.string.play_update_available).assertIsDisplayed()
    assertFalse(manager.isConfirmationDialogVisible)
    text(R.string.play_update_start).performClick()
    compose.runOnIdle {
      assertTrue(manager.isConfirmationDialogVisible)
      assertFalse(manager.isImmediateFlowVisible)
      manager.userAcceptsUpdate()
      manager.downloadStarts()
    }
    text(R.string.play_update_downloading).assertIsDisplayed()
    compose.onNodeWithText("架空の記録").assertIsDisplayed()
    compose.runOnIdle { manager.downloadCompletes() }
    text(R.string.play_update_downloaded).assertIsDisplayed()
    assertFalse(manager.isInstallSplashScreenVisible)
    text(R.string.play_update_restart).performClick()
    compose.runOnIdle { assertTrue(manager.isInstallSplashScreenVisible) }
  }

  @Test fun deferralSurvivesForegroundVisitsAndANewerVersionCanBeOffered() {
    val manager = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    show(manager)
    text(R.string.play_update_later).performClick()
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.runOnIdle { manager.setUpdateAvailable(35) }
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    text(R.string.play_update_available).assertIsDisplayed()
    assertFalse(manager.isConfirmationDialogVisible)
  }

  @Test fun downloadCompletedWhileBackgroundedIsRecoveredOnResume() {
    val manager = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    show(manager)
    text(R.string.play_update_start).performClick()
    compose.runOnIdle { manager.userAcceptsUpdate(); manager.downloadStarts() }
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.runOnUiThread { manager.downloadCompletes() }
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    text(R.string.play_update_downloaded).assertIsDisplayed()
    assertFalse(manager.isInstallSplashScreenVisible)
  }

  @Test fun noFlexibleUpdateOrPlayFailureDoesNotHideRecordsOrLaunchAnUpdate() {
    val manager = FakeAppUpdateManager(compose.activity).apply {
      setUpdateAvailable(34, AppUpdateType.IMMEDIATE)
    }
    show(manager)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.onNodeWithText("架空の記録").assertIsDisplayed()
    compose.runOnIdle { manager.setInstallErrorCode(InstallErrorCode.ERROR_API_NOT_AVAILABLE) }
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.onNodeWithText("架空の記録").assertIsDisplayed()
    assertFalse(manager.isImmediateFlowVisible)
  }

  @Test fun consentCannotBeLaunchedTwiceAfterRestorationAndCancellationDefersTheVersion() {
    val fake = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    var launches = 0
    var requestCode = 0
    val registry = object : ActivityResultRegistry() {
      override fun <I, O> onLaunch(code: Int, contract: ActivityResultContract<I, O>, input: I,
        options: ActivityOptionsCompat?) { requestCode = code }
    }
    val owner = object : ActivityResultRegistryOwner { override val activityResultRegistry = registry }
    val manager = object : AppUpdateManager by fake {
      override fun startUpdateFlowForResult(info: AppUpdateInfo, launcher: ActivityResultLauncher<IntentSenderRequest>,
        options: AppUpdateOptions): Boolean {
        launches++
        val started = fake.startUpdateFlowForResult(info, launcher, options)
        // The Play fake does not deliver Activity Results. Use the standard registry test seam.
        val intent = PendingIntent.getActivity(compose.activity, 34,
          Intent(compose.activity, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        launcher.launch(IntentSenderRequest.Builder(intent.intentSender).build())
        return started
      }
    }
    val restoration = StateRestorationTester(compose)
    compose.activityRule.scenario.onActivity { activity ->
      val host = activity.findViewById<ViewGroup>(android.R.id.content)
      (host.getChildAt(0) as? ComposeView)?.disposeComposition()
      host.removeAllViews()
    }
    restoration.setContent {
      CompositionLocalProvider(LocalActivityResultRegistryOwner provides owner) {
        ShittimTheme(true) { PlayUpdateNotice(manager) }
      }
    }
    text(R.string.play_update_start).performClick()
    text(R.string.play_update_start).assertIsNotEnabled()
    restoration.emulateSavedInstanceStateRestore()
    text(R.string.play_update_start).assertIsNotEnabled()
    compose.runOnIdle {
      assertEquals(1, launches)
      fake.userRejectsUpdate()
      registry.dispatchResult(requestCode, Activity.RESULT_CANCELED, null)
    }
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
  }

  @Test fun lateForegroundQueryCannotOverwriteACompletedDownloadEvent() {
    val fake = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    compose.runOnIdle {
      fake.startUpdateFlowForResult(fake.appUpdateInfo.result, AppUpdateType.FLEXIBLE, compose.activity, 34)
      fake.userAcceptsUpdate()
      fake.downloadStarts()
    }
    val pendingInfo = fake.appUpdateInfo.result
    val pendingQuery = TaskCompletionSource<AppUpdateInfo>()
    val manager = object : AppUpdateManager by fake {
      override fun getAppUpdateInfo() = pendingQuery.task
    }
    show(manager)
    compose.runOnIdle {
      fake.downloadCompletes()
      pendingQuery.setResult(pendingInfo)
    }
    text(R.string.play_update_downloaded).assertIsDisplayed()
    assertFalse(fake.isInstallSplashScreenVisible)
  }

  @Test fun noUpdateIgnoresAStaleDownloadedStatusAndNeverOffersRestart() {
    val manager = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    compose.runOnIdle {
      manager.startUpdateFlowForResult(manager.appUpdateInfo.result, AppUpdateType.FLEXIBLE, compose.activity, 34)
      manager.userAcceptsUpdate()
      manager.downloadStarts()
      manager.downloadCompletes()
      // The SDK fake retains installStatus even though it no longer has a defined meaning.
      manager.setUpdateNotAvailable()
    }
    show(manager)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.onNodeWithText("架空の記録").assertIsDisplayed()
    assertFalse(manager.isInstallSplashScreenVisible)
  }

  @Test fun cancelingAFreshlyDiscoveredReleaseDefersThatReleaseImmediatelyAndOnResume() {
    val manager = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    show(manager)
    // A release can arrive between the initial query and the explicit update action.
    compose.runOnIdle { manager.setUpdateAvailable(35) }
    text(R.string.play_update_start).performClick()
    compose.runOnIdle {
      manager.userAcceptsUpdate()
      manager.downloadStarts()
      manager.userCancelsDownload()
    }
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.runOnIdle { manager.setUpdateAvailable(36) }
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    text(R.string.play_update_available).assertIsDisplayed()
  }

  @Test fun cancelingANewerResumedDownloadDoesNotReuseThePreviousRequestedVersion() {
    val manager = FakeAppUpdateManager(compose.activity).apply { setUpdateAvailable(34) }
    show(manager)
    text(R.string.play_update_start).performClick()
    compose.runOnIdle {
      manager.userAcceptsUpdate()
      manager.downloadStarts()
      manager.userCancelsDownload()
    }
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.runOnUiThread {
      manager.setUpdateAvailable(35)
      manager.startUpdateFlowForResult(manager.appUpdateInfo.result, AppUpdateType.FLEXIBLE, compose.activity, 35)
      manager.userAcceptsUpdate()
      manager.downloadStarts()
    }
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    text(R.string.play_update_downloading).assertIsDisplayed()
    compose.runOnIdle { manager.userCancelsDownload() }
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
    compose.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
    compose.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
    compose.onNodeWithTag("play-update-notice").assertDoesNotExist()
  }
}
