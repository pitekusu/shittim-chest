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
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.paging.Pager
import androidx.paging.PagingConfig
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
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.emitAll

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
    data object PauseSync : Event
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
    var syncRequest by remember { mutableStateOf<SessionState.SignedIn?>(null) }
    val syncScope = rememberCoroutineScope()
    LaunchedEffect(sessionState) {
      if (syncRequest != null && syncRequest !== sessionState) {
        syncRequest = null
        syncState = RecordSyncState.Paused
      }
      if (sessionState !is SessionState.SignedIn) syncState = RecordSyncState.Idle
    }
    // Lifecycle cancels directly at STOP, even when the stopped UI cannot recompose.
    LifecycleStartEffect(syncRequest) {
      val request = syncRequest
      val job = request?.let {
        syncScope.launch {
          try {
            val completed = session.withAuthorizedToken { token ->
              records?.synchronize(token, request.cacheAccountId)
                ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
              true
            }
            if (syncRequest === request && session.state.value === request) {
              syncRequest = null
              syncState = if (completed == true) RecordSyncState.Completed else RecordSyncState.Paused
              if (completed == true) listRetry++
            }
          } catch (error: CancellationException) { throw error }
          catch (error: RecordReadException) {
            if (syncRequest === request && session.state.value === request) {
              syncRequest = null
              syncState = RecordSyncState.Failed(error.failure)
              if (error.failure == RecordReadFailure.AUTH_REQUIRED) session.onForeground()
            }
          }
        }
      }
      onStopOrDispose {
        job?.cancel()
        if (request != null && syncRequest === request) {
          syncRequest = null
          syncState = RecordSyncState.Paused
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
          val loadedIds = ConcurrentHashMap.newKeySet<String>()
          val saved = try { records?.cachedRecords(cacheAccountId).orEmpty() }
          catch (error: RecordReadException) {
            if (error.failure == RecordReadFailure.AUTH_REQUIRED) return@LaunchedEffect
            emptyList()
          }
          loadedIds.addAll(saved.map { it.recordId })
          var source: Flow<PagingData<RecordListEntry>>? = null
          val pages = Pager(PagingConfig(pageSize = 12, initialLoadSize = 12,
            prefetchDistance = 3, enablePlaceholders = false)) {
            RecordPagingSource(
              loadPage = { cursor ->
                try {
                  val page = session.withAuthorizedToken { token ->
                    records?.recentRecords(token, currentSession.cacheAccountId, cursor)
                      ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
                  } ?: throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
                  if (cursor == null && listOwner === currentSession.user) {
                    recordList = RecordListState.Ready(checkNotNull(source), loadedIds)
                  }
                  page
                } catch (error: RecordReadException) {
                  if (cursor != null || error.failure !in setOf(RecordReadFailure.UNAVAILABLE,
                    RecordReadFailure.STORAGE_UNAVAILABLE)) throw error
                  val cached = records?.cachedRecords(cacheAccountId)
                    ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
                  if (listOwner === currentSession.user) recordList = RecordListState.Ready(
                    checkNotNull(source), loadedIds, saved = true, refreshFailure = error.failure)
                  RecordListPage(cached, null)
                }
              },
              onLoaded = { entries -> loadedIds.addAll(entries.map { it.recordId }) },
            )
          }
          source = flow {
            if (saved.isNotEmpty()) emit(PagingData.from(saved))
            emitAll(pages.flow)
          }
          recordList = RecordListState.Ready(source, loadedIds, saved = saved.isNotEmpty())
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
    LaunchedEffect(sessionState, selectedRecordId, recordRetry) {
      record = RecordPreviewState.Idle
      if (cacheAccountId != null && selectedRecordId != null) {
        record = RecordPreviewState.Loading
        record = try {
          val saved = records?.cachedRecord(cacheAccountId, selectedRecordId)
          if (saved != null) record = RecordPreviewState.Ready(saved, saved = true, updating = signedIn)
          if (!signedIn) {
            if (saved != null) RecordPreviewState.Ready(saved, saved = true) else RecordPreviewState.Empty
          } else try {
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
            if (saved != null && error.failure in setOf(RecordReadFailure.UNAVAILABLE,
              RecordReadFailure.STORAGE_UNAVAILABLE)) {
              RecordPreviewState.Ready(saved, saved = true, refreshFailure = error.failure)
            } else throw error
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
          if (!signedIn) session.retry()
        }
        BootstrapScreen.Event.RefreshRecords -> {
          listRetry++
          if (!signedIn) session.retry()
        }
        BootstrapScreen.Event.RecordsAuthRequired -> session.onForeground()
        BootstrapScreen.Event.StartSync -> if (currentSignedIn != null && syncRequest == null) {
          syncState = RecordSyncState.Running // Claim synchronously: double taps cannot start two jobs.
          syncRequest = currentSignedIn
        }
        BootstrapScreen.Event.PauseSync -> if (syncRequest != null) {
          syncRequest = null
          syncState = RecordSyncState.Paused
        }
        is BootstrapScreen.Event.OpenRecord -> if (
          event.recordId in ((visibleList as? RecordListState.Ready)?.loadedIds ?: emptySet())
        ) session.openDestination("/records/${event.recordId}")
        BootstrapScreen.Event.CloseRecord -> session.closeDestination()
      }
    }
  }
}
