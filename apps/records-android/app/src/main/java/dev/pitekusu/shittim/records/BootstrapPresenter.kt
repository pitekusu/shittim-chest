package dev.pitekusu.shittim.records

import android.content.ActivityNotFoundException
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.paging.PagingData
import com.slack.circuit.runtime.CircuitUiEvent
import com.slack.circuit.runtime.CircuitUiState
import com.slack.circuit.runtime.presenter.Presenter
import com.slack.circuit.runtime.screen.Screen
import coil3.SingletonImageLoader
import dev.pitekusu.shittim.records.auth.MobileLoginContract
import dev.pitekusu.shittim.records.auth.MobileLoginStatus
import dev.pitekusu.shittim.records.auth.MobileLoginStep
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.MobileSessionUser
import dev.pitekusu.shittim.records.auth.SessionState
import dev.zacsweers.metro.Inject
import kotlinx.coroutines.flow.flowOf

internal enum class ThemeChoice {
  System,
  Light,
  Dark,
}

// A single fixed destination; no navigation stack or screen serialization is needed yet.
internal data object BootstrapScreen : Screen {
  class State(
    val themeChoice: ThemeChoice,
    val session: SessionState = SessionState.SignedOut(),
    val records: RecordListState = RecordListState.Idle,
    val record: RecordPreviewState = RecordPreviewState.Idle,
    val selectedRecordId: String? = null,
    val sync: RecordSyncState = RecordSyncState.Idle,
    val canReadRecords: Boolean = session is SessionState.SignedIn,
    val eventSink: (Event) -> Unit,
  ) : CircuitUiState

  sealed interface Event : CircuitUiEvent {
    data class SelectTheme(val choice: ThemeChoice) : Event
    data object Login : Event
    data object Logout : Event
    data object Retry : Event
    data object RetryRecord : Event
    data object RefreshRecords : Event
    data object RecordsAuthRequired : Event
    data object StartSync : Event
    data class OpenRecord(val recordId: String) : Event
    data object CloseRecord : Event
  }
}

@Inject
internal class BootstrapPresenter(
  private val session: MobileSessionModel,
) : Presenter<BootstrapScreen.State> {
  @Composable
  override fun present(): BootstrapScreen.State {
    // Restore the presentation state, without persisting a device/account preference.
    var themeChoice by rememberSaveable { mutableStateOf(ThemeChoice.System) }
    val sessionState by session.state.collectAsState()
    val destination by session.destination.collectAsState()
    val cacheAccountId = session.offlineCacheAccountId
    val context = LocalContext.current
    val signedIn = sessionState is SessionState.SignedIn
    DisposableEffect(signedIn) {
      // Discard presigned avatar bitmaps when authentication leaves this screen.
      onDispose { if (signedIn) SingletonImageLoader.get(context).memoryCache?.clear() }
    }
    val launcher = rememberLauncherForActivityResult(MobileLoginContract(), session::loginResult)
    val records = remember(context.applicationContext) {
      try {
        RecordsRepository.open(context.applicationContext, session::isCacheAuthorized)
      }
      catch (_: Exception) { null }
    }
    DisposableEffect(records) { onDispose { records?.close() } }
    // The preview belongs to this exact session, including after an account switch.
    var record by remember(sessionState) { mutableStateOf<RecordPreviewState>(RecordPreviewState.Idle) }
    var recordRetry by remember { mutableStateOf(0) }
    var listRetry by remember { mutableStateOf(0) }
    var recordList by remember { mutableStateOf<RecordListState>(RecordListState.Idle) }
    var listOwner by remember { mutableStateOf<MobileSessionUser?>(null) }
    var lastListRetry by remember { mutableStateOf(-1) }
    var syncState by remember { mutableStateOf<RecordSyncState>(RecordSyncState.Idle) }
    LaunchedEffect(cacheAccountId, signedIn) {
      if (cacheAccountId != null && signedIn) RecordSyncScheduler.schedule(context)
    }
    LaunchedEffect(cacheAccountId) {
      syncState = RecordSyncState.Idle
      if (cacheAccountId != null) {
        RecordSyncScheduler.states(context).collect { status ->
          syncState = status
          // Periodic work resets to ENQUEUED without retaining its result output.
          // Observe work/progress changes as well, including deletion-only completion.
          listRetry++
          if ((status as? RecordSyncState.Failed)?.reason == RecordReadFailure.AUTH_REQUIRED) {
            session.onForeground()
          }
        }
      }
    }
    // The session model owns the validated destination across foreground checks and rotation.
    val selectedRecordId = destination.takeIf { cacheAccountId != null && it.startsWith("/records/") }
      ?.removePrefix("/records/")
    LaunchedEffect(sessionState, listRetry) {
      val currentSession = sessionState
      if (cacheAccountId == null) {
        if (currentSession != SessionState.Checking) {
          listOwner = null
          recordList = RecordListState.Idle
        }
        return@LaunchedEffect
      }
      when (currentSession) {
        is SessionState.SignedIn -> {
          // Keep loaded pages while navigating to a detail; replace them on account change.
          if (lastListRetry == listRetry && listOwner === currentSession.user && recordList is RecordListState.Ready) {
            return@LaunchedEffect
          }
          listOwner = currentSession.user
          lastListRetry = listRetry
          val saved = try { records?.cachedRecords(cacheAccountId)
            ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
          catch (error: RecordReadException) {
            if (error.failure == RecordReadFailure.AUTH_REQUIRED) return@LaunchedEffect
            recordList = RecordListState.Error(error.failure)
            return@LaunchedEffect
          }
          // No online paging/detail refresh on ordinary navigation: the worker alone
          // reconciles revisions, while the UI reads decrypted local data immediately.
          recordList = if (saved.isEmpty() && syncState is RecordSyncState.Failed) {
            RecordListState.Error((syncState as RecordSyncState.Failed).reason)
          } else if (saved.isEmpty() && syncState != RecordSyncState.Completed) RecordListState.Idle
          else RecordListState.Ready(flowOf(PagingData.from(saved)), saved.map { it.recordId }.toSet(), saved = true)
        }
        SessionState.Unavailable -> {
          listOwner = null
          recordList = try {
            val saved = records?.cachedRecords(cacheAccountId)
              ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
            RecordListState.Ready(flowOf(PagingData.from(saved)), saved.map { it.recordId }.toSet(), saved = true)
          } catch (error: RecordReadException) { RecordListState.Error(error.failure) }
        }
        is SessionState.SignedOut, SessionState.SigningOut, SessionState.Browser,
        SessionState.StorageError -> {
          listOwner = null
          recordList = RecordListState.Idle
        }
        SessionState.Checking -> Unit
      }
    }
    LaunchedEffect(sessionState, selectedRecordId, recordRetry, listRetry) {
      record = RecordPreviewState.Idle
      if (cacheAccountId != null && selectedRecordId != null) {
        record = RecordPreviewState.Loading
        record = try {
          val saved = records?.cachedRecord(cacheAccountId, selectedRecordId)
          if (saved != null) RecordPreviewState.Ready(saved, saved = true)
          else if (!signedIn) RecordPreviewState.Empty
          else try {
            val result = session.withAuthorizedToken {
              records?.record(it, cacheAccountId, selectedRecordId)
                ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
            }
            when (result) {
              null -> RecordPreviewState.Idle
              RecordReadResult.Empty -> RecordPreviewState.Error(RecordReadFailure.INVALID_RESPONSE)
              is RecordReadResult.Found -> RecordPreviewState.Ready(result.preview)
            }
          } catch (error: RecordReadException) {
            if (error.failure == RecordReadFailure.NOT_FOUND) listRetry++
            throw error
          }
        } catch (error: RecordReadException) {
          if (error.failure == RecordReadFailure.AUTH_REQUIRED) session.onForeground()
          RecordPreviewState.Error(error.failure)
        }
      }
    }
    val currentSignedIn = sessionState as? SessionState.SignedIn
    val visibleList = if (cacheAccountId != null && (currentSignedIn == null ||
      listOwner === currentSignedIn.user)) recordList else RecordListState.Idle
    return BootstrapScreen.State(themeChoice, sessionState, visibleList, record, selectedRecordId, syncState,
      canReadRecords = cacheAccountId != null) { event ->
      when (event) {
        is BootstrapScreen.Event.SelectTheme -> themeChoice = event.choice
        BootstrapScreen.Event.Login -> if (session.beginLogin()) {
          try { launcher.launch(session.loginDestination) }
          catch (_: ActivityNotFoundException) {
            session.loginResult(MobileLoginStep.Finished(MobileLoginStatus.BROWSER_UNAVAILABLE))
          }
        }
        BootstrapScreen.Event.Logout -> session.logout()
        BootstrapScreen.Event.Retry -> session.retry()
        BootstrapScreen.Event.RetryRecord -> {
          recordRetry++
          if (!signedIn) session.retry() else RecordSyncScheduler.syncNow(context)
        }
        BootstrapScreen.Event.RefreshRecords -> {
          listRetry++
          if (!signedIn) session.retry() else RecordSyncScheduler.syncNow(context)
        }
        BootstrapScreen.Event.RecordsAuthRequired -> session.onForeground()
        BootstrapScreen.Event.StartSync -> if (currentSignedIn != null) RecordSyncScheduler.syncNow(context)
        is BootstrapScreen.Event.OpenRecord -> if (
          event.recordId in ((visibleList as? RecordListState.Ready)?.loadedIds ?: emptySet())
        ) session.openDestination("/records/${event.recordId}")
        BootstrapScreen.Event.CloseRecord -> session.closeDestination()
      }
    }
  }
}
