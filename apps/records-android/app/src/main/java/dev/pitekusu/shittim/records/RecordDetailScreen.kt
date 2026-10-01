package dev.pitekusu.shittim.records

import androidx.annotation.DrawableRes
import androidx.annotation.StringRes
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ShortNavigationBar
import androidx.compose.material3.ShortNavigationBarItem
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.runtime.collectAsState
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import kotlinx.coroutines.launch

internal enum class RecordDetailSection(@StringRes val title: Int, @DrawableRes val icon: Int) {
  Result(R.string.detail_result, R.drawable.ic_crown),
  Opinions(R.string.detail_opinions, R.drawable.ic_person),
  Voting(R.string.detail_voting, R.drawable.ic_detail_vote),
  Affection(R.string.detail_affection, R.drawable.ic_detail_heart),
}

/** Page state belongs to one open record, never to its refreshed response instance. */
@Composable
internal fun RecordDetailScreen(state: RecordPreviewState, recordId: String,
  onEvent: (BootstrapScreen.Event) -> Unit, modifier: Modifier = Modifier,
  resultScrollState: LazyListState = rememberLazyListState(),
  scrollTag: String = "record-detail-content", motionAllowed: Boolean = true,
  playedSections: Set<String> = emptySet(), onSectionSeen: (String) -> Unit = {}) {
  key(recordId) {
    val pager = rememberPagerState(pageCount = { RecordDetailSection.entries.size })
    val scope = rememberCoroutineScope()
    val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
    // States remain composed outside the lazy pager, so switching pages cannot reset reading position.
    val scrollStates = listOf(resultScrollState, rememberLazyListState(),
      rememberLazyListState(), rememberLazyListState())
    Column(modifier.fillMaxSize()) {
      if (state is RecordPreviewState.Ready) {
        Text(state.preview.question, maxLines = 2, overflow = TextOverflow.Ellipsis,
          style = MaterialTheme.typography.titleSmall,
          color = MaterialTheme.colorScheme.onSurface,
          modifier = Modifier.fillMaxWidth().padding(horizontal = ShittimSpacing.Medium,
            vertical = ShittimSpacing.Small))
      }
      HorizontalPager(pager, Modifier.weight(1f).fillMaxWidth().testTag("detail-pager"),
        userScrollEnabled = state is RecordPreviewState.Ready) { page ->
        val section = RecordDetailSection.entries[page]
        val active = motionAllowed && !pager.isScrollInProgress && pager.settledPage == page &&
          lifecycle.isAtLeast(Lifecycle.State.STARTED)
        LazyColumn(Modifier.fillMaxSize().testTag(if (pager.settledPage == page) scrollTag
          else "detail-inactive-$page"), state = scrollStates[page],
          contentPadding = PaddingValues(ShittimSpacing.Medium)) {
          item(key = section) {
            RecordPreviewPanel(state, onEvent, recordId, playedSections, onSectionSeen,
              section = section, motionActive = active)
          }
        }
      }
      if (state is RecordPreviewState.Ready) {
        // The parent already owns safe-drawing insets; don't reserve navigation-bar space twice.
        ShortNavigationBar(windowInsets = WindowInsets(0, 0, 0, 0)) {
          RecordDetailSection.entries.forEachIndexed { page, section ->
            ShortNavigationBarItem(selected = pager.currentPage == page,
              onClick = { scope.launch { pager.animateScrollToPage(page) } },
              icon = { Icon(painterResource(section.icon), null, Modifier.size(24.dp)) },
              label = { Text(stringResource(section.title)) },
              modifier = Modifier.testTag("detail-section-${section.name}"))
          }
        }
      }
    }
  }
}
