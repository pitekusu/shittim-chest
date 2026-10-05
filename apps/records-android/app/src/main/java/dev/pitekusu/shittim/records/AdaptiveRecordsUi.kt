package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.togetherWith
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.ExperimentalMaterial3ExpressiveApi
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MediumFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.material3.adaptive.ExperimentalMaterial3AdaptiveApi
import androidx.compose.material3.adaptive.currentWindowAdaptiveInfoV2
import androidx.compose.material3.adaptive.layout.calculatePaneScaffoldDirective
import androidx.compose.material3.adaptive.layout.PaneScaffoldDirective
import androidx.compose.material3.adaptive.navigation.BackNavigationBehavior
import androidx.compose.material3.adaptive.navigation3.ListDetailSceneStrategy
import androidx.compose.material3.adaptive.navigation3.rememberListDetailSceneStrategy
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.PullToRefreshDefaults
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.LookaheadScope
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.navigation3.runtime.NavKey
import androidx.navigation3.runtime.entryProvider
import androidx.navigation3.runtime.rememberDecoratedNavEntries
import androidx.navigation3.runtime.rememberSaveableStateHolderNavEntryDecorator
import androidx.navigation3.scene.NavigationBackHandler
import androidx.navigation3.scene.rememberNavigationEventState
import androidx.navigation3.scene.rememberSceneState
import androidx.navigation3.ui.NavDisplay
import androidx.paging.compose.LazyPagingItems
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3AdaptiveApi::class, ExperimentalMaterial3ExpressiveApi::class)
@Composable
internal fun AdaptiveRecordsUi(
  state: BootstrapScreen.State,
  pagingItems: LazyPagingItems<RecordJournalRow>?,
  listScrollState: LazyListState,
  modifier: Modifier = Modifier,
  playedSections: Set<String> = emptySet(),
  motionAllowed: Boolean = true,
  onSectionSeen: (String) -> Unit = {},
) {
  val windowDirective = calculatePaneScaffoldDirective(currentWindowAdaptiveInfoV2())
  BoxWithConstraints(modifier) {
    // Use the available content width, not physical screen size; preserve standard hinge avoidance.
    val twoPanes = maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f
    val directive = PaneScaffoldDirective(maxHorizontalPartitions = if (twoPanes) 2 else 1,
      horizontalPartitionSpacerSize = windowDirective.horizontalPartitionSpacerSize,
      maxVerticalPartitions = windowDirective.maxVerticalPartitions,
      verticalPartitionSpacerSize = windowDirective.verticalPartitionSpacerSize,
      defaultPanePreferredWidth = windowDirective.defaultPanePreferredWidth,
      defaultPanePreferredHeight = windowDirective.defaultPanePreferredHeight,
      excludedBounds = windowDirective.excludedBounds, shouldAutoFocusCurrentDestination = false)
    // Compact layouts use NavDisplay's slide without Adaptive's predictive scale restoration.
    val sceneStrategy = rememberListDetailSceneStrategy<NavKey>(directive = directive,
      backNavigationBehavior = BackNavigationBehavior.PopLatest)
    val currentState = rememberUpdatedState(state)
    val focusManager = LocalFocusManager.current
    // Disclosure state is visual only; search text remains in Circuit's screen-lifetime state.
    var queryMode by remember(state.recordOwner) { mutableStateOf(RecordQueryMode.Closed) }
    val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
    val visibleMotion = motionAllowed && lifecycle.isAtLeast(Lifecycle.State.STARTED) &&
      ValueAnimator.areAnimatorsEnabled()
    val onList = state.backStack.lastOrNull() == RecordsList
    val listMotion = visibleMotion && queryMode == RecordQueryMode.Closed && onList
    val refreshAvailable = state.canReadRecords && onList &&
      queryMode == RecordQueryMode.Closed && motionAllowed && lifecycle.isAtLeast(Lifecycle.State.STARTED)
    // WorkManager owns the operation. Queued/offline work must not keep the gesture spinner alive.
    val refreshing = refreshAvailable && state.sync == RecordSyncState.Running
    val refreshState = rememberPullToRefreshState()
    val searchScrollState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    val keyboard = LocalSoftwareKeyboardController.current
    var previousQuery by remember { mutableStateOf(state.listQuery) }
    LaunchedEffect(state.listQuery) {
      if (previousQuery != state.listQuery) {
        previousQuery = state.listQuery
        // Criteria changes are intentional navigation; sync snapshots must never enter this path.
        listScrollState.scrollToItem(0)
        searchScrollState.scrollToItem(0)
      }
    }
    LaunchedEffect(state.backStack.lastOrNull(), state.recordOwner) {
      if (!onList) {
        queryMode = RecordQueryMode.Closed
        focusManager.clearFocus(force = true)
        keyboard?.hide()
      }
    }
    val listTitle = stringResource(R.string.record_title)
    val detailTitle = stringResource(R.string.record_detail_title)
    // A new account cannot inherit outgoing entries or their in-memory decrypted previews.
    key(state.recordOwner) {
      // Cached NavEntries keep their lifetime while their content observes the latest inputs.
      val listContent = rememberUpdatedState<@Composable () -> Unit> {
        // Both entries need their own opaque background: NavDisplay keeps the outgoing
        // entry below the incoming slide, including the reader's otherwise-empty margins.
        ShittimBackdrop(Modifier.fillMaxSize().testTag("records-list-pane").semantics {
          paneTitle = listTitle
          isTraversalGroup = true
        }) {
          PullToRefreshBox(isRefreshing = refreshing,
            onRefresh = { if (refreshAvailable) state.eventSink(BootstrapScreen.Event.RefreshRecords) },
            enabled = refreshAvailable && !refreshing,
            state = refreshState,
            modifier = Modifier.fillMaxSize().testTag("records-pull-refresh"),
            contentAlignment = Alignment.TopCenter,
            indicator = {
              if (refreshAvailable) PullToRefreshDefaults.Indicator(
                state = refreshState, isRefreshing = refreshing,
                modifier = Modifier.align(Alignment.TopCenter).testTag("records-refresh-indicator"),
                containerColor = MaterialTheme.colorScheme.primaryContainer,
                color = MaterialTheme.colorScheme.onPrimaryContainer)
            }) {
            LazyColumn(Modifier.widthIn(max = 560.dp).fillMaxSize().testTag("bootstrap-content"),
              state = listScrollState, contentPadding = PaddingValues(
                start = ShittimSpacing.Large, end = ShittimSpacing.Large,
                // Keep the final card above both independent floating actions.
                top = ShittimSpacing.Medium, bottom = 200.dp),
              verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
              item(key = "brand") { BootstrapHeader(Modifier.fillMaxWidth(), compact = true, motionAllowed = listMotion) }
              if (!state.listQuery.isDefault) item(key = "records-query-active") {
                RecordActiveQueryChips(state.listQuery, requesters = state.requesters, onEvent = state.eventSink)
              }
              recordListItems(state.records, pagingItems, state.eventSink,
                sync = if (state.session is SessionState.SignedIn) state.sync else RecordSyncState.Idle,
                offline = state.session == SessionState.Unavailable,
                query = state.listQuery, searching = state.searching,
                selectedRecordId = state.selectedRecordId.takeIf { twoPanes }, motionAllowed = listMotion)
            }
            val queryAvailable = (state.records as? RecordListState.Ready)?.saved == true || !state.listQuery.isDefault
            if (state.canReadRecords && onList && queryMode == RecordQueryMode.Closed && motionAllowed) {
              Box(Modifier.align(Alignment.BottomCenter).widthIn(max = 560.dp).fillMaxWidth(),
                contentAlignment = Alignment.BottomEnd) {
                // The parent already consumes safe-drawing/IME insets; do not pad them twice.
                Column(Modifier.padding(ShittimSpacing.Medium), horizontalAlignment = Alignment.End,
                  verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
                  // Drafts remain available even before records load or while reading offline.
                  MediumFloatingActionButton(
                    onClick = { state.eventSink(BootstrapScreen.Event.ComposeDebate) },
                    modifier = Modifier.testTag("debate-compose-open"),
                    shape = CircleShape,
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = MaterialTheme.colorScheme.onPrimary) {
                    Icon(painterResource(R.drawable.ic_add_debate),
                      contentDescription = stringResource(R.string.debate_start), Modifier.size(36.dp))
                  }
                  if (queryAvailable) {
                    // Nav3 moves this entry between single- and two-pane lookahead roots. A local
                    // root keeps Material's toolbar alignment-line owner valid across that move.
                    LookaheadScope {
                      RecordQueryToolbar(state.listQuery,
                        onSearch = { queryMode = RecordQueryMode.Search },
                        onFilters = { focusManager.clearFocus(force = true); queryMode = RecordQueryMode.Filters })
                    }
                  }
                }
              }
            }
          }
        }
      }
      val detailContent = rememberUpdatedState<@Composable (RecordDetail) -> Unit> { route ->
        // Pop changes the live selection immediately; NavDisplay retains this entry until
        // its slide finishes. Keep only this entry's preview in memory, never SavedState.
        var preview by remember { mutableStateOf(
          if (state.selectedRecordId == route.recordId) state.record else RecordPreviewState.Idle) }
        if (state.selectedRecordId == route.recordId) preview = state.record
        val entryLifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
        ShittimBackdrop(Modifier.fillMaxSize().testTag("records-detail-pane")
          .semantics { paneTitle = detailTitle; isTraversalGroup = true }) {
          Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            RecordDetailScreen(preview, route.recordId, state.eventSink,
              Modifier.widthIn(max = 760.dp).fillMaxSize(), rememberLazyListState(),
              scrollTag = if (twoPanes) "record-detail-content" else "bootstrap-content",
              motionAllowed = motionAllowed && state.selectedRecordId == route.recordId &&
                entryLifecycle.isAtLeast(Lifecycle.State.RESUMED), playedSections = playedSections,
              onSectionSeen = onSectionSeen)
          }
        }
      }
      val composeContent = rememberUpdatedState<@Composable () -> Unit> {
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateComposeScreen(state.debate, state.debateOnline,
            onEdit = { state.eventSink(BootstrapScreen.Event.EditDebate(it)) },
            onSubmit = { state.eventSink(BootstrapScreen.Event.SubmitDebate) },
            onCheck = { state.eventSink(BootstrapScreen.Event.CheckDebate) },
            onRetry = { state.eventSink(BootstrapScreen.Event.RetryDebate) },
            onReauth = { state.eventSink(BootstrapScreen.Event.ReauthenticateDebate) },
            onNew = { state.eventSink(BootstrapScreen.Event.NewDebateDraft) },
            modifier = Modifier.widthIn(max = 760.dp).align(Alignment.TopCenter))
        }
      }
      val requestsContent = rememberUpdatedState<@Composable () -> Unit> {
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateRequestsScreen(state.debateHistory, state.debateOnline,
            onOpen = { state.eventSink(BootstrapScreen.Event.OpenDebateRequest(it)) },
            onMore = { state.eventSink(BootstrapScreen.Event.LoadMoreDebateRequests) },
            onRetry = { state.eventSink(BootstrapScreen.Event.RefreshDebateRequests) },
            modifier = Modifier.widthIn(max = 760.dp).align(Alignment.TopCenter))
        }
      }
      val statusContent = rememberUpdatedState<@Composable (DebateRequestStatus) -> Unit> { route ->
        ShittimBackdrop(Modifier.fillMaxSize()) {
          DebateRequestStatusScreen(route.requestId, state.debateStatus, state.debate,
            state.debateOnline,
            onRetry = { state.eventSink(BootstrapScreen.Event.RetryDebate) },
            onResult = { state.eventSink(BootstrapScreen.Event.OpenDebateResult(it)) },
            onReauth = { state.eventSink(BootstrapScreen.Event.ReauthenticateDebate) },
            onNew = { state.eventSink(BootstrapScreen.Event.NewDebateDraft) },
            modifier = Modifier.widthIn(max = 760.dp).align(Alignment.TopCenter))
        }
      }
      // Keep Adaptive metadata and entry content identities stable between a predictive
      // preview and its committed destination. The holders still render current screen state.
      val recordsEntryProvider = remember {
        entryProvider<NavKey> {
          entry<RecordsList>(metadata = ListDetailSceneStrategy.listPane(detailPlaceholder = {
            Box(Modifier.fillMaxSize().padding(ShittimSpacing.Large), contentAlignment = Alignment.Center) {
              Text(stringResource(R.string.record_select), color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
          }) + ListDetailSceneStrategy.preferredPaneSize(width = 400.dp)) { listContent.value() }
          entry<RecordDetail>(metadata = ListDetailSceneStrategy.detailPane()) { detailContent.value(it) }
          entry<DebateCompose> { composeContent.value() }
          entry<DebateRequests> { requestsContent.value() }
          entry<DebateRequestStatus> { statusContent.value(it) }
        }
      }
      val entries = rememberDecoratedNavEntries(backStack = state.backStack,
        entryDecorators = listOf(rememberSaveableStateHolderNavEntryDecorator()),
        entryProvider = recordsEntryProvider)
      val onBack = { state.eventSink(BootstrapScreen.Event.CloseRecord) }
      val sceneState = rememberSceneState(entries, listOf(sceneStrategy), onBack = onBack)
      // Wide scenes own their Back handler internally. Rehost only their visual scope on a
      // route change to cancel a gesture against the old detail; keep decorated entry state
      // outside this key so resize and selection do not discard reading state. Compact hosts
      // stay alive across pops to retain their outgoing slide.
      key(if (twoPanes) state.backStack.last() else RecordsList) {
        val navigationEventState = key(state.backStack.last()) { rememberNavigationEventState(sceneState) }
        key(navigationEventState) {
          // Match NavDisplay's standard order: the Adaptive scene's internal handler wins.
          NavigationBackHandler(sceneState, navigationEventState, onBackCompleted = onBack)
        }
        NavDisplay(sceneState, navigationEventState, modifier = Modifier.fillMaxSize(), sizeTransform = null,
          transitionSpec = {
            slideInHorizontally(tween(280, easing = FastOutSlowInEasing)) { it } togetherWith
              slideOutHorizontally(tween(280, easing = FastOutSlowInEasing)) { -it / 4 }
          },
          popTransitionSpec = {
            slideInHorizontally(tween(280, easing = FastOutSlowInEasing)) { -it / 4 } togetherWith
              slideOutHorizontally(tween(280, easing = FastOutSlowInEasing)) { it }
          },
          predictivePopTransitionSpec = {
            slideInHorizontally(tween(280, easing = FastOutSlowInEasing)) { -it / 4 } togetherWith
              slideOutHorizontally(tween(280, easing = FastOutSlowInEasing)) { it }
          })
      }
    }
    if (state.canReadRecords && onList) {
      when (queryMode) {
        RecordQueryMode.Filters -> RecordFilterSheet(state.listQuery,
          requesters = state.requesters,
          onDismiss = { queryMode = RecordQueryMode.Closed }, onEvent = state.eventSink)
        RecordQueryMode.Search -> RecordFullScreenSearch(state.listQuery,
          onDismiss = { queryMode = RecordQueryMode.Closed }, onEvent = { event ->
            if (event is BootstrapScreen.Event.OpenRecord) {
              // Translate the stable result anchor, not its index: the main list also has a brand/criteria row.
              val anchor = searchScrollState.layoutInfo.visibleItemsInfo.firstOrNull()
              val index = pagingItems?.itemSnapshotList?.items?.indexOfFirst { it.stableKey == anchor?.key } ?: -1
              val prefix = 2 + if (state.listQuery.isDefault) 0 else 1 // brand, optional chips, context
              val offset = searchScrollState.firstVisibleItemScrollOffset
              val ownerAtStart = state.recordOwner
              scope.launch {
                if (index >= 0) listScrollState.scrollToItem(prefix + index, offset)
                else listScrollState.scrollToItem(0)
                if (currentState.value.canReadRecords && currentState.value.selectedRecordId == null &&
                  currentState.value.recordOwner == ownerAtStart) {
                  currentState.value.eventSink(event)
                }
              }
            } else state.eventSink(event)
          }) { resultEvent ->
            LazyColumn(Modifier.fillMaxSize().testTag("records-search-results"),
              state = searchScrollState, contentPadding = PaddingValues(ShittimSpacing.Medium),
              verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
              val filters = state.listQuery.copy(text = "")
              if (!filters.isDefault) item(key = "search-query-active") {
                RecordActiveQueryChips(filters, requesters = state.requesters, onEvent = state.eventSink)
              }
              recordListItems(state.records, pagingItems, resultEvent,
                sync = if (state.session is SessionState.SignedIn) state.sync else RecordSyncState.Idle,
                offline = state.session == SessionState.Unavailable,
                query = state.listQuery, searching = state.searching, motionAllowed = visibleMotion)
            }
          }
        RecordQueryMode.Closed -> Unit
      }
    }
  }
}
