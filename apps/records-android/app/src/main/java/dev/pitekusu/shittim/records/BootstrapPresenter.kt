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
import androidx.navigation3.runtime.rememberNavBackStack
import androidx.navigation3.runtime.NavKey
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
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewmodel.compose.LocalViewModelStoreOwner
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle

internal enum class ThemeChoice {
  System,
  Light,
  Dark,
}

// Circuit owns presentation; Nav3 owns list/detail routes inside this fixed root screen.
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
    val requesters: List<RecordRequesterChoice> = emptyList(),
    val searching: Boolean = false,
    val loginCompletion: Int = 0,
    // The presenter supplies its real stack; this default is for static previews only.
    val backStack: List<NavKey> = listOf(RecordsList) + listOfNotNull(selectedRecordId?.let(::RecordDetail)),
    val recordOwner: String? = (session as? SessionState.SignedIn)?.cacheAccountId,
    val debate: DebateSubmissionState = DebateSubmissionState(),
    val debateStatus: DebateStatusState = DebateStatusState(),
    val debateHistory: DebateHistoryState = DebateHistoryState(),
    val debateOnline: Boolean = session is SessionState.SignedIn,
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
    data class OpenRecord(val recordId: String) : Event
    data object CloseRecord : Event
    data class SearchRecords(val text: String) : Event
    data class SelectWinner(val winner: RecordWinner) : Event
    data class SelectRequester(val displayName: String?) : Event
    data class SelectOrder(val order: RecordOrder) : Event
    data object ClearRecordQuery : Event
    data object ComposeDebate : Event
    class EditDebate(val question: String) : Event {
      override fun toString(): String = "EditDebate(<redacted>)"
    }
    data object SubmitDebate : Event
    data object CheckDebate : Event
    data object RetryDebate : Event
    data object ReauthenticateDebate : Event
    data object NewDebateDraft : Event
    data object ShowDebateRequests : Event
    data class OpenDebateRequest(val requestId: String) : Event
    data object LoadMoreDebateRequests : Event
    data object RefreshDebateRequests : Event
    data class OpenDebateResult(val recordId: String) : Event
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
    val loginCompletion by session.loginCompletion.collectAsState()
    val cachePermit by session.cachePermit.collectAsState()
    val pendingDestination by session.pendingDestination.collectAsState()
    val cacheAccountId = cachePermit?.accountId?.takeIf(session::isCacheAuthorized)
    val backStack = rememberNavBackStack(RecordsList)
    // Only opaque routes are saveable. Account ownership stays in memory and a restored route
    // is just a hint: neither cache reads nor UI visibility can bypass the live permit.
    var stackOwner by remember { mutableStateOf<String?>(null) }
    if (sessionState == SessionState.SigningOut) {
      backStack.closeRecord()
      stackOwner = null
    } else if (cacheAccountId != null && stackOwner != cacheAccountId) {
      if (stackOwner != null) {
        backStack.closeRecord()
        session.discardDestination()
      }
      stackOwner = cacheAccountId
    }
    LaunchedEffect(cacheAccountId, pendingDestination) {
      val request = pendingDestination ?: return@LaunchedEffect
      val owner = cacheAccountId ?: return@LaunchedEffect
      if (session.consumeDestination(request, owner)) {
        backStack.openRecord(request.returnTo.removePrefix("/records/"))
      }
    }
    val selectedRoute = backStack.lastOrNull() as? RecordDetail
    val selectedRecordId = selectedRoute?.recordId.takeIf { cacheAccountId != null }
    val context = LocalContext.current
    val signedIn = sessionState is SessionState.SignedIn
    val debateOnline = signedIn && rememberValidatedDebateNetwork()
    RecordNotificationPermissionEffect(authorized = cacheAccountId != null && signedIn)
    val currentSignedIn by rememberUpdatedState(signedIn)
    val currentSessionState by rememberUpdatedState(sessionState)
    DisposableEffect(cacheAccountId) {
      // Restored offline access can show decoded avatars before SignedIn exists.
      onDispose { if (cacheAccountId != null) SingletonImageLoader.get(context).memoryCache?.clear() }
    }
    val launcher = rememberLauncherForActivityResult(MobileLoginContract(), session::loginResult)
    val modelOwner = requireNotNull(LocalViewModelStoreOwner.current)
    val debateModel = remember(cacheAccountId, modelOwner) {
      cacheAccountId?.let { owner ->
        val factory = viewModelFactory { initializer {
          val client = DebateRequestsClient()
          val store = try { DebateWorkspaceStore.open(context.applicationContext, session::isCacheAuthorized) }
          catch (_: Exception) { null }
          DebateSubmissionModel({ session.isCacheAuthorized(owner) },
            { store?.load(owner) ?: throw DebateRequestException(DebateFailure.STORAGE) },
            { store?.save(owner, it) ?: throw DebateRequestException(DebateFailure.STORAGE) },
            { id, question -> session.withAuthorizedToken {
              if (!hasValidatedDebateNetwork(context)) throw DebateRequestException(DebateFailure.UNAVAILABLE)
              client.submit(it, id, question)
            }
              ?: throw DebateRequestException(DebateFailure.AUTH_REQUIRED) },
            { id ->
              class Lookup(val value: DebateRequest?)
              val result = session.withAuthorizedToken { Lookup(client.find(it, id)) }
                ?: throw DebateRequestException(DebateFailure.AUTH_REQUIRED)
              result.value
            }, { store?.close(); client.close() },
            { cursor -> session.withAuthorizedToken { client.list(it, cursor) }
              ?: throw DebateRequestException(DebateFailure.AUTH_REQUIRED) })
        } }
        ViewModelProvider(modelOwner, factory)["debate-$owner", DebateSubmissionModel::class.java]
      }
    }
    var previousDebateModel by remember { mutableStateOf<DebateSubmissionModel?>(null) }
    if (previousDebateModel !== debateModel) {
      previousDebateModel?.hide()
      previousDebateModel = debateModel
    }
    LaunchedEffect(debateModel) { debateModel?.restore() }
    val debate = debateModel?.state?.collectAsState()?.value ?: DebateSubmissionState()
    val debateStatus = debateModel?.status?.collectAsState()?.value ?: DebateStatusState()
    val debateHistory = debateModel?.history?.collectAsState()?.value ?: DebateHistoryState()
    LaunchedEffect(debate.failure, debateStatus.failure, debateHistory.failure) {
      if (listOf(debate.failure, debateStatus.failure, debateHistory.failure).contains(DebateFailure.AUTH_REQUIRED))
        session.onAuthenticationRequired()
    }
    var historyRefresh by remember(cacheAccountId) { mutableStateOf(0) }
    var historyMore by remember(cacheAccountId) { mutableStateOf(false) }
    val foregroundLifecycle = LocalLifecycleOwner.current.lifecycle
    val debateRoute = backStack.lastOrNull()
    LaunchedEffect(cacheAccountId, debate.workspace?.requestId, debateRoute) {
      val id = debate.workspace?.requestId ?: return@LaunchedEffect
      if (cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) && debateRoute == DebateCompose) {
        backStack[backStack.lastIndex] = DebateRequestStatus(id)
      }
    }
    LaunchedEffect(debateModel, debateRoute, debateOnline, historyRefresh, debate.busy) {
      if (!debateOnline) return@LaunchedEffect
      foregroundLifecycle.repeatOnLifecycle(Lifecycle.State.RESUMED) {
        when (val route = debateRoute) {
          is DebateRequestStatus -> {
            do {
              debateModel?.refreshStatus(route.requestId)
              val latest = debateModel?.status?.value
              if (latest?.requestId == route.requestId && latest.request?.terminal == true) break
              if (latest?.failure in setOf(DebateFailure.NOT_FOUND, DebateFailure.AUTH_REQUIRED,
                  DebateFailure.REAUTH_REQUIRED, DebateFailure.FORBIDDEN, DebateFailure.INVALID_RESPONSE)) break
              delay(5_000)
            } while (true)
          }
          DebateRequests -> {
            debateModel?.refreshHistory(more = historyMore)
            historyMore = false
          }
          else -> Unit
        }
      }
    }
    val records = remember(context.applicationContext) {
      try {
        RecordsRepository.open(context.applicationContext, session::isCacheAuthorized)
      }
      catch (_: Exception) { null }
    }
    DisposableEffect(records) { onDispose { records?.close() } }
    // Keep the same account's saved preview during a foreground session check.
    // Permission loss, account change, or navigation drops it immediately.
    var record by remember(cacheAccountId, selectedRecordId) { mutableStateOf<RecordPreviewState>(RecordPreviewState.Idle) }
    var recordRetry by remember { mutableStateOf(0) }
    var listRetry by remember { mutableStateOf(0) }
    var recordList by remember { mutableStateOf<RecordListState>(RecordListState.Idle) }
    // Search text and decrypted snapshots are account-bound memory, never SavedState.
    var listQuery by remember(cacheAccountId) { mutableStateOf(RecordListQuery()) }
    var savedEntries by remember(cacheAccountId) { mutableStateOf<List<RecordListEntry>?>(null) }
    val requesters = remember(savedEntries) { recordRequesterChoices(savedEntries.orEmpty()) }
    var searching by remember(cacheAccountId) { mutableStateOf(false) }
    var listOwner by remember { mutableStateOf<String?>(null) }
    var syncState by remember { mutableStateOf<RecordSyncState>(RecordSyncState.Idle) }
    LaunchedEffect(cacheAccountId, signedIn) {
      if (cacheAccountId != null && signedIn) {
        RecordSyncScheduler.schedule(context)
      }
    }
    LaunchedEffect(cacheAccountId) {
      syncState = RecordSyncState.Idle
      if (cacheAccountId != null) {
        RecordSyncScheduler.states(context).collect { status ->
          syncState = status
          if ((status as? RecordSyncState.Failed)?.reason == RecordReadFailure.AUTH_REQUIRED) {
            session.onSyncAuthenticationRequired()
          }
        }
      }
    }
    LaunchedEffect(cacheAccountId) {
      if (cacheAccountId != null) {
        // Initial local loading already includes older saves. Only new committed changes,
        // not WorkManager start/progress/end bookkeeping, invalidate the current snapshot.
        var observed = RecordSyncScheduler.cacheChanges.value
        RecordSyncScheduler.cacheChanges.collect { generation ->
          if (generation != observed) {
            observed = generation
            listRetry++
          }
        }
      }
    }
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
      snapshotFlow { listRetry }.conflate().collect {
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
        snapshotFlow { savedEntries }.conflate().collect { saved ->
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
          val status = syncState
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
    // Empty-list completion/errors must still change the presentation, without decrypting or
    // searching the same saved entries again just because worker/session state changed.
    LaunchedEffect(cacheAccountId, syncState, currentSessionState == SessionState.Unavailable,
      savedEntries?.isEmpty(), listQuery) {
      if (cacheAccountId == null || !session.isCacheAuthorized(cacheAccountId) ||
        savedEntries?.isEmpty() != true || !listQuery.isDefault) return@LaunchedEffect
      recordList = when {
        currentSessionState == SessionState.Unavailable || syncState == RecordSyncState.Completed ->
          RecordListState.Ready.fromSaved(emptyList(), recordList as? RecordListState.Ready)
        syncState is RecordSyncState.Failed -> RecordListState.Error((syncState as RecordSyncState.Failed).reason)
        else -> RecordListState.Idle
      }
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
      canReadRecords = cacheAccountId != null, listQuery = listQuery, searching = searching,
      requesters = if (cacheAccountId != null) requesters else emptyList(),
      loginCompletion = loginCompletion, backStack = backStack.toList(), recordOwner = cacheAccountId,
      debate = debate, debateStatus = debateStatus, debateHistory = debateHistory,
      debateOnline = debateOnline) { event ->
      when (event) {
        is BootstrapScreen.Event.SelectTheme -> themeChoice = event.choice
        BootstrapScreen.Event.Login -> if (session.beginLogin(
          session.pendingDestination.value?.returnTo ?: selectedRoute?.let { "/records/${it.recordId}" } ?: "/"
        )) {
          try { launcher.launch(session.loginDestination) }
          catch (_: ActivityNotFoundException) {
            session.loginResult(MobileLoginStep.Finished(MobileLoginStatus.BROWSER_UNAVAILABLE))
          }
        }
        BootstrapScreen.Event.Logout -> if (
          cacheAccountId?.let(session::isCacheAuthorized) ?: (session.offlineCacheAccountId == null)
        ) {
          // Clear synchronously before IO or another callback can observe the previous selection.
          backStack.closeRecord()
          stackOwner = null
          session.logout()
        }
        BootstrapScreen.Event.Retry -> session.retry()
        BootstrapScreen.Event.RetryRecord -> {
          recordRetry++
          if (!signedIn) session.retry() else RecordSyncScheduler.syncNow(context)
        }
        BootstrapScreen.Event.RefreshRecords -> if (
          cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) &&
          backStack.lastOrNull() == RecordsList && session.pendingDestination.value == null &&
          syncState != RecordSyncState.Running
        ) {
          // Queue the existing delta sync; keep the saved list, query, and scroll position.
          // Check the live stack/request/permit even when an older UI callback is retained.
          RecordSyncScheduler.syncNow(context)
        }
        BootstrapScreen.Event.RecordsAuthRequired -> session.onAuthenticationRequired()
        is BootstrapScreen.Event.OpenRecord -> if (
          cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) &&
          stackOwner == cacheAccountId && session.pendingDestination.value == null &&
          event.recordId in ((visibleList as? RecordListState.Ready)?.loadedIds ?: emptySet())
        ) backStack.openRecord(event.recordId)
        BootstrapScreen.Event.CloseRecord -> if (
          cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) &&
          stackOwner == cacheAccountId && backStack.lastOrNull() != RecordsList &&
          session.pendingDestination.value == null
        ) {
          if (backStack.lastOrNull() == DebateCompose) debateModel?.flush { backStack.closeRecord() }
          else backStack.removeLastOrNull()
        }
        is BootstrapScreen.Event.SearchRecords -> listQuery = listQuery.copy(text = event.text)
        is BootstrapScreen.Event.SelectWinner -> listQuery = listQuery.copy(winner = event.winner)
        is BootstrapScreen.Event.SelectRequester -> listQuery = listQuery.copy(requesterName = event.displayName)
        is BootstrapScreen.Event.SelectOrder -> listQuery = listQuery.copy(order = event.order)
        BootstrapScreen.Event.ClearRecordQuery -> listQuery = RecordListQuery()
        BootstrapScreen.Event.ComposeDebate -> if (cacheAccountId != null && session.isCacheAuthorized(cacheAccountId)) {
          backStack.closeRecord()
          backStack.add(DebateCompose)
        }
        is BootstrapScreen.Event.EditDebate -> debateModel?.edit(event.question)
        BootstrapScreen.Event.SubmitDebate -> if (signedIn && hasValidatedDebateNetwork(context)) debateModel?.submit()
        BootstrapScreen.Event.CheckDebate -> if (signedIn && hasValidatedDebateNetwork(context)) debateModel?.reconcile()
        BootstrapScreen.Event.RetryDebate -> if (signedIn && hasValidatedDebateNetwork(context)) debateModel?.retryConfirmedMissing()
        BootstrapScreen.Event.NewDebateDraft -> if (cacheAccountId != null && session.isCacheAuthorized(cacheAccountId)) {
          debateModel?.newDraft()
          backStack.closeRecord()
          backStack.add(DebateCompose)
        }
        BootstrapScreen.Event.ShowDebateRequests -> if (cacheAccountId != null && session.isCacheAuthorized(cacheAccountId)) {
          val show = { backStack.closeRecord(); backStack.add(DebateRequests); Unit }
          if (backStack.lastOrNull() == DebateCompose) debateModel?.flush(show) else show()
        }
        is BootstrapScreen.Event.OpenDebateRequest -> if (
          cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) && stackOwner == cacheAccountId &&
          backStack.lastOrNull() == DebateRequests && validDebateRequestId(event.requestId) &&
          debateHistory.items.any { it.requestId == event.requestId }
        ) backStack.add(DebateRequestStatus(event.requestId))
        BootstrapScreen.Event.LoadMoreDebateRequests -> if (signedIn && hasValidatedDebateNetwork(context) && backStack.lastOrNull() == DebateRequests) {
          historyMore = true
          historyRefresh++
        }
        BootstrapScreen.Event.RefreshDebateRequests -> if (signedIn && hasValidatedDebateNetwork(context) && backStack.lastOrNull() == DebateRequests) {
          historyMore = false
          historyRefresh++
        }
        is BootstrapScreen.Event.OpenDebateResult -> if (
          cacheAccountId != null && session.isCacheAuthorized(cacheAccountId) && stackOwner == cacheAccountId &&
          debateRoute is DebateRequestStatus && debateStatus.requestId == debateRoute.requestId &&
          debateStatus.request?.let { it.status == "published" && it.recordId == event.recordId } == true
        ) {
          backStack.closeRecord()
          backStack.openRecord(event.recordId)
          if (signedIn) RecordSyncScheduler.syncNow(context)
        }
        BootstrapScreen.Event.ReauthenticateDebate -> debateModel?.flush {
          session.reauthenticate {
            if (session.beginLogin("/")) {
              try { launcher.launch(session.loginDestination) }
              catch (_: ActivityNotFoundException) {
                session.loginResult(MobileLoginStep.Finished(MobileLoginStatus.BROWSER_UNAVAILABLE))
              }
            }
          }
        }
      }
    }
  }
}
