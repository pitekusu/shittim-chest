package dev.pitekusu.shittim.records

import android.app.Activity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.material3.Snackbar
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LifecycleResumeEffect
import com.google.android.play.core.appupdate.AppUpdateInfo
import com.google.android.play.core.appupdate.AppUpdateManager
import com.google.android.play.core.appupdate.AppUpdateManagerFactory
import com.google.android.play.core.appupdate.AppUpdateOptions
import com.google.android.play.core.install.InstallStateUpdatedListener
import com.google.android.play.core.install.model.AppUpdateType
import com.google.android.play.core.install.model.InstallStatus
import com.google.android.play.core.install.model.UpdateAvailability
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.tasks.await

/** Play owns eligibility, consent, download and installation; never download an APK ourselves. */
@Composable
internal fun PlayUpdateNotice(manager: AppUpdateManager? = null) {
  val context = LocalContext.current.applicationContext
  val updates = manager ?: remember(context) { AppUpdateManagerFactory.create(context) }
  val scope = rememberCoroutineScope()
  var version by remember { mutableIntStateOf(0) }
  var deferredVersion by rememberSaveable { mutableIntStateOf(0) }
  var requestedVersion by rememberSaveable { mutableIntStateOf(0) }
  var consentPending by rememberSaveable { mutableStateOf(false) }
  var status by remember { mutableIntStateOf(InstallStatus.UNKNOWN) }
  var available by remember { mutableStateOf(false) }
  var busy by remember { mutableStateOf(false) }
  var failed by remember { mutableStateOf(false) }
  var resumed by remember { mutableStateOf(false) }
  var installEvents by remember { mutableIntStateOf(0) }
  fun applyInfo(info: AppUpdateInfo) {
    val availability = info.updateAvailability()
    version = if (availability == UpdateAvailability.UPDATE_AVAILABLE ||
      availability == UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) info.availableVersionCode() else 0
    // Play defines installStatus only while an update is actually in progress.
    status = if (availability == UpdateAvailability.DEVELOPER_TRIGGERED_UPDATE_IN_PROGRESS) {
      requestedVersion = version
      info.installStatus()
    } else InstallStatus.UNKNOWN
    if (status != InstallStatus.UNKNOWN) {
      consentPending = false
      failed = status == InstallStatus.FAILED
    }
    available = availability == UpdateAvailability.UPDATE_AVAILABLE &&
      info.isUpdateTypeAllowed(AppUpdateType.FLEXIBLE)
  }
  val launcher = rememberLauncherForActivityResult(ActivityResultContracts.StartIntentSenderForResult()) { result ->
    busy = false
    consentPending = false
    // RESULT_OK is consent, not a completed install. Download completion comes from Play.
    if (result.resultCode != Activity.RESULT_OK) {
      deferredVersion = requestedVersion
      failed = result.resultCode != Activity.RESULT_CANCELED
    } else if (status == InstallStatus.UNKNOWN || status == InstallStatus.FAILED) {
      status = InstallStatus.PENDING
    }
  }

  DisposableEffect(updates) {
    // The SDK can finish, fail or cancel a download while this Activity is paused.
    val listener = InstallStateUpdatedListener { install ->
      installEvents++
      status = install.installStatus()
      busy = false
      consentPending = false
      failed = status == InstallStatus.FAILED
      if (status == InstallStatus.CANCELED) {
        deferredVersion = requestedVersion.takeIf { it > 0 } ?: version
      }
    }
    updates.registerListener(listener)
    onDispose { updates.unregisterListener(listener) }
  }

  LifecycleResumeEffect(updates) {
    resumed = true
    val check = scope.launch {
      try {
        val observedEvents = installEvents
        val info = updates.appUpdateInfo.await()
        // A delayed query cannot overwrite a newer install event (especially DOWNLOADED).
        if (observedEvents == installEvents) applyInfo(info)
      } catch (cancelled: CancellationException) {
        throw cancelled
      } catch (_: Exception) {
        // Offline, unsupported/debug installs or Play unavailable must not block local records.
        available = false
      }
    }
    onPauseOrDispose {
      resumed = false
      check.cancel()
    }
  }

  val downloading = status == InstallStatus.PENDING || status == InstallStatus.DOWNLOADING
  val downloaded = status == InstallStatus.DOWNLOADED
  // Play-side cancellation can happen while our listener is paused; do not re-offer that request.
  val offered = available && version != deferredVersion &&
    (version != requestedVersion || consentPending)
  if (!downloaded && !downloading && !offered && !failed) return

  // An overlay leaves the journal's size and scroll anchor unchanged as the notice comes and goes.
  Box(Modifier.fillMaxSize().safeDrawingPadding().padding(16.dp), contentAlignment = Alignment.BottomCenter) {
    Snackbar(
      Modifier.widthIn(max = 560.dp).fillMaxWidth().testTag("play-update-notice"),
      actionOnNewLine = true,
      action = {
        if (!downloading) TextButton(enabled = resumed && !busy && !consentPending, onClick = {
          if (!resumed || busy || consentPending) return@TextButton
          busy = true
          failed = false
          scope.launch {
            try {
              if (downloaded) {
                // Never restart the app (including an unsent draft) without this explicit action.
                updates.completeUpdate().await()
                busy = false
              } else {
                // AppUpdateInfo is one-shot. Always obtain a new instance just before launching.
                val observedEvents = installEvents
                val info = updates.appUpdateInfo.await()
                if (!resumed || observedEvents != installEvents) {
                  busy = false
                  return@launch
                }
                applyInfo(info)
                if (available) {
                  requestedVersion = version
                  consentPending = true
                  if (!updates.startUpdateFlowForResult(info, launcher,
                      AppUpdateOptions.newBuilder(AppUpdateType.FLEXIBLE).build())) {
                    busy = false
                    consentPending = false
                    failed = true
                  }
                } else busy = false
              }
            } catch (cancelled: CancellationException) {
              busy = false
              throw cancelled
            } catch (_: Exception) {
              busy = false
              consentPending = false
              failed = true
            }
          }
        }) { Text(stringResource(if (downloaded) R.string.play_update_restart else R.string.play_update_start)) }
      },
      dismissAction = {
        TextButton(enabled = !busy && !consentPending, onClick = {
          deferredVersion = version
          failed = false
          // A completed download stays in Play; remind on a later foreground visit, not immediately.
          status = InstallStatus.UNKNOWN
          available = false
        }) { Text(stringResource(R.string.play_update_later)) }
      },
    ) {
      Text(stringResource(when {
        failed -> R.string.play_update_failed
        downloaded -> R.string.play_update_downloaded
        downloading -> R.string.play_update_downloading
        else -> R.string.play_update_available
      }))
    }
  }
}
