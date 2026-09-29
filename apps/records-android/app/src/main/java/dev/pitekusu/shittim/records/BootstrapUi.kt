package dev.pitekusu.shittim.records

import android.content.res.Configuration
import android.animation.ValueAnimator
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
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
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import androidx.paging.LoadState
import androidx.paging.compose.collectAsLazyPagingItems
import dev.pitekusu.shittim.records.ui.ShittimBackdrop
import dev.pitekusu.shittim.records.ui.ShittimDisplayFont
import dev.pitekusu.shittim.records.ui.ShittimEmblem
import dev.pitekusu.shittim.records.ui.ShittimTheme
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.auth.SessionState
import androidx.lifecycle.Lifecycle

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
      val pagingItems = (state.records as? RecordListState.Ready)?.pages?.collectAsLazyPagingItems()
      val refreshError = pagingItems?.loadState?.refresh as? LoadState.Error
      val appendError = pagingItems?.loadState?.append as? LoadState.Error
      LaunchedEffect(refreshError, appendError) {
        if (listOfNotNull(refreshError, appendError).any {
            (it.error as? RecordReadException)?.failure == RecordReadFailure.AUTH_REQUIRED
          }) state.eventSink(BootstrapScreen.Event.RecordsAuthRequired)
      }
      var menuOpen by rememberSaveable { mutableStateOf(false) }
      // Opaque record IDs only; animation state survives rotation without retaining record text.
      var playedSections by rememberSaveable { mutableStateOf(arrayListOf<String>()) }
      LaunchedEffect(state.canReadRecords) { if (!state.canReadRecords) menuOpen = false }
      Column(Modifier.fillMaxSize().safeDrawingPadding().imePadding()) {
        if (state.canReadRecords) RecordsAppBar { menuOpen = true }
        BoxWithConstraints(Modifier.weight(1f).fillMaxWidth()) {
          val listScrollState = rememberSaveable(saver = LazyListState.Saver) { LazyListState() }
          val detailScrollState = rememberSaveable(state.selectedRecordId, saver = LazyListState.Saver) {
            LazyListState()
          }
          val transientScrollState = rememberLazyListState()
          if (state.canReadRecords) {
            AdaptiveRecordsUi(state, pagingItems, listScrollState, detailScrollState,
              Modifier.fillMaxSize(), playedSections.toSet()) { key ->
                if (key !in playedSections) playedSections = ArrayList(playedSections).apply { add(key) }
              }
          } else if (maxWidth >= 840.dp && LocalDensity.current.fontScale < 1.5f) {
            Row(
              Modifier.fillMaxSize().padding(ShittimSpacing.Large),
              horizontalArrangement = Arrangement.spacedBy(48.dp, Alignment.CenterHorizontally),
              verticalAlignment = Alignment.CenterVertically,
            ) {
              BootstrapHeader(Modifier.weight(1f, fill = false).widthIn(max = 400.dp),
                compact = state.canReadRecords)
              BootstrapLoginControls(
                state, transientScrollState, false,
                Modifier.weight(1f, fill = false).widthIn(max = 480.dp).fillMaxHeight(),
              )
            }
          } else {
            BootstrapLoginControls(state, transientScrollState, true,
              Modifier.align(Alignment.TopCenter).widthIn(max = 560.dp).fillMaxSize())
          }
        }
      }
      if (menuOpen && state.canReadRecords) RecordsNavigationMenu(state, onDismiss = { menuOpen = false })
      BrandIntroOverlay(state.loginCompletion, state.session is SessionState.SignedIn,
        LocalStartupIntro.current)
    }
  }
}

@Composable
internal fun BootstrapHeader(modifier: Modifier, compact: Boolean) {
  val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
  val ringRotation = if (lifecycle.isAtLeast(Lifecycle.State.STARTED) &&
    ValueAnimator.areAnimatorsEnabled()) {
    val spin = rememberInfiniteTransition(label = "brand ring")
    val degrees by spin.animateFloat(0f, 360f,
      animationSpec = infiniteRepeatable(tween(8_000, easing = LinearEasing)),
      label = "brand ring rotation")
    degrees
  } else 0f
  Column(modifier, verticalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
    Row(verticalAlignment = Alignment.CenterVertically,
      horizontalArrangement = Arrangement.spacedBy(ShittimSpacing.Medium)) {
      ShittimEmblem(Modifier.size(if (compact) 44.dp else 72.dp), ringRotation)
      Text(
        stringResource(R.string.brand_title),
        fontFamily = ShittimDisplayFont,
        style = if (compact) MaterialTheme.typography.titleMedium else MaterialTheme.typography.headlineMedium,
        color = MaterialTheme.colorScheme.primary,
      )
    }
    Text(
      stringResource(R.string.app_name),
      style = if (compact) MaterialTheme.typography.titleLargeEmphasized
        else MaterialTheme.typography.headlineLargeEmphasized,
      color = MaterialTheme.colorScheme.onSurface,
      modifier = Modifier.semantics { heading() },
    )
  }
}

@Composable
private fun BootstrapLoginControls(
  state: BootstrapScreen.State,
  scrollState: LazyListState,
  showHeader: Boolean,
  modifier: Modifier,
) {
  LazyColumn(modifier.testTag("bootstrap-content"), state = scrollState,
    contentPadding = PaddingValues(if (showHeader) ShittimSpacing.Large else 0.dp),
    verticalArrangement = Arrangement.spacedBy(ShittimSpacing.ExtraLarge, Alignment.CenterVertically),
    horizontalAlignment = Alignment.CenterHorizontally) {
    if (showHeader) item(key = "brand") {
      BootstrapHeader(Modifier.fillMaxWidth(), compact = false)
    }
    item(key = "session") { SessionPanel(state.session, state.eventSink, canReadRecords = state.canReadRecords) }
    if (state.session !is SessionState.SignedIn) item(key = "theme") {
      BootstrapThemeSelector(state.themeChoice) { state.eventSink(BootstrapScreen.Event.SelectTheme(it)) }
    }
  }
}
