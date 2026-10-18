package dev.pitekusu.shittim.records

import android.content.Context
import dev.pitekusu.shittim.records.auth.CacheAuthorization

/** Bind every request, cache read and late response to this exact verified session permit. */
internal fun createInsightsModel(context: Context, runtime: RecordsRuntime, owner: String,
  permit: CacheAuthorization?, initial: InsightsSelection = InsightsSelection()): InsightsModel {
  val active = { runtime.isActive(owner) && (runtime.demo != null || runtime.session?.cachePermit?.value === permit) }
  if (runtime.demo != null) {
    // The demo only constructs local fixtures. It never instantiates a production HTTP client/store.
    val sample = insightsSampleState()
    fun guard() { if (!active()) throw InsightsException(InsightsFailure.AUTH_REQUIRED) }
    return InsightsModel(active,
      cachedRankings = { guard(); sample.rankings.data },
      cachedParticipants = { guard(); sample.participants.data.orEmpty() },
      cachedRequesters = { guard(); sample.requesters.data.orEmpty() },
      cachedCosts = { guard(); sample.costs[it]?.data },
      refreshRankings = { guard(); requireNotNull(sample.rankings.data) },
      refreshParticipants = { guard(); sample.participants.data.orEmpty() },
      refreshRequesters = { guard(); sample.requesters.data.orEmpty() },
      refreshCosts = { guard(); requireNotNull(sample.costs[it]?.data) },
      appendParticipants = { guard(); sample.participants.data.orEmpty() },
      appendRequesters = { guard(); sample.requesters.data.orEmpty() }, initial = initial)
  }
  val repository = lazy { InsightsRepository.open(context.applicationContext) { account -> account == owner && active() } }
  suspend fun <T> authorized(read: suspend (InsightsRepository, String) -> T): T {
    if (!active()) throw InsightsException(InsightsFailure.AUTH_REQUIRED)
    if (!hasValidatedDebateNetwork(context)) throw InsightsException(InsightsFailure.UNAVAILABLE)
    return runtime.session?.withAuthorizedToken { token ->
      if (!active()) throw InsightsException(InsightsFailure.AUTH_REQUIRED)
      read(repository.value, token)
    } ?: throw InsightsException(if (active()) InsightsFailure.UNAVAILABLE else InsightsFailure.AUTH_REQUIRED)
  }
  return InsightsModel(active,
    cachedRankings = { repository.value.cachedRankings(owner)?.value },
    cachedParticipants = { repository.value.cachedParticipants(owner).map { it.value } },
    cachedRequesters = { repository.value.cachedRequesters(owner).map { it.value } },
    cachedCosts = { repository.value.cachedCosts(owner, it)?.value },
    refreshRankings = { authorized { repo, token -> repo.refreshRankings(token, owner).value } },
    refreshParticipants = { authorized { repo, token -> repo.refreshParticipants(token, owner).map { it.value } } },
    refreshRequesters = { authorized { repo, token -> repo.refreshRequesters(token, owner).map { it.value } } },
    refreshCosts = { period -> authorized { repo, token -> repo.refreshCosts(token, owner, period).value } },
    appendParticipants = { cursor -> authorized { repo, token -> repo.appendParticipants(token, owner, cursor).map { it.value } } },
    appendRequesters = { cursor -> authorized { repo, token -> repo.appendRequesters(token, owner, cursor).map { it.value } } },
    authenticationRequired = { if (active()) runtime.session?.onAuthenticationRequired() },
    close = { if (repository.isInitialized()) repository.value.close() },
    warmRankings = { repository.value.warmRankingsAvatars(owner); repository.value.cachedRankings(owner)?.value },
    warmParticipants = { repository.value.warmParticipantsAvatars(owner); repository.value.cachedParticipants(owner).map { it.value } },
    warmRequesters = { repository.value.warmRequestersAvatars(owner); repository.value.cachedRequesters(owner).map { it.value } },
    initial = initial)
}
