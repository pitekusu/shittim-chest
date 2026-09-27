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
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.platform.LocalContext
import com.slack.circuit.runtime.CircuitUiEvent
import com.slack.circuit.runtime.CircuitUiState
import com.slack.circuit.runtime.presenter.Presenter
import com.slack.circuit.runtime.screen.Screen
import coil3.SingletonImageLoader
import dev.pitekusu.shittim.records.auth.MobileLoginContract
import dev.pitekusu.shittim.records.auth.MobileLoginStatus
import dev.pitekusu.shittim.records.auth.MobileLoginStep
import dev.pitekusu.shittim.records.auth.MobileSessionModel
import dev.pitekusu.shittim.records.auth.SessionState
import dev.zacsweers.metro.Inject
import kotlinx.coroutines.flow.conflate
import kotlinx.coroutines.delay

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
    val listQuery: RecordListQuery = RecordListQuery(),
    val searching: Boolean = false,
    val eventSink: (Event) -> Unit,
  ) : CircuitUiState

  sealed interface Event : CircuitUiEvent {
    data class SelectTheme(val choice: ThemeChoice) : Event
    data object Login : Event
    data object Logout : Event
    data object Retry : Event
    data object RetryRecord : Event
    data object RecordsAuthRequired : Event
    data class OpenRecord(val recordId: String) : Event
    data object CloseRecord : Event
    data class SearchRecords(val text: String) : Event
    data class SelectWinner(val winner: RecordWinner) : Event
    data class SelectOrder(val order: RecordOrder) : Event
    data object ClearRecordQuery : Event
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
    val cachePermit by session.cachePermit.collectAsState()
    val destination by session.destination.collectAsState()
    val cacheAccountId = cachePermit?.accountId?.takeIf(session::isCacheAuthorized)
    val context = LocalContext.current
    val signedIn = sessionState is SessionState.SignedIn
    val currentSignedIn by rememberUpdatedState(signedIn)
    val currentSessionState by rememberUpdatedState(sessionState)
    DisposableEffect(cacheAccountId) {
      // Restored offline access can show decoded avatars before SignedIn exists.
      onDispose { if (cacheAccountId != null) SingletonImageLoader.get(context).memoryCache?.clear() }
    }
    val launcher = rememberLauncherForActivityResult(MobileLoginContract(), session::loginResult)
    val records = remember(context.applicationContext) {
      try {
        RecordsRepository.open(context.applicationContext, session::isCacheAuthorized)
      }
      catch (_: Exception) { null }
    }
    DisposableEffect(records) { onDispose { records?.close() } }
    // Keep the same account's saved preview during a foreground session check.
    // Permission loss, account change, or navigation drops it immediately.
    var record by remember(cacheAccountId, destination) { mutableStateOf<RecordPreviewState>(RecordPreviewState.Idle) }
    var recordRetry by remember { mutableStateOf(0) }
    var listRetry by remember { mutableStateOf(0) }
    var recordList by remember { mutableStateOf<RecordListState>(RecordListState.Idle) }
    // Search text and decrypted snapshots are account-bound memory, never SavedState.
    var listQuery by remember(cacheAccountId) { mutableStateOf(RecordListQuery()) }
    var savedEntries by remember(cacheAccountId) { mutableStateOf<List<RecordListEntry>?>(null) }
    var searching by remember(cacheAccountId) { mutableStateOf(false) }
    var listOwner by remember { mutableStateOf<String?>(null) }
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
            session.onSyncAuthenticationRequired()
          }
        }
      }
    }
    // The session model owns the validated destination across foreground checks and rotation.
    val selectedRecordId = destination.takeIf { cacheAccountId != null && it.startsWith("/records/") }
      ?.removePrefix("/records/")
    LaunchedEffect(cacheAccountId) {
      if (cacheAccountId == null) {
        listOwner = null
        recordList = RecordListState.Idle
        return@LaunchedEffect
      }
      if (listOwner != cacheAccountId) recordList = RecordListState.Idle
      listOwner = cacheAccountId
      // Finish the current local read; progress only queues the latest reload.
      // collectLatest / effect keys based on progress would starve a slow decrypted read.
      snapshotFlow { listRetry to (currentSessionState == SessionState.Unavailable) }.conflate().collect {
        val saved = try { records?.cachedRecords(cacheAccountId)
          ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE) }
        catch (error: RecordReadException) {
          if (session.isCacheAuthorized(cacheAccountId)) recordList = RecordListState.Error(error.failure)
          return@collect
        }
        if (!session.isCacheAuthorized(cacheAccountId)) return@collect
        savedEntries = saved
      }
    }
    LaunchedEffect(cacheAccountId, listQuery) {
      if (cacheAccountId == null) return@LaunchedEffect
      val query = listQuery
      searching = query.searchesText
      try {
        if (query.searchesText) delay(250) // Coalesce typing without any network request.
        // Progress may queue a new snapshot, but must not continually cancel a decrypted search.
        snapshotFlow { savedEntries to syncState }.conflate().collect { (saved, status) ->
          if (saved == null) return@collect
          searching = query.searchesText
          val selected = try {
            records?.queryCachedRecords(cacheAccountId, saved, query)
              ?: throw RecordReadException(RecordReadFailure.STORAGE_UNAVAILABLE)
          } catch (error: RecordReadException) {
            if (session.isCacheAuthorized(cacheAccountId)) recordList = RecordListState.Error(error.failure)
            searching = false
            return@collect
          }
          if (!session.isCacheAuthorized(cacheAccountId)) return@collect
          recordList = if (saved.isEmpty() && query.isDefault &&
            currentSessionState != SessionState.Unavailable && status is RecordSyncState.Failed) {
            RecordListState.Error(status.reason)
          } else if (saved.isEmpty() && query.isDefault &&
            currentSessionState != SessionState.Unavailable && status != RecordSyncState.Completed) RecordListState.Idle
          else RecordListState.Ready.fromSaved(selected, recordList as? RecordListState.Ready, saved.size)
          searching = false
        }
      } finally { searching = false }
    }
    LaunchedEffect(cacheAccountId, selectedRecordId) {
      if (cacheAccountId != null && selectedRecordId != null) {
        snapshotFlow { Triple(recordRetry, listRetry, currentSignedIn) }.conflate().collect {
          if (record !is RecordPreviewState.Ready) record = RecordPreviewState.Loading
          val loaded = try {
            val saved = records?.cachedRecord(cacheAccountId, selectedRecordId)
            if (saved != null) RecordPreviewState.Ready(saved, saved = true)
            else if (!currentSignedIn) RecordPreviewState.Empty
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
            if (error.failure == RecordReadFailure.AUTH_REQUIRED) session.onAuthenticationRequired()
            RecordPreviewState.Error(error.failure)
          }
          if (session.isCacheAuthorized(cacheAccountId)) record = loaded
        }
      }
    }
    val visibleList = if (cacheAccountId != null && listOwner == cacheAccountId) recordList else RecordListState.Idle
    return BootstrapScreen.State(themeChoice, sessionState, visibleList, record, selectedRecordId, syncState,
      canReadRecords = cacheAccountId != null, listQuery = listQuery, searching = searching) { event ->
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
        BootstrapScreen.Event.RecordsAuthRequired -> session.onAuthenticationRequired()
        is BootstrapScreen.Event.OpenRecord -> if (
          event.recordId in ((visibleList as? RecordListState.Ready)?.loadedIds ?: emptySet())
        ) session.openDestination("/records/${event.recordId}")
        BootstrapScreen.Event.CloseRecord -> session.closeDestination()
        is BootstrapScreen.Event.SearchRecords -> listQuery = listQuery.copy(text = event.text)
        is BootstrapScreen.Event.SelectWinner -> listQuery = listQuery.copy(winner = event.winner)
        is BootstrapScreen.Event.SelectOrder -> listQuery = listQuery.copy(order = event.order)
        BootstrapScreen.Event.ClearRecordQuery -> listQuery = RecordListQuery()
      }
    }
  }
}
