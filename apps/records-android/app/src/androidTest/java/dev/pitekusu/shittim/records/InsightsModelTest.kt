package dev.pitekusu.shittim.records

import androidx.lifecycle.ViewModelStore
import androidx.test.ext.junit.runners.AndroidJUnit4
import java.math.BigDecimal
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

/** Screen requests must stay lazy, keep usable snapshots, and end with the exact session. */
@RunWith(AndroidJUnit4::class)
class InsightsModelTest {
  private suspend fun await(condition: () -> Boolean) = withTimeout(5_000) {
    while (!condition()) delay(10)
  }

  private suspend fun dispose(model: InsightsModel) = withContext(Dispatchers.Main) {
    ViewModelStore().apply { put("insights", model); clear() }
  }

  @Test fun firstVisitFetchesOnlyTheVisibleResourceAndPersonaChangesReuseItsThreeRankings() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.beginVisit(online = true) }
      await { model.state.value.participants.data != null && !model.state.value.participants.loading }
      assertEquals(InsightsTab.Affection, model.state.value.tab)
      assertEquals(InsightsAffectionView.Participants, model.state.value.view)
      assertEquals("participant-a", model.state.value.participant)
      assertEquals(listOf("remote:participants"), fixture.remoteCalls())
      assertEquals(listOf("cache:participants"), fixture.cacheCalls())

      withContext(Dispatchers.Main) {
        model.event(InsightsEvent.Participant("participant-b"))
        model.event(InsightsEvent.Participant("participant-c"))
        model.open(online = true)
      }
      assertEquals("participant-c", model.state.value.participant)
      assertEquals(listOf("remote:participants"), fixture.remoteCalls())
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Tab(InsightsTab.Rankings)) }
      await { model.state.value.rankings.data != null }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Tab(InsightsTab.Affection)) }
      assertEquals("participant-c", model.state.value.participant)
      assertEquals(listOf("remote:participants", "remote:rankings"), fixture.remoteCalls())
      assertTrue(model.state.value.requesters.data == null && model.state.value.costs.isEmpty())
    } finally { dispose(model) }
  }

  @Test fun costPeriodsLoadIndependentlyAndReturningToAPeriodReusesItsResult() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Tab(InsightsTab.Costs)); model.open(true) }
      await { model.state.value.costs[InsightsPeriod.Week]?.data != null }
      assertEquals(InsightsPeriod.Week, model.state.value.period)
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Period(InsightsPeriod.Today)) }
      await { model.state.value.costs[InsightsPeriod.Today]?.data != null }
      withContext(Dispatchers.Main) {
        model.event(InsightsEvent.Period(InsightsPeriod.Week))
        model.event(InsightsEvent.Period(InsightsPeriod.Today))
      }
      assertEquals(listOf("remote:costs-week", "remote:costs-today"), fixture.remoteCalls())
      assertEquals(setOf(InsightsPeriod.Week, InsightsPeriod.Today), model.state.value.costs.keys)
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Refresh) }
      await { fixture.remoteCalls().size == 3 && model.state.value.costs[InsightsPeriod.Today]?.loading == false }
      assertEquals("remote:costs-today", fixture.remoteCalls().last())
      assertNull(model.state.value.participants.data)
    } finally { dispose(model) }
  }

  @Test fun cachedDataIsShownBeforeTheOnlineRefreshCompletes() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val cached = listOf(participantPage(score = 300))
    val fresh = listOf(participantPage(score = 700))
    val response = CompletableDeferred<List<InsightsAffectionPage>>()
    fixture.participantsCache = cached
    fixture.participantsRefresh = { response.await() }
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.beginVisit(true) }
      await { model.state.value.participants.saved && model.state.value.participants.loading }
      assertEquals(cached, model.state.value.participants.data)
      response.complete(fresh)
      await { !model.state.value.participants.loading }
      assertEquals(fresh, model.state.value.participants.data)
      assertFalse(model.state.value.participants.saved)
      assertNull(model.state.value.participants.error)
    } finally { response.complete(fresh); dispose(model) }
  }

  @Test fun recreatingTheModelOfflineReadsOnlySelectedCachedResourcesAndNeverCallsRemote() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val first = fixture.model()
    try {
      withContext(Dispatchers.Main) { first.beginVisit(true) }
      await { first.state.value.participants.data != null }
    } finally { dispose(first) }
    fixture.calls.clear()
    fixture.requestersCache = listOf(requesterPage("先頭の架空依頼者", "requester-next"),
      requesterPage("次の架空依頼者"))
    fixture.costsCache[InsightsPeriod.Week] = costs(InsightsPeriod.Week)
    fixture.rankingsCache = rankings()
    val restarted = fixture.model()
    try {
      withContext(Dispatchers.Main) { restarted.beginVisit(false) }
      await { restarted.state.value.participants.saved && !restarted.state.value.participants.loading }
      assertEquals(fixture.participantsCache, restarted.state.value.participants.data)
      assertEquals(listOf("cache:participants"), fixture.cacheCalls())
      withContext(Dispatchers.Main) { restarted.event(InsightsEvent.View(InsightsAffectionView.Requesters)) }
      await { restarted.state.value.requesters.saved }
      withContext(Dispatchers.Main) { restarted.event(InsightsEvent.NextRequester) }
      assertEquals(1, restarted.state.value.requesterPage)
      withContext(Dispatchers.Main) { restarted.event(InsightsEvent.NextRequester); restarted.event(InsightsEvent.Refresh) }
      assertEquals(1, restarted.state.value.requesterPage)
      withContext(Dispatchers.Main) { restarted.event(InsightsEvent.Tab(InsightsTab.Rankings)) }
      await { restarted.state.value.rankings.saved }
      withContext(Dispatchers.Main) { restarted.event(InsightsEvent.Tab(InsightsTab.Costs)) }
      await { restarted.state.value.costs[InsightsPeriod.Week]?.saved == true }
      assertTrue(fixture.remoteCalls().isEmpty())
      assertEquals(listOf("cache:participants", "cache:requesters", "cache:rankings", "cache:costs-week"), fixture.cacheCalls())
    } finally { dispose(restarted) }
  }

  @Test fun participantAppendFailuresKeepThePreviousGenerationUntilAnExplicitSuccessfulRefresh() = runBlocking<Unit> {
    for (failure in listOf(InsightsFailure.UNAVAILABLE, InsightsFailure.CURSOR_INVALID)) {
      val fixture = ModelFixture()
      fixture.participantsRefresh = { listOf(participantPage(nextCursor = "participant-next")) }
      fixture.participantsAppend = { cursor ->
        assertEquals("participant-next", cursor)
        throw InsightsException(failure)
      }
      val model = fixture.model()
      try {
        withContext(Dispatchers.Main) { model.beginVisit(true) }
        await { model.state.value.participants.data != null }
        val previous = model.state.value.participants.data
        withContext(Dispatchers.Main) { model.event(InsightsEvent.MoreParticipants) }
        await { model.state.value.participants.error == failure }
        assertEquals(previous, model.state.value.participants.data)
        assertFalse(model.state.value.participants.loading)
        val replacement = listOf(participantPage(score = 800))
        fixture.participantsRefresh = { replacement }
        withContext(Dispatchers.Main) { model.event(InsightsEvent.Refresh) }
        await { model.state.value.participants.data == replacement }
        assertNull(model.state.value.participants.error)
        assertEquals(1, model.state.value.participants.data!!.size)
      } finally { dispose(model) }
    }
  }

  @Test fun requesterPagesAreReusedAndFailedRefreshKeepsThePageBeforeSuccessfulReset() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val first = requesterPage("架空依頼者一人目", "requester-next")
    val second = requesterPage("架空依頼者二人目")
    fixture.requestersRefresh = { listOf(first) }
    fixture.requestersAppend = { cursor -> assertEquals("requester-next", cursor); listOf(first, second) }
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.event(InsightsEvent.View(InsightsAffectionView.Requesters)); model.open(true) }
      await { model.state.value.requesters.data != null && !model.state.value.requesters.loading }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.PreviousRequester) }
      assertEquals(0, model.state.value.requesterPage)
      withContext(Dispatchers.Main) { model.event(InsightsEvent.NextRequester) }
      await { model.state.value.requesterPage == 1 }
      withContext(Dispatchers.Main) {
        model.event(InsightsEvent.PreviousRequester)
        model.event(InsightsEvent.NextRequester)
        model.event(InsightsEvent.NextRequester)
      }
      assertEquals(1, model.state.value.requesterPage)
      assertEquals(listOf("remote:requesters", "remote:append-requesters"), fixture.remoteCalls())
      fixture.requestersRefresh = { throw InsightsException(InsightsFailure.STORAGE_UNAVAILABLE) }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Refresh) }
      await { model.state.value.requesters.error == InsightsFailure.STORAGE_UNAVAILABLE }
      assertEquals(listOf(first, second), model.state.value.requesters.data)
      assertEquals(1, model.state.value.requesterPage)
      val replacement = listOf(requesterPage("新しい世代の架空依頼者"))
      fixture.requestersRefresh = { replacement }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Refresh) }
      await { model.state.value.requesters.data == replacement && model.state.value.requesterPage == 0 }
      assertNull(model.state.value.requesters.error)
    } finally { dispose(model) }
  }

  @Test fun requesterAppendExpiryKeepsTheVisiblePageAndCanBeRetriedWithoutDuplicatingIt() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val first = requesterPage("同名の架空依頼者", "requester-next")
    val second = requesterPage("同名の架空依頼者")
    fixture.requestersRefresh = { listOf(first) }
    fixture.requestersAppend = { throw InsightsException(InsightsFailure.CURSOR_INVALID) }
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.event(InsightsEvent.View(InsightsAffectionView.Requesters)); model.open(true) }
      await { model.state.value.requesters.data != null }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.NextRequester) }
      await { model.state.value.requesters.error == InsightsFailure.CURSOR_INVALID }
      assertEquals(0, model.state.value.requesterPage)
      assertEquals(listOf(first), model.state.value.requesters.data)
      fixture.requestersAppend = { listOf(first, second) }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.NextRequester) }
      await { model.state.value.requesterPage == 1 }
      assertEquals(listOf(first, second), model.state.value.requesters.data)
      assertNull(model.state.value.requesters.error)
    } finally { dispose(model) }
  }

  @Test fun logoutDiscardsAnUncancellableLateResponseEvenIfTheOwnerBecomesActiveAgain() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val started = CompletableDeferred<Unit>()
    val release = CompletableDeferred<Unit>()
    val returned = CompletableDeferred<Unit>()
    fixture.participantsRefresh = {
      started.complete(Unit)
      withContext(NonCancellable) { release.await() }
      returned.complete(Unit)
      listOf(participantPage(score = 900))
    }
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.beginVisit(true) }
      withTimeout(5_000) { started.await() }
      withContext(Dispatchers.Main) { fixture.permitted = false; model.endSession(); fixture.permitted = true }
      assertEquals(InsightsUiState(), model.state.value)
      release.complete(Unit)
      withTimeout(5_000) { returned.await() }
      withContext(Dispatchers.Main) { assertEquals(InsightsUiState(), model.state.value) }
    } finally { release.complete(Unit); dispose(model) }
  }

  @Test fun aNewRefreshCannotBeOverwrittenByAnOlderUncancellableResponse() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val started = CompletableDeferred<Unit>()
    val release = CompletableDeferred<Unit>()
    val returned = CompletableDeferred<Unit>()
    val older = listOf(participantPage(score = 200))
    val newer = listOf(participantPage(score = 850))
    fixture.participantsRefresh = {
      started.complete(Unit)
      withContext(NonCancellable) { release.await() }
      returned.complete(Unit)
      older
    }
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.beginVisit(true) }
      withTimeout(5_000) { started.await() }
      fixture.participantsRefresh = { newer }
      withContext(Dispatchers.Main) { model.event(InsightsEvent.Refresh) }
      await { model.state.value.participants.data == newer }
      release.complete(Unit)
      withTimeout(5_000) { returned.await() }
      withContext(Dispatchers.Main) { assertEquals(newer, model.state.value.participants.data) }
    } finally { release.complete(Unit); dispose(model) }
  }

  @Test fun permissionExpiryAndRemoteAuthenticationFailureClearAllDisplayedResources() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val model = fixture.model()
    try {
      withContext(Dispatchers.Main) { model.beginVisit(true) }
      await { model.state.value.participants.data != null }
      withContext(Dispatchers.Main) { fixture.permitted = false; model.open(false) }
      assertEquals(InsightsUiState(), model.state.value)
      val calls = fixture.calls.toList()
      withContext(Dispatchers.Main) {
        model.event(InsightsEvent.Tab(InsightsTab.Costs))
        model.event(InsightsEvent.Refresh)
      }
      assertEquals(calls, fixture.calls)
    } finally { dispose(model) }

    val rejected = ModelFixture()
    rejected.rankingsRefresh = { throw InsightsException(InsightsFailure.AUTH_REQUIRED) }
    val second = rejected.model()
    try {
      withContext(Dispatchers.Main) { second.beginVisit(true) }
      await { second.state.value.participants.data != null }
      withContext(Dispatchers.Main) { second.event(InsightsEvent.Tab(InsightsTab.Rankings)) }
      await { rejected.authenticationFailures == 1 }
      assertEquals(InsightsUiState(), second.state.value)
      assertFalse(rejected.permitted)
    } finally { dispose(second) }
  }

  @Test fun aNewPermitRestoresOnlyFiniteSelectionsAndLoadsItsOwnCachedDataLazily() = runBlocking<Unit> {
    val fixture = ModelFixture()
    val firstPage = requesterPage("最初の架空依頼者", "requester-next")
    val secondPage = requesterPage("次の架空依頼者")
    fixture.requestersRefresh = { listOf(firstPage) }
    fixture.requestersAppend = { listOf(firstPage, secondPage) }
    val first = fixture.model()
    val selection = try {
      withContext(Dispatchers.Main) { first.beginVisit(true) }
      await { first.state.value.participants.data != null }
      withContext(Dispatchers.Main) {
        first.event(InsightsEvent.Participant("participant-c"))
        first.event(InsightsEvent.View(InsightsAffectionView.Requesters))
      }
      await { first.state.value.requesters.data != null }
      withContext(Dispatchers.Main) { first.event(InsightsEvent.NextRequester) }
      await { first.state.value.requesterPage == 1 }
      withContext(Dispatchers.Main) {
        first.event(InsightsEvent.Period(InsightsPeriod.Month))
        first.event(InsightsEvent.Tab(InsightsTab.Costs))
      }
      await { first.state.value.costs[InsightsPeriod.Month]?.data != null }
      first.state.value.selection().also { remembered ->
        assertEquals(InsightsSelection(InsightsTab.Costs, InsightsAffectionView.Requesters,
          "participant-c", InsightsPeriod.Month, 1), remembered)
      }
    } finally { dispose(first) }

    fixture.calls.clear()
    val verifiedAgain = fixture.model(initial = selection)
    try {
      // A fresh permit keeps display choices while decrypted state is reconstructed anew.
      assertEquals(selection, verifiedAgain.state.value.selection())
      assertNull(verifiedAgain.state.value.participants.data)
      assertNull(verifiedAgain.state.value.requesters.data)
      assertNull(verifiedAgain.state.value.rankings.data)
      assertTrue(verifiedAgain.state.value.costs.isEmpty() && fixture.calls.isEmpty())
      withContext(Dispatchers.Main) { verifiedAgain.beginVisit(false) }
      await { verifiedAgain.state.value.costs[InsightsPeriod.Month]?.saved == true }
      assertEquals(listOf("cache:costs-month"), fixture.calls)
      assertEquals(selection, verifiedAgain.state.value.selection())
      withContext(Dispatchers.Main) { verifiedAgain.event(InsightsEvent.Tab(InsightsTab.Affection)) }
      await { verifiedAgain.state.value.requesters.saved }
      assertEquals(InsightsAffectionView.Requesters, verifiedAgain.state.value.view)
      assertEquals("participant-c", verifiedAgain.state.value.participant)
      assertEquals(InsightsPeriod.Month, verifiedAgain.state.value.period)
      assertEquals(1, verifiedAgain.state.value.requesterPage)
      assertEquals(listOf(firstPage, secondPage), verifiedAgain.state.value.requesters.data)
      assertEquals(listOf("cache:costs-month", "cache:requesters"), fixture.calls)
      assertNull(verifiedAgain.state.value.participants.data)
      assertTrue(fixture.remoteCalls().isEmpty())
    } finally { dispose(verifiedAgain) }
  }

  @Test fun moreActionsProceedDuringAvatarWarmupAndItsLateResultCannotRemoveTheNewPage() = runBlocking<Unit> {
    for (view in InsightsAffectionView.entries) {
      val fixture = ModelFixture()
      val firstParticipant = participantPage(nextCursor = "participant-next")
      val secondParticipant = participantPage(score = 400).let { page ->
        page.copy(rankings = page.rankings.map { ranking ->
          ranking.copy(entries = ranking.entries.map { it.copy(rank = 2) })
        })
      }
      val firstRequester = requesterPage("最初の架空依頼者", "requester-next")
      val secondRequester = requesterPage("次の架空依頼者")
      fixture.participantsRefresh = { listOf(firstParticipant) }
      fixture.participantsAppend = { cursor ->
        assertEquals("participant-next", cursor)
        listOf(firstParticipant, secondParticipant)
      }
      fixture.requestersRefresh = { listOf(firstRequester) }
      fixture.requestersAppend = { cursor ->
        assertEquals("requester-next", cursor)
        listOf(firstRequester, secondRequester)
      }
      val warming = CompletableDeferred<Unit>()
      val release = CompletableDeferred<Unit>()
      val returned = CompletableDeferred<Unit>()
      var warms = 0
      suspend fun warmFirstPage(): Boolean {
        if (++warms != 1) return false
        warming.complete(Unit)
        withContext(NonCancellable) { release.await() }
        returned.complete(Unit)
        return true
      }
      fixture.participantsWarm = { if (warmFirstPage()) listOf(firstParticipant) else null }
      fixture.requestersWarm = { if (warmFirstPage()) listOf(firstRequester) else null }
      val model = fixture.model(initial = InsightsSelection(view = view))
      try {
        withContext(Dispatchers.Main) { model.beginVisit(true) }
        withTimeout(5_000) { warming.await() }
        val requester = view == InsightsAffectionView.Requesters
        assertFalse(if (requester) model.state.value.requesters.loading else model.state.value.participants.loading)
        withContext(Dispatchers.Main) {
          model.event(if (requester) InsightsEvent.NextRequester else InsightsEvent.MoreParticipants)
        }
        await { if (requester) model.state.value.requesters.data?.size == 2
          else model.state.value.participants.data?.size == 2 }
        assertFalse(release.isCompleted) // Paging does not wait for optional images.
        assertEquals(2, fixture.remoteCalls().size)
        release.complete(Unit)
        withTimeout(5_000) { returned.await() }
        withContext(Dispatchers.Main) {
          if (requester) {
            assertEquals(listOf(firstRequester, secondRequester), model.state.value.requesters.data)
            assertEquals(1, model.state.value.requesterPage)
          } else assertEquals(listOf(firstParticipant, secondParticipant), model.state.value.participants.data)
        }
      } finally { release.complete(Unit); dispose(model) }
    }
  }

  private class ModelFixture {
    var permitted = true
    var authenticationFailures = 0
    val calls = mutableListOf<String>()
    var participantsCache = emptyList<InsightsAffectionPage>()
    var requestersCache = emptyList<InsightsRequesterPage>()
    var rankingsCache: InsightsRankings? = null
    val costsCache = mutableMapOf<InsightsPeriod, InsightsCosts>()
    var participantsRefresh: suspend () -> List<InsightsAffectionPage> = { listOf(participantPage()) }
    var requestersRefresh: suspend () -> List<InsightsRequesterPage> = { listOf(requesterPage("架空の依頼者")) }
    var rankingsRefresh: suspend () -> InsightsRankings = { rankings() }
    var costsRefresh: suspend (InsightsPeriod) -> InsightsCosts = { costs(it) }
    var participantsAppend: suspend (String) -> List<InsightsAffectionPage> = { error("unexpected participant append") }
    var requestersAppend: suspend (String) -> List<InsightsRequesterPage> = { error("unexpected requester append") }
    fun remoteCalls() = calls.filter { it.startsWith("remote:") }
    fun cacheCalls() = calls.filter { it.startsWith("cache:") }
    var participantsWarm: suspend () -> List<InsightsAffectionPage>? = { null }
    var requestersWarm: suspend () -> List<InsightsRequesterPage>? = { null }
    fun model(initial: InsightsSelection = InsightsSelection()) = InsightsModel(
      active = { permitted },
      cachedRankings = { calls += "cache:rankings"; rankingsCache },
      cachedParticipants = { calls += "cache:participants"; participantsCache },
      cachedRequesters = { calls += "cache:requesters"; requestersCache },
      cachedCosts = { calls += "cache:costs-${it.wireValue}"; costsCache[it] },
      refreshRankings = { calls += "remote:rankings"; rankingsRefresh().also { rankingsCache = it } },
      refreshParticipants = { calls += "remote:participants"; participantsRefresh().also { participantsCache = it } },
      refreshRequesters = { calls += "remote:requesters"; requestersRefresh().also { requestersCache = it } },
      refreshCosts = { period -> calls += "remote:costs-${period.wireValue}"; costsRefresh(period).also { costsCache[period] = it } },
      appendParticipants = { calls += "remote:append-participants"; participantsAppend(it).also { participantsCache = it } },
      appendRequesters = { calls += "remote:append-requesters"; requestersAppend(it).also { requestersCache = it } },
      authenticationRequired = { authenticationFailures++; permitted = false },
      warmParticipants = { participantsWarm() },
      warmRequesters = { requestersWarm() },
      initial = initial,
    )
  }

  companion object {
    private const val DATE = "2026-10-18T00:00:00Z"
    private fun avatar() = RecordAvatar(null, "cyan")
    private fun participantPage(score: Int = 500, nextCursor: String? = null) = InsightsAffectionPage(
      generatedAt = DATE,
      rankings = INSIGHTS_PARTICIPANTS.map { participant ->
        InsightsParticipantRanking(participant, "架空の人格 $participant",
          listOf(InsightsAffectionEntry(1, "架空の依頼者", avatar(), score)))
      }, nextCursor = nextCursor)
    private fun requesterPage(name: String, nextCursor: String? = null) = InsightsRequesterPage(
      generatedAt = DATE,
      requesters = listOf(InsightsRequester(name, avatar(), INSIGHTS_PARTICIPANTS.map { participant ->
        InsightsRequesterParticipant(participant, "架空の人格 $participant", 500, 0)
      })), nextCursor = nextCursor)
    private fun rankings() = InsightsRankings(wins = listOf(InsightsCountEntry(1, "架空の人格", avatar(), 3)),
      requests = listOf(InsightsCountEntry(1, "架空の依頼者", avatar(), 2)), generatedAt = DATE)
    private fun costs(period: InsightsPeriod) = InsightsCosts(period = period, timeZone = "Asia/Tokyo",
      startDate = "2026-10-18", endDate = "2026-10-18", currency = "JPY", total = BigDecimal("1.000001"),
      breakdown = InsightsCostBreakdown(BigDecimal("1.000000"), BigDecimal("0.000001"), BigDecimal("0.000000"), BigDecimal("0.000000")),
      conversion = InsightsCostConversion("frankfurter-v2", "daily-reference-rate", "USD", DATE),
      updatedAt = DATE, status = "partial")
  }
}
