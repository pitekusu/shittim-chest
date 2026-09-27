package dev.pitekusu.shittim.records

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
import androidx.compose.material3.TextButton
import androidx.compose.material3.adaptive.ExperimentalMaterial3AdaptiveApi
import androidx.compose.material3.adaptive.currentWindowAdaptiveInfoV2
import androidx.compose.material3.adaptive.layout.AnimatedPane
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffold
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffoldDefaults
import androidx.compose.material3.adaptive.layout.ListDetailPaneScaffoldRole
import androidx.compose.material3.adaptive.layout.ThreePaneScaffoldDestinationItem
import androidx.compose.material3.adaptive.layout.calculatePaneScaffoldDirective
import androidx.compose.material3.adaptive.layout.calculateThreePaneScaffoldValue
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
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

@OptIn(ExperimentalMaterial3AdaptiveApi::class)
@Composable
internal fun AdaptiveRecordsUi(
  state: BootstrapScreen.State,
  pagingItems: LazyPagingItems<RecordListEntry>?,
  listScrollState: LazyListState,
  detailScrollState: LazyListState,
  modifier: Modifier = Modifier,
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
    val listTitle = stringResource(R.string.record_title)
    val detailTitle = stringResource(R.string.record_detail_title)
    ListDetailPaneScaffold(directive, value, modifier = Modifier.fillMaxSize(),
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
              item(key = "session") { SessionPanel(state.session, state.eventSink, canReadRecords = true) }
              item(key = "theme") {
                BootstrapThemeSelector(state.themeChoice) {
                  state.eventSink(BootstrapScreen.Event.SelectTheme(it))
                }
              }
              if (state.session == SessionState.Unavailable) item(key = "offline-notice") {
                Text(stringResource(R.string.record_offline))
              }
              recordListItems(state.records, pagingItems, state.eventSink,
                sync = if (state.session is SessionState.SignedIn) state.sync else RecordSyncState.Idle,
                query = state.listQuery, searching = state.searching, selectedRecordId = state.selectedRecordId)
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
            // Keep the exit visible while reading a long record, including at enlarged text sizes.
            Column(Modifier.widthIn(max = 760.dp).fillMaxWidth().padding(horizontal = ShittimSpacing.Large)) {
              TextButton(onClick = { state.eventSink(BootstrapScreen.Event.CloseRecord) }) {
                Text(stringResource(R.string.record_close))
              }
              Text(detailTitle, style = MaterialTheme.typography.titleLargeEmphasized,
                modifier = Modifier.semantics { heading() })
            }
            LazyColumn(Modifier.widthIn(max = 760.dp).fillMaxSize()
              .testTag(if (twoPanes) "record-detail-content" else "bootstrap-content"),
              state = detailScrollState, contentPadding = PaddingValues(ShittimSpacing.Large)) {
              item(key = "record-detail") { RecordPreviewPanel(state.record, state.eventSink, showCloseButton = false) }
            }
          }
        }
      })
  }
}
