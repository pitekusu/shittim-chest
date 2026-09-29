package dev.pitekusu.shittim.records

import androidx.activity.compose.PredictiveBackHandler
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.isTraversalGroup
import androidx.compose.ui.semantics.paneTitle
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.paging.compose.LazyPagingItems
import dev.pitekusu.shittim.records.auth.SessionState
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3AdaptiveApi::class)
@Composable
internal fun AdaptiveRecordsUi(
  state: BootstrapScreen.State,
  pagingItems: LazyPagingItems<RecordListEntry>?,
  listScrollState: LazyListState,
  detailScrollState: LazyListState,
  modifier: Modifier = Modifier,
  playedSections: Set<String> = emptySet(),
  onSectionSeen: (String) -> Unit = {},
) {
  val windowDirective = calculatePaneScaffoldDirective(currentWindowAdaptiveInfoV2())
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
    // Circuit owns the selection; Adaptive owns only the current visual transition.
    val scaffoldState = remember { MutableThreePaneScaffoldState(value) }
    val currentState = rememberUpdatedState(state)
    val currentValue = rememberUpdatedState(value)
    val focusManager = LocalFocusManager.current
    var searchFocusAllowed by remember { mutableStateOf(state.selectedRecordId == null) }
    // Disclosure state is visual only; search text remains in Circuit's screen-lifetime state.
    var queryMode by remember { mutableStateOf(RecordQueryMode.Closed) }
    LaunchedEffect(value, state.selectedRecordId) {
      if (state.selectedRecordId != null) {
        queryMode = RecordQueryMode.Closed
        searchFocusAllowed = false
        focusManager.clearFocus(force = true)
      }
      scaffoldState.animateTo(value)
      // Keep the returning pane's search field out of focus restoration until it is fully visible.
      if (currentState.value.selectedRecordId == null) searchFocusAllowed = true
    }
    PredictiveBackHandler(enabled = state.selectedRecordId != null) { progress ->
      val selectedAtStart = currentState.value.selectedRecordId
      try {
        progress.collect { event ->
          scaffoldState.seekTo(event.progress, listValue, isPredictiveBackInProgress = true)
        }
        // A completed gesture must not close another record or act after authentication is lost.
        if (selectedAtStart != null && currentState.value.canReadRecords &&
          currentState.value.selectedRecordId == selectedAtStart) {
          scaffoldState.animateTo(listValue, isPredictiveBackInProgress = true)
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
          Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
            LazyColumn(Modifier.widthIn(max = 560.dp).fillMaxSize().testTag("bootstrap-content"),
              state = listScrollState, contentPadding = PaddingValues(ShittimSpacing.Large),
              verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Large)) {
              item(key = "brand") { BootstrapHeader(Modifier.fillMaxWidth(), compact = true) }
              if (state.session == SessionState.Unavailable) item(key = "offline-notice") {
                Text(stringResource(R.string.record_offline))
              }
              recordListItems(state.records, pagingItems, state.eventSink,
                sync = if (state.session is SessionState.SignedIn) state.sync else RecordSyncState.Idle,
                query = state.listQuery, searching = state.searching, selectedRecordId = state.selectedRecordId,
                searchCanFocus = twoPanes || (state.selectedRecordId == null && searchFocusAllowed),
                queryMode = if (state.selectedRecordId == null) queryMode else RecordQueryMode.Closed,
                onQueryModeChange = { queryMode = it })
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
          } else Column(Modifier.fillMaxSize(), horizontalAlignment = Alignment.CenterHorizontally) {
            // Keep the detail title visible while reading a long record; system Back returns to the list.
            Column(Modifier.widthIn(max = 760.dp).fillMaxWidth().padding(horizontal = ShittimSpacing.Large)) {
              Text(detailTitle, style = MaterialTheme.typography.titleLargeEmphasized,
                modifier = Modifier.semantics { heading() })
            }
            LazyColumn(Modifier.widthIn(max = 760.dp).fillMaxSize()
              .testTag(if (twoPanes) "record-detail-content" else "bootstrap-content"),
              state = detailScrollState, contentPadding = PaddingValues(ShittimSpacing.Large)) {
              item(key = "record-detail") { RecordPreviewPanel(state.record, state.eventSink,
                recordId = state.selectedRecordId,
                playedSections = playedSections, onSectionSeen = onSectionSeen) }
            }
          }
        }
      })
  }
}
