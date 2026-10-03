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

private data class DetailPage(val section: RecordDetailSection, val person: Int = 0,
  val finalOpinion: Boolean = false)

private fun personaOrder(name: String, slot: String?): Int = when (participantVisualSlot(name, slot)) {
  "participant-a" -> 0
  "participant-b" -> 1
  "participant-c" -> 2
  else -> 3
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
    // The presenter reloads decrypted text after recreation. Attaching a smaller placeholder
    // pager would clamp its saved page (or ask for a now-missing page key) before Ready arrives.
    // Leave the saved reader states unconsumed until the actual finite sequence is available.
    if (state !is RecordPreviewState.Ready) {
      LazyColumn(modifier.fillMaxSize().testTag(scrollTag),
        contentPadding = PaddingValues(ShittimSpacing.Medium)) {
        item(key = "status") { RecordPreviewPanel(state, onEvent, recordId) }
      }
    } else {
      val scope = rememberCoroutineScope()
      var questionOpen by remember { mutableStateOf(false) }
      var opinionChoice by rememberSaveable { mutableIntStateOf(0) }
      var affectionChoice by rememberSaveable { mutableIntStateOf(-1) }
      // Playback belongs to this visit, not Bootstrap's lifetime-wide winner animation history.
      var affectionPlayed by rememberSaveable { mutableStateOf(arrayListOf<String>()) }
      val preview = state.preview
      val decisionMarkdown = rememberMarkdownState(preview.decision)
      // Live responses need not arrive in persona order; old caches can lack slots.
      val opinions = preview.opinions.sortedBy {
        personaOrder(it.participantName, it.participantSlot)
      }
      val affectionChanges = preview.affection?.changes.orEmpty()
      val affectionOrder = affectionChanges.indices.sortedBy {
        personaOrder(affectionChanges[it].participantName, affectionChanges[it].participantSlot)
      }
      val defaultAffection = affectionChanges.indexOfFirst {
        voteParticipantMatches(it.participantName, it.participantSlot,
          preview.winnerName, preview.winnerSlot)
      }.coerceAtLeast(0)
      val affectionIndex = affectionChoice.takeIf { it in affectionChanges.indices } ?: defaultAffection
      // One standard pager owns all horizontal gestures, including the opinion/voting boundary.
      // Its finite endpoints provide normal overscroll without loops or custom gesture handling.
      val pages = buildList {
        repeat((opinions.size * 2).coerceAtLeast(1)) { step ->
          add(DetailPage(RecordDetailSection.Opinions, step / 2, step % 2 == 1))
        }
        add(DetailPage(RecordDetailSection.Voting))
        add(DetailPage(RecordDetailSection.Result))
        if (affectionOrder.isEmpty()) add(DetailPage(RecordDetailSection.Affection))
        else affectionOrder.forEach { add(DetailPage(RecordDetailSection.Affection, it)) }
      }
      val pager = rememberPagerState(pageCount = { pages.size })
      val selected = pages[pager.currentPage.coerceIn(pages.indices)]
      var previousPage by rememberSaveable { mutableIntStateOf(pager.settledPage) }
      // Keep parsed answers in memory outside lazy pages. Re-parsing an empty placeholder on return
      // would temporarily shrink the list and clamp a saved reading position back to the top.
      val opinionMarkdown = opinions.mapIndexed { index, value -> key(index) {
        listOf(rememberMarkdownState(value.initialProposal), rememberMarkdownState(value.finalProposal))
      } }
      val lifecycle by LocalLifecycleOwner.current.lifecycle.currentStateFlow.collectAsState()
      // Result/voting positions survive tab changes; opinions restart on a deliberate answer change.
      val scrollStates = List(RecordDetailSection.entries.size) { rememberLazyListState() }
      // The API has three personas; each initial/final answer has its own small saved scroll state.
      val opinionScrollStates = opinions.map { value -> key(
        participantVisualSlot(value.participantName, value.participantSlot) ?: value.participantName) {
        listOf(rememberLazyListState(), rememberLazyListState())
      } }
      val affectionScrollStates = affectionChanges.map { value -> key(
        participantVisualSlot(value.participantName, value.participantSlot) ?: value.participantName) {
        rememberLazyListState()
      } }
      val questionScrollState = rememberScrollState()
      fun resetOpinion(page: DetailPage) {
        if (page.section == RecordDetailSection.Opinions) {
          // Incoming lazy pages may not have a first layout yet. Request the next layout's
          // position without suspending navigation until that offscreen list is attached.
          opinionScrollStates.getOrNull(page.person)?.get(if (page.finalOpinion) 1 else 0)?.requestScrollToItem(0)
        }
      }
      suspend fun openPage(index: Int, animate: Boolean = false) {
        resetOpinion(pages[index])
        if (animate) pager.animateScrollToPage(index) else pager.scrollToPage(index)
      }
      // Reset the incoming answer during the swipe, before its old offset can become visible.
      // A cancelled swipe leaves the outgoing answer untouched. Rotation/sync are not navigation.
      LaunchedEffect(pager.targetPage) {
        if (pager.targetPage != pager.settledPage) resetOpinion(pages[pager.targetPage.coerceIn(pages.indices)])
      }
      LaunchedEffect(pager.settledPage) {
        val settled = pages[pager.settledPage.coerceIn(pages.indices)]
        if (previousPage != pager.settledPage) resetOpinion(settled)
        previousPage = pager.settledPage
        if (settled.section == RecordDetailSection.Opinions) {
          opinionChoice = settled.person * 2 + if (settled.finalOpinion) 1 else 0
        }
        if (settled.section != RecordDetailSection.Affection || affectionChoice != settled.person) {
          affectionPlayed = arrayListOf()
        }
        if (settled.section == RecordDetailSection.Affection) affectionChoice = settled.person
      }
      var affectionControlsBottom by remember { mutableFloatStateOf(Float.NEGATIVE_INFINITY) }
      BoxWithConstraints(modifier.fillMaxSize()) {
        // The shared question stays outside the pager. Bound only its viewport, never its text,
        // so long questions and large fonts cannot consume all of the answer/navigation space.
        val questionMaximumHeight = maxHeight * 0.35f
        Column(Modifier.fillMaxSize()) {
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
          if (selected.section == RecordDetailSection.Opinions && opinions.isNotEmpty()) {
            RecordOpinionControls(opinions, selected.person, selected.finalOpinion,
              onPerson = { index -> scope.launch { openPage(index * 2) } },
              onStage = { final -> scope.launch { openPage(selected.person * 2 + if (final) 1 else 0) } })
          }
          if (selected.section == RecordDetailSection.Affection && affectionChanges.isNotEmpty()) {
            RecordAffectionControls(affectionOrder.map { affectionChanges[it] },
              affectionOrder.indexOf(selected.person),
              Modifier.onGloballyPositioned { affectionControlsBottom = it.boundsInWindow().bottom }) { index ->
              scope.launch { openPage(pages.indexOf(DetailPage(RecordDetailSection.Affection, affectionOrder[index]))) }
            }
          }
          Box(Modifier.weight(1f).fillMaxWidth().then(when (selected.section) {
            RecordDetailSection.Opinions -> Modifier.testTag("opinion-pager")
            RecordDetailSection.Affection -> Modifier.testTag("affection-pager")
            else -> Modifier
          })) {
            HorizontalPager(pager, Modifier.fillMaxSize().testTag("detail-pager"),
              key = { pages[it].let { entry -> "${entry.section}:${entry.person}:${entry.finalOpinion}" } },
              userScrollEnabled = !questionOpen) { page ->
              val entry = pages[page]
              val section = entry.section
              val selectedPage = pager.settledPage == page
              val active = motionAllowed && !questionOpen && !pager.isScrollInProgress && selectedPage &&
                lifecycle.isAtLeast(Lifecycle.State.STARTED)
              if (section == RecordDetailSection.Opinions && opinions.isNotEmpty()) {
                val markdown = opinionMarkdown[entry.person][if (entry.finalOpinion) 1 else 0]
                val parsed by markdown.state.collectAsState()
                // On restoration, an empty Markdown placeholder would clamp the saved list
                // offset to zero. Attach the real list only after local asynchronous parsing.
                if (parsed is State.Loading) {
                  Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                  }
                } else {
                  LazyColumn(Modifier.fillMaxSize()
                    .testTag(if (selectedPage) scrollTag else "detail-inactive-$page")
                    .then(if (selectedPage) Modifier else Modifier.clearAndSetSemantics {}),
                    state = opinionScrollStates[entry.person][if (entry.finalOpinion) 1 else 0],
                    contentPadding = PaddingValues(ShittimSpacing.Medium)) {
                    item(key = "answer") {
                      RecordPreviewPanel(state, onEvent, recordId, playedSections, onSectionSeen,
                        section = section, motionActive = active,
                        opinion = opinions[entry.person], finalOpinion = entry.finalOpinion,
                        opinionMarkdown = markdown)
                    }
                  }
                }
              } else {
                val scrollState = if (section == RecordDetailSection.Affection && affectionChanges.isNotEmpty())
                  affectionScrollStates[entry.person]
                  else if (section == RecordDetailSection.Result) resultScrollState
                  else scrollStates[section.ordinal]
                LazyColumn(Modifier.fillMaxSize().testTag(if (selectedPage) scrollTag
                  else "detail-inactive-$page")
                  .then(if (selectedPage) Modifier else Modifier.clearAndSetSemantics {}), state = scrollState,
                  contentPadding = PaddingValues(ShittimSpacing.Medium)) {
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
                      affectionIndex = entry.person,
                      minimumVisibleTop = { affectionControlsBottom },
                      decisionMarkdown = decisionMarkdown)
                  }
                }
              }
            }
          }
          // The parent already owns safe-drawing insets; don't reserve navigation-bar space twice.
          ShortNavigationBar(windowInsets = WindowInsets(0, 0, 0, 0)) {
            RecordDetailSection.entries.forEach { section ->
              ShortNavigationBarItem(selected = selected.section == section,
                onClick = { scope.launch {
                  val target = when (section) {
                    RecordDetailSection.Opinions -> opinionChoice.coerceIn(0, (opinions.size * 2 - 1).coerceAtLeast(0))
                    RecordDetailSection.Affection -> pages.indexOf(DetailPage(section, affectionIndex))
                    else -> pages.indexOf(DetailPage(section))
                  }
                  openPage(target.coerceAtLeast(0), animate = true)
                } },
                icon = { Icon(painterResource(section.icon), null, Modifier.size(24.dp)) },
                label = { Text(stringResource(section.title)) },
                modifier = Modifier.testTag("detail-section-${section.name}"))
            }
          }
        }
      }
      if (questionOpen) {
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
}
