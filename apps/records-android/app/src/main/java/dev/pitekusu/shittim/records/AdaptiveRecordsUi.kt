package dev.pitekusu.shittim.records

import android.animation.ValueAnimator
import androidx.activity.compose.PredictiveBackHandler
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
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.adaptive.ExperimentalMaterial3AdaptiveApi
import androidx.compose.material3.adaptive.currentWindowAdaptiveInfoV2
import androidx.compose.material3.adaptive.layout.AnimatedPane
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffold
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffoldDefaults
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffoldRole
import androidx.compose.material3.adaptive.layout.MutableThreePaneScaffoldState
import androidx.compose.material3.adaptive.layout.ThreePaneScaffoldDestinationItem
import androidx.compose.material3.adaptive.layout.calculatePaneScaffoldDirective
import androidx.compose.material3.adaptive.layout.calculateThreePaneScaffoldValue
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.material3.pulltorefresh.PullToRefreshDefaults
import androidx.compose.material3.pulltorefresh.rememberPullToRefreshState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.paging.compose.LazyPagingItems
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

@OptIn(ExperimentalMaterial3AdaptiveApi::class)
@Composable
internal fun AdaptiveRecordsUi(
  state: BootstrapScreen.State,
  pagingItems: LazyPagingItems<RecordJournalRow>?,
  listScrollState: LazyListState,
  detailScrollState: LazyListState,
  modifier: Modifier = Modifier,
  playedSections: Set<String> = emptySet(),
  motionAllowed: Boolean = true,
  onSectionSeen: (String) -> Unit = {},
) {
  val windowDirective = calculatePaneScaffoldDirective(currentWindowAdaptiveInfoV2())
  // AnimatedPane retains its role bucket while hidden. Own a bounded detail bucket outside
  // that pane so reopening resets the reader and old visits do not accumulate in SavedState.
  val detailStateHolder = rememberSaveableStateHolder()
  val detailVisit = rememberSaveable(state.selectedRecordId) { UUID.randomUUID().toString() }
  var previousDetailVisit by rememberSaveable { mutableStateOf(detailVisit) }
  LaunchedEffect(detailVisit) {
    if (previousDetailVisit != detailVisit) detailStateHolder.removeState(previousDetailVisit)
    previousDetailVisit = detailVisit
  }
  BoxWithConstraints(modifier) {
    // Use the available content width, not physical screen size; preserve standard hinge avoidance.
    val twoPanes = maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f
    val directive = windowDirective.copy(maxHorizontalPartitions = if (twoPanes) 2 else 1)
    val destination = if (state.selectedRecordId == null) ListDetailPaneScaffoldRole.List
      else ListDetailPaneScaffoldRole.Detail
    val value = calculateThreePaneScaffoldValue(directive.maxHorizontalPartitions,
      ListDetailPaneScaffoldDefaults.adaptStrategies(), ThreePaneScaffoldDestinationItem<Unit>(destination))
    val listValue = calculateThreePaneScaffoldValue(directive.maxHorizontalPartitions,
      ListDetailPaneScaffoldDefaults.adaptStrategies(),
      ThreePaneScaffoldDestinationItem<Unit>(ListDetailPaneScaffoldRole.List))
    // Nav3 owns the selection; Adaptive temporarily owns only the visual transition.
    val scaffoldState = remember { MutableThreePaneScaffoldState(value) }
    val currentState = rememberUpdatedState(state)
    val currentValue = rememberUpdatedState(value)
    val focusManager = LocalFocusManager.current
    // Disclosure state is visual only; search text remains in Circuit's screen-lifetime state.
    var queryMode by remember { mutableStateOf(RecordQueryMode.Closed) }
    val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
    val visibleMotion = motionAllowed && lifecycle.isAtLeast(Lifecycle.State.STARTED) &&
      ValueAnimator.areAnimatorsEnabled()
    val listMotion = visibleMotion && queryMode == RecordQueryMode.Closed && state.selectedRecordId == null
    val refreshAvailable = state.canReadRecords && state.selectedRecordId == null &&
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
    LaunchedEffect(value, state.selectedRecordId) {
      if (state.selectedRecordId != null) {
        queryMode = RecordQueryMode.Closed
        focusManager.clearFocus(force = true)
        keyboard?.hide()
      }
      scaffoldState.animateTo(value)
    }
    PredictiveBackHandler(enabled = state.selectedRecordId != null) { progress ->
      val selectedAtStart = currentState.value.selectedRecordId
      var gestureInProgress = false
      try {
        progress.collect { event ->
          gestureInProgress = true
          scaffoldState.seekTo(event.progress, listValue, isPredictiveBackInProgress = true)
        }
        // A completed gesture must not close another record or act after authentication is lost.
        if (selectedAtStart != null && currentState.value.canReadRecords &&
          currentState.value.selectedRecordId == selectedAtStart) {
          // Keep the outgoing detail until its short slide finishes, rather than swapping
          // it for a placeholder. A bounded tween avoids the old long spring tail;
          // the compact list never renders a selected-card highlight during the return.
          if (ValueAnimator.areAnimatorsEnabled()) {
            scaffoldState.animateTo(listValue,
              animationSpec = tween(280, easing = FastOutSlowInEasing),
              // A button-only Back has no predictive preview. Marking it as one
              // shrinks the list, then adds a separate scale-restoring spring.
              isPredictiveBackInProgress = gestureInProgress)
          } else scaffoldState.snapTo(listValue)
          if (currentState.value.canReadRecords && currentState.value.selectedRecordId == selectedAtStart) {
            currentState.value.eventSink(BootstrapScreen.Event.CloseRecord)
          } else scaffoldState.snapTo(currentValue.value)
        } else scaffoldState.snapTo(currentValue.value)
      } catch (cancelled: CancellationException) {
        // Restore without waiting for frames: composition may already be removed after logout.
        withContext(NonCancellable) { scaffoldState.snapTo(currentValue.value) }
        throw cancelled
      }
    }
    val listTitle = stringResource(R.string.record_title)
    val detailTitle = stringResource(R.string.record_detail_title)
    ListDetailPaneScaffold(directive, scaffoldState, modifier = Modifier.fillMaxSize(),
      listPane = {
        AnimatedPane(Modifier.preferredWidth(400.dp).semantics {
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
                top = ShittimSpacing.Medium, bottom = 104.dp),
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
            if (queryAvailable && state.selectedRecordId == null && queryMode == RecordQueryMode.Closed && motionAllowed) {
              Box(Modifier.align(Alignment.BottomCenter).widthIn(max = 560.dp).fillMaxWidth(),
                contentAlignment = Alignment.BottomEnd) {
                RecordQueryToolbar(state.listQuery, Modifier.padding(ShittimSpacing.Medium),
                  onSearch = { queryMode = RecordQueryMode.Search },
                  onFilters = { focusManager.clearFocus(force = true); queryMode = RecordQueryMode.Filters })
              }
            }
          }
        }
      },
      detailPane = {
        AnimatedPane(Modifier.semantics { paneTitle = detailTitle; isTraversalGroup = true }) {
          if (state.selectedRecordId == null) {
            Box(Modifier.fillMaxSize().padding(ShittimSpacing.Large), contentAlignment = Alignment.Center) {
              Text(stringResource(R.string.record_select), color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
          } else Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            detailStateHolder.SaveableStateProvider(detailVisit) {
              RecordDetailScreen(state.record, state.selectedRecordId, state.eventSink,
                Modifier.widthIn(max = 760.dp).fillMaxSize(), detailScrollState,
                scrollTag = if (twoPanes) "record-detail-content" else "bootstrap-content",
                motionAllowed = motionAllowed, playedSections = playedSections,
                onSectionSeen = onSectionSeen)
            }
          }
        }
      })
    if (state.canReadRecords && state.selectedRecordId == null) {
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
              scope.launch {
                if (index >= 0) listScrollState.scrollToItem(prefix + index, offset)
                else listScrollState.scrollToItem(0)
                if (currentState.value.canReadRecords && currentState.value.selectedRecordId == null) {
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
