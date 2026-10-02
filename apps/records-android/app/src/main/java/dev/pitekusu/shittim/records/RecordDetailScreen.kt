package dev.pitekusu.shittim.records

import androidx.annotation.DrawableRes
import androidx.annotation.StringRes
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Icon
import androidx.compose.material3.CircularProgressIndicator
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
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.Alignment
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.LaunchedEffect
import dev.pitekusu.shittim.records.ui.ShittimSpacing
import dev.pitekusu.shittim.records.ui.participantVisualSlot
import kotlinx.coroutines.launch
import com.mikepenz.markdown.model.rememberMarkdownState
import com.mikepenz.markdown.model.State

internal enum class RecordDetailSection(@StringRes val title: Int, @DrawableRes val icon: Int) {
  Opinions(R.string.detail_opinions, R.drawable.ic_person),
  Voting(R.string.detail_voting, R.drawable.ic_detail_vote),
  Result(R.string.detail_result, R.drawable.ic_crown),
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
    var affectionChoice by rememberSaveable { mutableIntStateOf(-1) }
    // Playback belongs to this visit, not Bootstrap's lifetime-wide winner animation history.
    var affectionPlayed by rememberSaveable { mutableStateOf(arrayListOf<String>()) }
    LaunchedEffect(pager.settledPage) {
      if (RecordDetailSection.entries[pager.settledPage] != RecordDetailSection.Affection) {
        affectionPlayed = arrayListOf()
      }
    }
    val preview = (state as? RecordPreviewState.Ready)?.preview
    val decisionMarkdown = rememberMarkdownState(preview?.decision.orEmpty())
    // Live responses need not arrive in persona order; old caches can lack slots.
    val opinions = preview?.opinions.orEmpty().sortedBy {
      when (participantVisualSlot(it.participantName, it.participantSlot)) {
        "participant-a" -> 0
        "participant-b" -> 1
        "participant-c" -> 2
        else -> 3
      }
    }
    val defaultOpinion = opinions.indexOfFirst {
      voteParticipantMatches(it.participantName, it.participantSlot,
        preview?.winnerName.orEmpty(), preview?.winnerSlot)
    }.coerceAtLeast(0)
    val opinionSteps = opinions.size * 2
    // Standard lazy pages repeat the available answers in both directions without a custom
    // gesture recognizer. Keep the pager outside the tab so refresh/rotation retain selection.
    val opinionPager = if (opinionSteps == 0) null else key(opinions.map {
      participantVisualSlot(it.participantName, it.participantSlot) ?: it.participantName
    }) {
      rememberPagerState(initialPage = opinionSteps * 50 + defaultOpinion * 2,
        pageCount = { opinionSteps * 101 })
    }
    // Recenter only at rest near the window edges. The logical answer and its reading position
    // remain identical, while SDK collection/scroll indices stay bounded rather than Int.MAX_VALUE.
    LaunchedEffect(opinionPager, opinionPager?.settledPage, opinionPager?.isScrollInProgress) {
      val answers = opinionPager ?: return@LaunchedEffect
      if (!answers.isScrollInProgress && (answers.settledPage < opinionSteps ||
        answers.settledPage >= answers.pageCount - opinionSteps)) {
        answers.scrollToPage(opinionSteps * 50 + answers.settledPage % opinionSteps)
      }
    }
    val opinionStep = opinionPager?.currentPage?.rem(opinionSteps) ?: 0
    val opinionIndex = opinionStep / 2
    val finalOpinion = opinionStep % 2 == 1
    val affectionChanges = preview?.affection?.changes.orEmpty()
    val defaultAffection = affectionChanges.indexOfFirst {
      voteParticipantMatches(it.participantName, it.participantSlot,
        preview?.winnerName.orEmpty(), preview?.winnerSlot)
    }.coerceAtLeast(0)
    val affectionIndex = affectionChoice.takeIf { it in affectionChanges.indices } ?: defaultAffection
    // Keep parsed answers in memory outside lazy pages. Re-parsing an empty placeholder on return
    // would temporarily shrink the list and clamp a saved reading position back to the top.
    val opinionMarkdown = opinions.mapIndexed { index, value -> key(index) {
      listOf(rememberMarkdownState(value.initialProposal), rememberMarkdownState(value.finalProposal))
    } }
    val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
    // States remain composed outside the lazy pager, so switching pages cannot reset reading position.
    val scrollStates = List(RecordDetailSection.entries.size) { rememberLazyListState() }
    // The API has three personas; each initial/final answer has its own small saved scroll state.
    val opinionScrollStates = opinions.map { value -> key(
      participantVisualSlot(value.participantName, value.participantSlot) ?: value.participantName) {
      listOf(rememberLazyListState(), rememberLazyListState())
    } }
    val affectionScrollStates = List(3) { rememberLazyListState() }
    val questionScrollState = rememberScrollState()
    BoxWithConstraints(modifier.fillMaxSize()) {
      // The shared question stays outside the pager. Bound only its viewport, never its text,
      // so long questions and large fonts cannot consume all of the answer/navigation space.
      val questionMaximumHeight = maxHeight * 0.35f
      Column(Modifier.fillMaxSize()) {
        if (state is RecordPreviewState.Ready) {
          Surface(onClick = { questionOpen = true },
            color = MaterialTheme.colorScheme.surfaceContainer,
            shape = MaterialTheme.shapes.large,
            modifier = Modifier.fillMaxWidth().heightIn(max = questionMaximumHeight)
              .padding(horizontal = ShittimSpacing.Medium, vertical = ShittimSpacing.Small)
              .testTag("detail-question-open")) {
            Column(Modifier.verticalScroll(questionScrollState).testTag("detail-question-scroll")
              .padding(ShittimSpacing.Medium)) {
              Text(stringResource(R.string.record_question),
                style = MaterialTheme.typography.labelLarge,
                color = MaterialTheme.colorScheme.primary)
              Text(state.preview.question, style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.testTag("detail-question-text"))
            }
          }
        }
        HorizontalPager(pager, Modifier.weight(1f).fillMaxWidth().testTag("detail-pager"),
          // The opinion pager owns horizontal swipes while this tab is selected. The bottom
          // navigation still switches tabs, and other tabs retain their normal swipe behavior.
          userScrollEnabled = state is RecordPreviewState.Ready &&
            (opinionPager == null || pager.settledPage != RecordDetailSection.Opinions.ordinal)) { page ->
          val section = RecordDetailSection.entries[page]
          var affectionControlsBottom by remember { mutableFloatStateOf(Float.NEGATIVE_INFINITY) }
          val active = motionAllowed && !questionOpen && !pager.isScrollInProgress && pager.settledPage == page &&
            lifecycle.isAtLeast(Lifecycle.State.STARTED)
          if (section == RecordDetailSection.Opinions && opinionPager != null) {
            Column(Modifier.fillMaxSize()) {
              RecordOpinionControls(opinions, opinionIndex, finalOpinion,
                onPerson = { index -> scope.launch {
                  // Even tapping the already selected persona returns to their initial answer.
                  val cycleStart = opinionPager.currentPage - opinionPager.currentPage % opinionSteps
                  opinionPager.scrollToPage(cycleStart + index * 2)
                } },
                onStage = { final -> scope.launch {
                  val cycleStart = opinionPager.currentPage - opinionPager.currentPage % opinionSteps
                  opinionPager.scrollToPage(cycleStart + opinionIndex * 2 + if (final) 1 else 0)
                } })
              HorizontalPager(opinionPager, Modifier.weight(1f).fillMaxWidth().testTag("opinion-pager"),
                userScrollEnabled = !questionOpen && !pager.isScrollInProgress && pager.settledPage == page) { answerPage ->
                val answerStep = answerPage % opinionSteps
                val person = answerStep / 2
                val final = answerStep % 2 == 1
                val selectedAnswer = opinionPager.settledPage == answerPage && pager.settledPage == page
                val markdown = opinionMarkdown[person][if (final) 1 else 0]
                val parsed by markdown.state.collectAsState()
                // On restoration, an empty Markdown placeholder would clamp the saved list
                // offset to zero. Attach the real list only after local asynchronous parsing.
                if (parsed is State.Loading) {
                  Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                  }
                } else {
                  LazyColumn(Modifier.fillMaxSize()
                    .testTag(if (selectedAnswer) scrollTag else "opinion-inactive-$answerPage")
                    .then(if (selectedAnswer) Modifier else Modifier.clearAndSetSemantics {}),
                    state = opinionScrollStates[person][if (final) 1 else 0],
                    contentPadding = PaddingValues(ShittimSpacing.Medium)) {
                    item(key = "answer") {
                      RecordPreviewPanel(state, onEvent, recordId, playedSections, onSectionSeen,
                        section = section, motionActive = active && selectedAnswer && !opinionPager.isScrollInProgress,
                        opinion = opinions[person], finalOpinion = final,
                        opinionMarkdown = markdown)
                    }
                  }
                }
              }
            }
          } else {
            val scrollState = if (section == RecordDetailSection.Affection && affectionChanges.isNotEmpty())
              affectionScrollStates[affectionIndex.coerceIn(0, 2)]
              else if (section == RecordDetailSection.Result) resultScrollState
              else scrollStates[page]
            LazyColumn(Modifier.fillMaxSize().testTag(if (pager.settledPage == page) scrollTag
              else "detail-inactive-$page")
              .then(if (pager.settledPage == page) Modifier else Modifier.clearAndSetSemantics {}), state = scrollState,
              contentPadding = PaddingValues(ShittimSpacing.Medium)) {
              if (section == RecordDetailSection.Affection && affectionChanges.isNotEmpty()) stickyHeader(key = "affection") {
                RecordAffectionControls(affectionChanges, affectionIndex,
                  Modifier.onGloballyPositioned { affectionControlsBottom = it.boundsInWindow().bottom }) {
                  if (it != affectionIndex) affectionPlayed = arrayListOf()
                  affectionChoice = it
                }
              }
              item(key = section) {
                RecordPreviewPanel(state, onEvent, recordId,
                  if (section == RecordDetailSection.Affection) affectionPlayed.toSet() else playedSections,
                  onSectionSeen = { seen ->
                    if (section == RecordDetailSection.Affection && seen !in affectionPlayed) {
                      affectionPlayed = ArrayList(affectionPlayed).apply { add(seen) }
                    }
                    onSectionSeen(seen)
                  },
                  section = section, motionActive = active,
                  affectionIndex = affectionIndex,
                  minimumVisibleTop = { affectionControlsBottom },
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
    }
    if (questionOpen && state is RecordPreviewState.Ready) {
      ModalBottomSheet(onDismissRequest = { questionOpen = false },
        sheetState = rememberBottomSheetState(initialValue = SheetValue.Hidden,
          enabledValues = setOf(SheetValue.Hidden, SheetValue.Expanded)),
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
