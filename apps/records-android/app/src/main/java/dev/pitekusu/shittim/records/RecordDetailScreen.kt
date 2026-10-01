package dev.pitekusu.shittim.records

import androidx.annotation.DrawableRes
import androidx.annotation.StringRes
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
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
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Surface
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberBottomSheetState
import androidx.compose.material3.SheetValue
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ShortNavigationBar
import androidx.compose.material3.ShortNavigationBarItem
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
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
import com.mikepenz.markdown.model.rememberMarkdownState

internal enum class RecordDetailSection(@StringRes val title: Int, @DrawableRes val icon: Int) {
  Result(R.string.detail_result, R.drawable.ic_crown),
  Opinions(R.string.detail_opinions, R.drawable.ic_person),
  Voting(R.string.detail_voting, R.drawable.ic_detail_vote),
  Affection(R.string.detail_affection, R.drawable.ic_detail_heart),
}

/** Page state belongs to one open record, never to its refreshed response instance. */
@Composable
@OptIn(ExperimentalMaterial3Api::class)
internal fun RecordDetailScreen(state: RecordPreviewState, recordId: String,
  onEvent: (BootstrapScreen.Event) -> Unit, modifier: Modifier = Modifier,
  resultScrollState: LazyListState = rememberLazyListState(),
  scrollTag: String = "record-detail-content", motionAllowed: Boolean = true,
  playedSections: Set<String> = emptySet(), onSectionSeen: (String) -> Unit = {}) {
  key(recordId) {
    val pager = rememberPagerState(pageCount = { RecordDetailSection.entries.size })
    val scope = rememberCoroutineScope()
    var questionOpen by remember { mutableStateOf(false) }
    var opinionChoice by rememberSaveable { mutableIntStateOf(-1) }
    var finalOpinion by rememberSaveable { mutableStateOf(true) }
    val preview = (state as? RecordPreviewState.Ready)?.preview
    val decisionMarkdown = rememberMarkdownState(preview?.decision.orEmpty())
    val opinions = preview?.opinions.orEmpty()
    val defaultOpinion = opinions.indexOfFirst {
      voteParticipantMatches(it.participantName, it.participantSlot,
        preview?.winnerName.orEmpty(), preview?.winnerSlot)
    }.coerceAtLeast(0)
    val opinionIndex = opinionChoice.takeIf { it in opinions.indices } ?: defaultOpinion
    val opinion = opinions.getOrNull(opinionIndex)
    // Keep parsed answers in memory outside lazy pages. Re-parsing an empty placeholder on return
    // would temporarily shrink the list and clamp a saved reading position back to the top.
    val opinionMarkdown = opinions.mapIndexed { index, value -> key(index) {
      listOf(rememberMarkdownState(value.initialProposal), rememberMarkdownState(value.finalProposal))
    } }
    val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
    // States remain composed outside the lazy pager, so switching pages cannot reset reading position.
    val scrollStates = listOf(resultScrollState, rememberLazyListState(),
      rememberLazyListState(), rememberLazyListState())
    // The API has three personas; each initial/final answer has its own small saved scroll state.
    val opinionScrollStates = List(3) { listOf(rememberLazyListState(), rememberLazyListState()) }
    Column(modifier.fillMaxSize()) {
      if (state is RecordPreviewState.Ready) {
        Surface(onClick = { questionOpen = true }, color = MaterialTheme.colorScheme.surfaceContainer,
          modifier = Modifier.fillMaxWidth().testTag("detail-question-open")) {
          Row(Modifier.padding(horizontal = ShittimSpacing.Medium, vertical = ShittimSpacing.Small),
            verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
        Text(state.preview.question, maxLines = 2, overflow = TextOverflow.Ellipsis,
          style = MaterialTheme.typography.titleSmall,
          color = MaterialTheme.colorScheme.onSurface,
          modifier = Modifier.weight(1f))
            Icon(painterResource(R.drawable.ic_open_web), stringResource(R.string.detail_question_full),
              Modifier.padding(start = 8.dp).size(20.dp))
          }
        }
      }
      HorizontalPager(pager, Modifier.weight(1f).fillMaxWidth().testTag("detail-pager"),
        userScrollEnabled = state is RecordPreviewState.Ready) { page ->
        val section = RecordDetailSection.entries[page]
        val active = motionAllowed && !questionOpen && !pager.isScrollInProgress && pager.settledPage == page &&
          lifecycle.isAtLeast(Lifecycle.State.STARTED)
        Column(Modifier.fillMaxSize()) {
        if (section == RecordDetailSection.Opinions && opinion != null) {
          RecordOpinionControls(opinions, opinionIndex, finalOpinion,
            onPerson = { opinionChoice = it }, onStage = { finalOpinion = it })
        }
        val scrollState = if (section == RecordDetailSection.Opinions && opinion != null)
          opinionScrollStates[opinionIndex.coerceIn(0, 2)][if (finalOpinion) 1 else 0]
          else scrollStates[page]
        LazyColumn(Modifier.weight(1f).fillMaxWidth().testTag(if (pager.settledPage == page) scrollTag
          else "detail-inactive-$page"), state = scrollState,
          contentPadding = PaddingValues(ShittimSpacing.Medium)) {
          item(key = section) {
            RecordPreviewPanel(state, onEvent, recordId, playedSections, onSectionSeen,
              section = section, motionActive = active, opinion = opinion, finalOpinion = finalOpinion,
              opinionMarkdown = opinionMarkdown.getOrNull(opinionIndex)?.get(if (finalOpinion) 1 else 0),
              decisionMarkdown = decisionMarkdown)
          }
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
    if (questionOpen && state is RecordPreviewState.Ready) {
      ModalBottomSheet(onDismissRequest = { questionOpen = false },
        sheetState = rememberBottomSheetState(initialValue = SheetValue.Hidden),
        modifier = Modifier.testTag("detail-question-sheet")) {
        LazyColumn(contentPadding = PaddingValues(ShittimSpacing.Medium)) {
          item { Text(stringResource(R.string.record_question),
            style = MaterialTheme.typography.titleLargeEmphasized) }
          item { RecordMarkdown(state.preview.question) }
          item { TextButton(onClick = { questionOpen = false }) {
            Text(stringResource(R.string.detail_close))
          } }
        }
      }
    }
  }
}
