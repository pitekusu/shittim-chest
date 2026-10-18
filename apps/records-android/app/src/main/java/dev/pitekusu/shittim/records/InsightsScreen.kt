package dev.pitekusu.shittim.records

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.LazyListScope
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.material3.Button
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.PrimaryTabRow
import androidx.compose.material3.PrimaryScrollableTabRow
import androidx.compose.material3.Surface
import androidx.compose.material3.Tab
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveableStateHolder
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.tooling.preview.Preview
import androidx.compose.ui.unit.dp
import dev.pitekusu.shittim.records.ui.ShittimParticipantAvatar
import dev.pitekusu.shittim.records.ui.ShittimProgress
import dev.pitekusu.shittim.records.ui.ShittimSectionHeading
import dev.pitekusu.shittim.records.ui.ShittimTheme
import dev.pitekusu.shittim.records.ui.shittimParticipantColor
import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode
import java.text.NumberFormat
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/** Only navigation choices and list positions enter saved state; rankings stay in authorized memory. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun InsightsScreen(state: InsightsUiState, onEvent: (InsightsEvent) -> Unit,
  modifier: Modifier = Modifier, showAppBar: Boolean = true) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  val active = state.activeResource()
  val scrollStates = rememberSaveableStateHolder()
  Surface(modifier.fillMaxSize().testTag("insights-screen"), color = MaterialTheme.colorScheme.background,
    contentColor = MaterialTheme.colorScheme.onBackground) {
    Column(Modifier.fillMaxSize()) {
      if (showAppBar) InsightsAppBar(state, onEvent)
      val tabs: @Composable () -> Unit = {
        InsightsTab.entries.forEach { tab ->
          Tab(selected = state.tab == tab, onClick = { onEvent(InsightsEvent.Tab(tab)) },
            modifier = Modifier.heightIn(min = 56.dp).testTag("insights-tab-${tab.name}"),
            text = { Text(stringResource(tab.title()), maxLines = 1, softWrap = false) })
        }
      }
      if (largeText) PrimaryScrollableTabRow(selectedTabIndex = state.tab.ordinal, edgePadding = 0.dp, tabs = tabs)
      else PrimaryTabRow(selectedTabIndex = state.tab.ordinal, tabs = tabs)
      BoxWithConstraints(Modifier.fillMaxSize()) {
        val wide = maxWidth >= 840.dp && !largeText
        // Keys contain finite UI choices, never names, API cursors or decrypted response text.
        val scrollKey = when (state.tab) {
          InsightsTab.Affection -> if (state.view == InsightsAffectionView.Participants)
            "affection-person-${state.participant}" else "affection-requester-${state.requesterPage}"
          InsightsTab.Rankings -> "rankings"
          InsightsTab.Costs -> "costs-${state.period.name}"
        }
        scrollStates.SaveableStateProvider(scrollKey) {
          // Wait for decrypted cache restoration before consuming the saved list position.
          val savedListState = rememberLazyListState()
          val listState = if (active.data != null) savedListState else remember { LazyListState() }
          val generation = state.generation()?.let { "$it|${active.revision}" }
          var displayedGeneration by remember { mutableStateOf(generation) }
          LaunchedEffect(generation) {
            if (generation != null && displayedGeneration != null && generation != displayedGeneration) {
              listState.scrollToItem(0)
            }
            displayedGeneration = generation
          }
          LazyColumn(state = listState, modifier = Modifier.fillMaxSize().testTag("insights-content"),
            contentPadding = PaddingValues(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            when (state.tab) {
              InsightsTab.Affection -> affectionContent(state, wide, onEvent)
              InsightsTab.Rankings -> rankingsContent(state, wide, onEvent)
              InsightsTab.Costs -> costsContent(state, wide, onEvent)
            }
          }
        }
      }
    }
  }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
internal fun InsightsAppBar(state: InsightsUiState, onEvent: (InsightsEvent) -> Unit) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  val active = state.activeResource()
  TopAppBar(title = { Text(stringResource(R.string.insights_title),
    style = MaterialTheme.typography.titleMediumEmphasized) },
    expandedHeight = if (largeText) 112.dp else 64.dp,
    navigationIcon = {
      IconButton(onClick = { onEvent(InsightsEvent.Back) }, Modifier.testTag("insights-back")) {
        Icon(painterResource(R.drawable.ic_arrow_back), stringResource(R.string.insights_back))
      }
    }, actions = {
      IconButton(onClick = { onEvent(InsightsEvent.Refresh) }, enabled = !active.loading,
        modifier = Modifier.testTag("insights-refresh")) {
        Icon(painterResource(R.drawable.ic_refresh), stringResource(R.string.insights_refresh))
      }
    })
}

private fun InsightsTab.title(): Int = when (this) {
  InsightsTab.Affection -> R.string.insights_affection
  InsightsTab.Rankings -> R.string.insights_rankings
  InsightsTab.Costs -> R.string.insights_costs
}

private fun InsightsPeriod.title(): Int = when (this) {
  InsightsPeriod.Today -> R.string.insights_today
  InsightsPeriod.Week -> R.string.insights_week
  InsightsPeriod.Month -> R.string.insights_month
  InsightsPeriod.All -> R.string.insights_all
}

private fun InsightsUiState.activeResource(): InsightsResource<*> = when (tab) {
  InsightsTab.Affection -> if (view == InsightsAffectionView.Participants) participants else requesters
  InsightsTab.Rankings -> rankings
  InsightsTab.Costs -> costs[period] ?: InsightsResource<InsightsCosts>()
}

private fun InsightsUiState.generation(): String? = when (tab) {
  InsightsTab.Affection -> if (view == InsightsAffectionView.Participants)
    participants.data?.firstOrNull()?.generatedAt else requesters.data?.firstOrNull()?.generatedAt
  InsightsTab.Rankings -> rankings.data?.generatedAt
  InsightsTab.Costs -> costs[period]?.data?.updatedAt
}

private fun LazyListScope.affectionContent(state: InsightsUiState, wide: Boolean,
  onEvent: (InsightsEvent) -> Unit) {
  val resource = state.activeResource()
  item(key = "affection-controls") {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
      FlowRow(Modifier.fillMaxWidth().selectableGroup(),
        horizontalArrangement = Arrangement.spacedBy(8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
        InsightsAffectionView.entries.forEach { view ->
          FilterChip(selected = state.view == view, onClick = { onEvent(InsightsEvent.View(view)) },
            modifier = Modifier.heightIn(min = 48.dp).testTag("insights-view-${view.name}"),
            label = { Text(stringResource(if (view == InsightsAffectionView.Participants)
              R.string.insights_participants else R.string.insights_requesters)) })
        }
      }
      if (state.view == InsightsAffectionView.Participants) {
        ParticipantSelector(state, onEvent)
        Text(stringResource(R.string.insights_affection_guide),
          style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      } else Text(stringResource(R.string.insights_requester_guide),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
  }
  item(key = "affection-status") {
    InsightsStatus(resource, state.online, state.generation(), onEvent)
  }
  if (state.view == InsightsAffectionView.Participants) {
    val pages = state.participants.data
    val entries = pages.orEmpty().flatMap { page ->
      page.rankings.firstOrNull { it.participant == state.participant }?.entries.orEmpty()
    }
    if (pages != null && entries.isEmpty()) item(key = "empty") { EmptyInsights() }
    items(entries.size, key = { "person-row-$it" }) { index ->
      AffectionRankingCard(entries[index], state.participant)
    }
    if (pages?.lastOrNull()?.nextCursor != null) item(key = "more") {
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = { onEvent(InsightsEvent.MoreParticipants) },
          enabled = state.online && !resource.loading && resource.error != InsightsFailure.CURSOR_INVALID,
          modifier = Modifier.fillMaxWidth().heightIn(min = 48.dp).testTag("insights-more")) {
          Text(stringResource(R.string.insights_more))
        }
        if (!state.online) Text(stringResource(R.string.insights_offline_more),
          style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    }
  } else {
    val pages = state.requesters.data
    val page = pages?.getOrNull(state.requesterPage)
    if (page != null && page.requesters.isEmpty()) item(key = "empty") { EmptyInsights() }
    val rows = page?.requesters.orEmpty().chunked(if (wide) 2 else 1)
    items(rows.size, key = { "requester-row-$it" }) { index ->
      Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
        rows[index].forEach { requester -> RequesterAffectionCard(requester, Modifier.weight(1f)) }
        if (wide && rows[index].size == 1) Box(Modifier.weight(1f))
      }
    }
    if (page != null) item(key = "requester-paging") {
      RequesterPaging(state, onEvent)
    }
  }
}

private data class ParticipantChoice(val slot: String, val name: String)
private val fallbackParticipants = listOf(ParticipantChoice("participant-a", "アロナ"),
  ParticipantChoice("participant-b", "プラナ"), ParticipantChoice("participant-c", "安倍晋三AI"))

@Composable
private fun ParticipantSelector(state: InsightsUiState, onEvent: (InsightsEvent) -> Unit) {
  val choices = state.participants.data?.firstOrNull()?.rankings?.map {
    ParticipantChoice(it.participant, it.displayName)
  }?.takeIf { it.isNotEmpty() } ?: fallbackParticipants
  if (LocalDensity.current.fontScale >= 1.5f) {
    var expanded by remember { mutableStateOf(false) }
    val selected = choices.firstOrNull { it.slot == state.participant } ?: choices.first()
    val selectionDescription = stringResource(R.string.insights_participant_selected, selected.name)
    Box {
      OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)
        .testTag("insights-persona-picker").semantics { stateDescription = selectionDescription }) {
        ShittimParticipantAvatar(selected.name, selected.slot, 32.dp)
        Text(selected.name, Modifier.weight(1f).padding(horizontal = 12.dp))
        Icon(painterResource(R.drawable.ic_expand_more), stringResource(R.string.insights_choose_participant))
      }
      DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
        choices.forEach { choice ->
          DropdownMenuItem(text = { Text(choice.name) },
            onClick = { expanded = false; onEvent(InsightsEvent.Participant(choice.slot)) },
            modifier = Modifier.heightIn(min = 48.dp).testTag("insights-persona-${choice.slot}"),
            leadingIcon = { ShittimParticipantAvatar(choice.name, choice.slot, 28.dp) },
            trailingIcon = if (state.participant == choice.slot) {
              { Icon(painterResource(R.drawable.ic_check), stringResource(R.string.insights_participant_selected, choice.name)) }
            } else null)
        }
      }
    }
  } else {
    FlowRow(Modifier.fillMaxWidth().selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp),
      verticalArrangement = Arrangement.spacedBy(4.dp)) {
      choices.forEach { choice ->
        FilterChip(selected = state.participant == choice.slot,
          onClick = { onEvent(InsightsEvent.Participant(choice.slot)) },
          label = { Text(choice.name) }, leadingIcon = { ShittimParticipantAvatar(choice.name, choice.slot, 28.dp) },
          modifier = Modifier.heightIn(min = 48.dp).testTag("insights-persona-${choice.slot}"))
      }
    }
  }
}

@Composable
private fun RequesterPaging(state: InsightsUiState, onEvent: (InsightsEvent) -> Unit) {
  val pages = state.requesters.data.orEmpty()
  val nextCached = state.requesterPage + 1 < pages.size
  val hasNext = nextCached || pages.getOrNull(state.requesterPage)?.nextCursor != null
  Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
    Text(stringResource(R.string.insights_requester_page, state.requesterPage + 1),
      style = MaterialTheme.typography.labelLarge)
    FlowRow(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
      OutlinedButton(onClick = { onEvent(InsightsEvent.PreviousRequester) },
        enabled = state.requesterPage > 0 && !state.requesters.loading,
        modifier = Modifier.heightIn(min = 48.dp).testTag("insights-requester-previous")) {
        Text(stringResource(R.string.insights_previous))
      }
      Button(onClick = { onEvent(InsightsEvent.NextRequester) }, enabled = hasNext && !state.requesters.loading &&
        (nextCached || (state.online && state.requesters.error != InsightsFailure.CURSOR_INVALID)),
        modifier = Modifier.heightIn(min = 48.dp).testTag("insights-requester-next")) {
        Text(stringResource(R.string.insights_next))
      }
    }
    if (!state.online && hasNext && !nextCached) Text(stringResource(R.string.insights_offline_more),
      style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
}

@Composable
private fun AffectionRankingCard(entry: InsightsAffectionEntry, participant: String) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  InsightsCard(Modifier.testTag("insights-affection-row")) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
      RankBadge(entry.rank)
      RequesterAvatar(entry.displayName, entry.avatar, size = 40.dp)
      if (!largeText) Text(entry.displayName, style = MaterialTheme.typography.titleMediumEmphasized,
        modifier = Modifier.weight(1f))
    }
    if (largeText) Text(entry.displayName, style = MaterialTheme.typography.titleMediumEmphasized,
      modifier = Modifier.fillMaxWidth())
    ScorePresentation(entry.score, entry.resetCount, participant)
  }
}

@Composable
private fun RequesterAffectionCard(requester: InsightsRequester, modifier: Modifier = Modifier) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  InsightsCard(modifier.testTag("insights-requester-card")) {
    Row(horizontalArrangement = Arrangement.spacedBy(12.dp), verticalAlignment = Alignment.CenterVertically) {
      RequesterAvatar(requester.displayName, requester.avatar, size = 40.dp)
      if (!largeText) Text(requester.displayName, style = MaterialTheme.typography.titleMediumEmphasized,
        modifier = Modifier.weight(1f).semantics { heading() })
    }
    if (largeText) Text(requester.displayName, style = MaterialTheme.typography.titleMediumEmphasized,
      modifier = Modifier.fillMaxWidth().semantics { heading() })
    val participants = requester.participants.sortedByDescending { it.score }
    participants.forEachIndexed { index, person ->
      if (index > 0) HorizontalDivider()
      val rank = participants.indexOfFirst { it.score == person.score } + 1
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), verticalAlignment = Alignment.CenterVertically) {
          RankBadge(rank)
          ShittimParticipantAvatar(person.displayName, person.participant, 32.dp)
          if (!largeText) Text(person.displayName, style = MaterialTheme.typography.titleSmallEmphasized,
            modifier = Modifier.weight(1f))
        }
        if (largeText) Text(person.displayName, style = MaterialTheme.typography.titleSmallEmphasized,
          modifier = Modifier.fillMaxWidth())
        ScorePresentation(person.score, person.resetCount, person.participant)
      }
    }
  }
}

@Composable
private fun ScorePresentation(score: Int, resets: Int, participant: String) {
  val accent = shittimParticipantColor("", participant)
  val heartCount = score / 100
  val heartsDescription = stringResource(R.string.insights_hearts, heartCount)
  Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Text(stringResource(R.string.insights_score, score), style = MaterialTheme.typography.titleLarge,
      color = accent)
    Row(Modifier.clearAndSetSemantics { contentDescription = heartsDescription },
      horizontalArrangement = Arrangement.spacedBy(3.dp)) {
      repeat(10) { index ->
        Icon(painterResource(R.drawable.ic_detail_heart), null, Modifier.size(18.dp),
          tint = if (index < heartCount) accent else MaterialTheme.colorScheme.outlineVariant)
      }
    }
    if (resets > 0) Row(horizontalArrangement = Arrangement.spacedBy(6.dp), verticalAlignment = Alignment.CenterVertically) {
      Icon(painterResource(R.drawable.ic_crown), null, Modifier.size(20.dp), tint = accent)
      Text(stringResource(R.string.insights_crowns, resets), style = MaterialTheme.typography.labelLarge,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
  }
}

@Composable
private fun RankBadge(rank: Int) {
  Surface(shape = MaterialTheme.shapes.small,
    color = if (rank <= 3) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceContainerHigh) {
    Text(stringResource(R.string.insights_rank, rank), Modifier.padding(horizontal = 8.dp, vertical = 6.dp),
      style = MaterialTheme.typography.labelMedium)
  }
}

private fun LazyListScope.rankingsContent(state: InsightsUiState, wide: Boolean,
  onEvent: (InsightsEvent) -> Unit) {
  item(key = "rankings-status") { InsightsStatus(state.rankings, state.online, state.rankings.data?.generatedAt, onEvent) }
  val rankings = state.rankings.data ?: return
  if (rankings.wins.isEmpty() && rankings.requests.isEmpty()) {
    item(key = "empty") { EmptyInsights() }
  } else if (wide) {
    item(key = "rankings-columns") {
      Row(horizontalArrangement = Arrangement.spacedBy(16.dp), modifier = Modifier.fillMaxWidth()) {
        CountRankingCard(rankings.wins, wins = true, Modifier.weight(1f))
        CountRankingCard(rankings.requests, wins = false, Modifier.weight(1f))
      }
    }
  } else {
    item(key = "wins") { CountRankingCard(rankings.wins, wins = true) }
    item(key = "requests") { CountRankingCard(rankings.requests, wins = false) }
  }
}

@Composable
private fun CountRankingCard(entries: List<InsightsCountEntry>, wins: Boolean, modifier: Modifier = Modifier) {
  val largeText = LocalDensity.current.fontScale >= 1.5f
  val totalCount = entries.fold(BigDecimal.ZERO) { sum, entry -> sum + entry.count.toBigDecimal() }
  val total = totalCount.toDouble()
  InsightsCard(modifier) {
    ShittimSectionHeading(stringResource(if (wins) R.string.insights_wins else R.string.insights_requests))
    Text(stringResource(if (wins) R.string.insights_wins_share else R.string.insights_requests_share),
      style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    Text(stringResource(if (wins) R.string.insights_wins_total else R.string.insights_requests_total,
      NumberFormat.getIntegerInstance(Locale.JAPAN).format(totalCount)), style = MaterialTheme.typography.titleLarge)
    if (entries.isEmpty()) EmptyInsights()
    entries.forEachIndexed { index, entry ->
      if (index > 0) HorizontalDivider()
      val share = if (total > 0) entry.count.toDouble() / total else 0.0
      Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp), verticalAlignment = Alignment.CenterVertically) {
          RankBadge(entry.rank)
          if (wins) ShittimParticipantAvatar(entry.displayName, null, 36.dp)
          else RequesterAvatar(entry.displayName, entry.avatar, size = 36.dp)
          if (!largeText) Text(entry.displayName, style = MaterialTheme.typography.titleSmallEmphasized,
            modifier = Modifier.weight(1f))
        }
        if (largeText) Text(entry.displayName, style = MaterialTheme.typography.titleSmallEmphasized,
          modifier = Modifier.fillMaxWidth())
        Text(stringResource(R.string.insights_count_share,
          NumberFormat.getIntegerInstance(Locale.JAPAN).format(entry.count), formatInsightsPercent(share)),
          style = MaterialTheme.typography.titleMedium)
        LinearProgressIndicator(progress = { share.toFloat().coerceIn(0f, 1f) },
          modifier = Modifier.fillMaxWidth().clearAndSetSemantics {})
      }
    }
  }
}

private fun LazyListScope.costsContent(state: InsightsUiState, wide: Boolean,
  onEvent: (InsightsEvent) -> Unit) {
  val resource = state.costs[state.period] ?: InsightsResource()
  item(key = "cost-period") {
    FlowRow(Modifier.fillMaxWidth().selectableGroup(), horizontalArrangement = Arrangement.spacedBy(8.dp),
      verticalArrangement = Arrangement.spacedBy(4.dp)) {
      InsightsPeriod.entries.forEach { period ->
        FilterChip(selected = state.period == period, onClick = { onEvent(InsightsEvent.Period(period)) },
          label = { Text(stringResource(period.title())) },
          modifier = Modifier.heightIn(min = 48.dp).testTag("insights-period-${period.name}"))
      }
    }
  }
  item(key = "cost-status") { InsightsStatus(resource, state.online, resource.data?.updatedAt, onEvent, scheduled = false) }
  val costs = resource.data ?: return
  if (costs.status == "unavailable") {
    item(key = "cost-unavailable") { InsightsCard { Text(stringResource(R.string.insights_cost_unavailable)) } }
    return
  }
  if (wide) item(key = "cost-columns") {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
      CostSummary(costs, Modifier.weight(1f))
      CostBreakdown(costs, Modifier.weight(1f))
    }
  } else {
    item(key = "cost-summary") { CostSummary(costs) }
    item(key = "cost-breakdown") { CostBreakdown(costs) }
  }
  item(key = "cost-notes") {
    InsightsCard {
      ShittimSectionHeading(stringResource(R.string.insights_cost_note_title))
      Text(stringResource(R.string.insights_cost_note), style = MaterialTheme.typography.bodyMedium)
      Text(stringResource(R.string.insights_cost_conversion), style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
      costs.conversion.updatedAt?.let { date ->
        Text(stringResource(R.string.insights_cost_conversion_at, formatInsightsDate(date)),
          style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      }
    }
  }
}

@Composable
private fun CostSummary(costs: InsightsCosts, modifier: Modifier = Modifier) {
  InsightsCard(modifier) {
    ShittimSectionHeading(stringResource(R.string.insights_cost_total))
    Text(stringResource(if (costs.status == "partial") R.string.insights_cost_partial else R.string.insights_cost_estimate),
      style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
    Text(stringResource(R.string.insights_yen, formatInsightsYen(costs.total)),
      style = MaterialTheme.typography.headlineLarge, modifier = Modifier.testTag("insights-cost-total"))
    Text(stringResource(R.string.insights_cost_range, formatInsightsDate(costs.startDate), formatInsightsDate(costs.endDate)),
      style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
  }
}

@Composable
private fun CostBreakdown(costs: InsightsCosts, modifier: Modifier = Modifier) {
  val amounts = listOf(R.string.insights_cost_fargate to costs.breakdown.fargate,
    R.string.insights_cost_lambda to costs.breakdown.lambda, R.string.insights_cost_openai to costs.breakdown.openai,
    R.string.insights_cost_other to costs.breakdown.otherAws)
  InsightsCard(modifier) {
    ShittimSectionHeading(stringResource(R.string.insights_cost_breakdown))
    amounts.forEach { (title, amount) ->
      val share = if (costs.total.signum() > 0) amount.divide(costs.total, MathContext.DECIMAL64).toDouble() else 0.0
      Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
        Text(stringResource(title), style = MaterialTheme.typography.titleSmallEmphasized)
        Text("${stringResource(R.string.insights_yen, formatInsightsYen(amount))} · ${formatInsightsPercent(share)}",
          style = MaterialTheme.typography.bodyLarge)
        LinearProgressIndicator(progress = { share.toFloat().coerceIn(0f, 1f) },
          modifier = Modifier.fillMaxWidth().clearAndSetSemantics {})
      }
    }
  }
}

@Composable
private fun InsightsStatus(resource: InsightsResource<*>, online: Boolean, timestamp: String?,
  onEvent: (InsightsEvent) -> Unit, scheduled: Boolean = true) {
  Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
    if (resource.data != null) {
      if (resource.saved || !online) {
        Text(stringResource(R.string.insights_saved), style = MaterialTheme.typography.labelLarge,
          color = MaterialTheme.colorScheme.primary, modifier = Modifier.testTag("insights-saved"))
      }
      if (!online) Text(stringResource(R.string.insights_offline), style = MaterialTheme.typography.labelLarge)
      if (timestamp != null) Text(stringResource(R.string.insights_generated_at, formatInsightsTimestamp(timestamp)),
        style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
      if (scheduled) Text(stringResource(R.string.insights_scheduled), style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
    if (resource.loading) ShittimProgress(stringResource(if (resource.data == null)
      R.string.insights_loading else R.string.insights_updating))
    val errorMessage = when (resource.error) {
      InsightsFailure.PREPARING -> R.string.insights_preparing
      InsightsFailure.CURSOR_INVALID -> R.string.insights_cursor_expired
      InsightsFailure.AUTH_REQUIRED -> R.string.insights_auth_required
      InsightsFailure.INVALID_RESPONSE -> R.string.insights_invalid_response
      InsightsFailure.STORAGE_UNAVAILABLE -> R.string.insights_storage_failed
      InsightsFailure.UNAVAILABLE -> if (resource.data == null) R.string.insights_error else R.string.insights_refresh_failed
      null -> if (resource.data == null && !resource.loading && !online) R.string.insights_offline_missing else null
    }
    if (errorMessage != null) {
      Surface(shape = MaterialTheme.shapes.medium, color = MaterialTheme.colorScheme.surfaceContainerHigh) {
        Column(Modifier.fillMaxWidth().padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
          Text(stringResource(errorMessage), modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            style = MaterialTheme.typography.bodyMedium)
          if (resource.error != InsightsFailure.AUTH_REQUIRED && !resource.loading) {
            TextButton(onClick = { onEvent(InsightsEvent.Refresh) }, modifier = Modifier.heightIn(min = 48.dp)) {
              Text(stringResource(if (resource.error == InsightsFailure.CURSOR_INVALID)
                R.string.insights_reload else R.string.insights_retry))
            }
          }
        }
      }
    }
  }
}

@Composable
private fun EmptyInsights() {
  Text(stringResource(R.string.insights_empty), style = MaterialTheme.typography.bodyMedium,
    color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun InsightsCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  Surface(modifier.fillMaxWidth(), shape = MaterialTheme.shapes.large,
    color = MaterialTheme.colorScheme.surfaceContainerLow) {
    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(16.dp), content = content)
  }
}

internal fun formatInsightsYen(value: BigDecimal): String =
  NumberFormat.getIntegerInstance(Locale.JAPAN).format(value.setScale(0, RoundingMode.CEILING))

private fun formatInsightsPercent(value: Double): String = NumberFormat.getPercentInstance(Locale.JAPAN).apply {
  maximumFractionDigits = 1
}.format(value)

private fun formatInsightsTimestamp(value: String): String = runCatching {
  DateTimeFormatter.ofPattern("yyyy/MM/dd HH:mm", Locale.JAPAN).withZone(ZoneId.of("Asia/Tokyo"))
    .format(Instant.parse(value))
}.getOrDefault("—")

private fun formatInsightsDate(value: String): String = runCatching {
  LocalDate.parse(value).format(DateTimeFormatter.ofPattern("yyyy/MM/dd", Locale.JAPAN))
}.getOrElse { formatInsightsTimestamp(value) }

@Preview(name = "Insights", widthDp = 360, heightDp = 800)
@Preview(name = "Insights large text", widthDp = 320, heightDp = 900, fontScale = 2f)
@Composable
private fun InsightsPreview() {
  ShittimTheme { InsightsScreen(insightsSampleState(), {}) }
}
