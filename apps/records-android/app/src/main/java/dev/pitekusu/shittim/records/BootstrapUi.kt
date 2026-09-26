package dev.pitekusu.shittim.records

import android.content.res.Configuration
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.paging.LoadState
import androidx.paging.compose.LazyPagingItems
import androidx.paging.compose.collectAsLazyPagingItems
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import dev.pitekusu.shittim.records.ui.ShittimEmblem
import dev.pitekusu.shittim.records.ui.ShittimTheme
import dev.pitekusu.shittim.records.auth.SessionState

@Preview(name = "Light", widthDp = 360, heightDp = 800)
@Preview(name = "Dark", uiMode = Configuration.UI_MODE_NIGHT_YES, widthDp = 360, heightDp = 800)
@Preview(name = "Small / large text", widthDp = 320, heightDp = 640, fontScale = 2f)
@Preview(name = "Expanded", widthDp = 1000, heightDp = 700)
@Composable
private fun BootstrapPreview() {
  BootstrapUi(BootstrapScreen.State(ThemeChoice.System) {})
}

@Composable
internal fun BootstrapUi(state: BootstrapScreen.State, modifier: Modifier = Modifier) {
  val themeChoice = state.themeChoice
  val darkTheme =
    when (themeChoice) {
      ThemeChoice.System -> isSystemInDarkTheme()
      ThemeChoice.Light -> false
      ThemeChoice.Dark -> true
    }
  ShittimTheme(darkTheme) {
    ShittimBackdrop(modifier) {
      BackHandler(enabled = state.canReadRecords && state.selectedRecordId != null) {
        state.eventSink(BootstrapScreen.Event.CloseRecord)
      }
      val pagingItems = (state.records as? RecordListState.Ready)?.pages?.collectAsLazyPagingItems()
      val refreshError = pagingItems?.loadState?.refresh as? LoadState.Error
      val appendError = pagingItems?.loadState?.append as? LoadState.Error
      LaunchedEffect(refreshError, appendError) {
        if (listOfNotNull(refreshError, appendError).any {
            (it.error as? RecordReadException)?.failure == RecordReadFailure.AUTH_REQUIRED
          }) state.eventSink(BootstrapScreen.Event.RecordsAuthRequired)
      }
      BoxWithConstraints(Modifier.fillMaxSize().safeDrawingPadding()) {
        val listScrollState = rememberSaveable(saver = LazyListState.Saver) { LazyListState() }
        val detailScrollState = rememberSaveable(state.selectedRecordId, saver = LazyListState.Saver) {
          LazyListState()
        }
        val transientScrollState = rememberLazyListState()
        val scrollState = when {
          !state.canReadRecords -> transientScrollState
          state.selectedRecordId != null -> detailScrollState
          state.records is RecordListState.Ready -> listScrollState
          else -> transientScrollState
        }
        // Large text keeps a single readable column even in a wide window.
        if (maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f) {
          Row(
            Modifier.fillMaxSize().padding(24.dp),
            horizontalArrangement = Arrangement.spacedBy(48.dp, Alignment.CenterHorizontally),
            verticalAlignment = Alignment.CenterVertically,
          ) {
            BootstrapHeader(Modifier.weight(1f, fill = false).widthIn(max = 400.dp),
              compact = state.canReadRecords)
            BootstrapControls(
              state, pagingItems, scrollState, false,
              Modifier.weight(1f, fill = false).widthIn(max = 480.dp).fillMaxHeight(),
            )
          }
        } else {
          BootstrapControls(state, pagingItems, scrollState, true,
            Modifier.align(Alignment.TopCenter).widthIn(max = 560.dp).fillMaxSize())
        }
      }
    }
  }
}

@Composable
private fun BootstrapHeader(modifier: Modifier, compact: Boolean) {
  Column(modifier, verticalArrangement = Arrangement.spacedBy(16.dp)) {
    ShittimEmblem(Modifier.size(if (compact) 40.dp else 72.dp))
    Text(
      stringResource(R.string.brand_title),
      fontFamily = ShittimDisplayFont,
      style = if (compact) MaterialTheme.typography.titleMedium else MaterialTheme.typography.headlineMedium,
      color = MaterialTheme.colorScheme.primary,
    )
    Text(
      stringResource(R.string.app_name),
      style = if (compact) MaterialTheme.typography.titleLargeEmphasized
        else MaterialTheme.typography.headlineLargeEmphasized,
      color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() },
    )
    if (!compact) {
      Text(
        stringResource(R.string.bootstrap_title),
        style = MaterialTheme.typography.titleLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
      )
    }
  }
}

@Composable
private fun BootstrapControls(
  state: BootstrapScreen.State,
  pagingItems: LazyPagingItems<RecordListEntry>?,
  scrollState: LazyListState,
  showHeader: Boolean,
  modifier: Modifier,
) {
  LazyColumn(modifier.testTag("bootstrap-content"), state = scrollState,
    contentPadding = PaddingValues(if (showHeader) 24.dp else 0.dp),
    verticalArrangement = if (state.canReadRecords) Arrangement.spacedBy(24.dp)
      else Arrangement.spacedBy(32.dp, Alignment.CenterVertically),
    horizontalAlignment = Alignment.CenterHorizontally) {
    if (showHeader) item(key = "brand") {
      BootstrapHeader(Modifier.fillMaxWidth(), compact = state.canReadRecords)
    }
    item(key = "session") { SessionPanel(state.session, state.eventSink) }
    item(key = "theme") {
      BootstrapThemeSelector(state.themeChoice) {
        state.eventSink(BootstrapScreen.Event.SelectTheme(it))
      }
    }
    if (state.canReadRecords) {
      if (state.session == SessionState.Unavailable) item(key = "offline-notice") {
        Text(stringResource(R.string.record_offline))
      }
      if (state.selectedRecordId == null) recordListItems(state.records, pagingItems, state.eventSink,
        sync = if (state.session is SessionState.SignedIn) state.sync else RecordSyncState.Idle)
      else item(key = "record-detail") { RecordPreviewPanel(state.record, state.eventSink) }
    }
  }
}
