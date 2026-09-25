package dev.pitekusu.shittim.records

import dev.pitekusu.shittim.records.auth.RECORDS_ORIGIN
import dev.pitekusu.shittim.records.auth.mobileOpaqueValue
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.accept
import io.ktor.client.request.bearerAuth
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.parameter
import io.ktor.http.ContentType
import io.ktor.http.HttpHeaders
import io.ktor.http.contentType
import io.ktor.serialization.kotlinx.json.json
import java.io.Closeable
import java.io.IOException
import java.net.URI
import java.time.Instant
import java.time.OffsetDateTime
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.CookieJar

internal enum class RecordReadFailure { AUTH_REQUIRED, NOT_FOUND, UNAVAILABLE, CURSOR_INVALID, INVALID_RESPONSE }

internal class RecordReadException(val failure: RecordReadFailure) :
  Exception("record_read_${failure.name.lowercase()}")

internal class RecordPreview(
  val question: String,
  val decision: String,
  val winnerName: String,
  val opinions: List<RecordOpinion> = emptyList(),
  val voting: RecordVoting? = null,
)

internal class RecordOpinion(
  val participantName: String,
  val summary: String,
  val initialProposal: String,
  val finalTitle: String,
  val finalProposal: String,
)

internal class RecordVoting(
  val votes: List<RecordVote>,
  val counts: List<RecordVoteCount>,
  val decidedBy: VoteDecisionMethod?,
  val legacyTieBreakApplied: Boolean,
)

internal class RecordVote(
  val voterName: String,
  val candidateName: String,
  val reason: String,
  val assessments: List<RecordAssessment>?,
)

internal class RecordVoteCount(val participantName: String, val count: Int)

internal class RecordAssessment(
  val candidateName: String,
  val entertainment: Int,
  val character: Int,
  val originality: Int,
  val responsiveness: Int,
  val interaction: Int,
  val reason: String,
  val total: Int,
)

internal enum class VoteDecisionMethod { MAJORITY, COMPOSITE_SCORE, TIE_LOTTERY }

internal sealed interface RecordReadResult {
  data object Empty : RecordReadResult
  class Found(val preview: RecordPreview) : RecordReadResult
}

internal class RecordListEntry(
  val recordId: String,
  val questionPreview: String,
  val requesterName: String,
  val requesterAvatar: RecordAvatar,
  val completedAt: Instant,
  val winnerName: String,
)

internal class RecordAvatar(val url: String?, val fallbackVariant: String)

internal class RecordListPage(val items: List<RecordListEntry>, val nextCursor: String?)

internal class RecordsReadClient(private val engine: HttpClientEngine = OkHttp.create {
  config {
    followRedirects(false)
    followSslRedirects(false)
    retryOnConnectionFailure(false)
    cookieJar(CookieJar.NO_COOKIES)
    cache(null)
  }
}) : Closeable {
  private val client = HttpClient(engine) {
    expectSuccess = false
    followRedirects = false
    install(HttpTimeout) {
      connectTimeoutMillis = 10_000
      socketTimeoutMillis = 20_000
      requestTimeoutMillis = 20_000
    }
    install(ContentNegotiation) {
      json(Json { ignoreUnknownKeys = true })
    }
  }

  suspend fun firstRecord(accessToken: String, returnTo: String): RecordReadResult {
    if (!mobileOpaqueValue.matches(accessToken)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    val requested = returnTo.removePrefix("/records/")
    val recordId = if (returnTo == "/") {
      val page: RecordPage = read("/api/v1/records?limit=1&sort=newest", accessToken)
      if (page.schemaVersion != 1 || page.items.size > 1 ||
        page.items.any { !mobileOpaqueValue.matches(it.recordId) }) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      page.items.firstOrNull()?.recordId ?: return RecordReadResult.Empty
    } else {
      if (!returnTo.startsWith("/records/") || !mobileOpaqueValue.matches(requested)) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      requested
    }
    val detail: RecordDetail = read("/api/v1/records/$recordId", accessToken)
    if (detail.schemaVersion != 2 || detail.recordId != recordId ||
      detail.question.isBlank() || detail.finalDecision.decision.isBlank() ||
      detail.result.winner != detail.finalDecision.winner ||
      detail.result.winner !in PARTICIPANTS ||
      detail.participants.size != 3 ||
      detail.participants.map { it.slot }.toSet() != PARTICIPANTS ||
      detail.participants.any { it.displayName.isBlank() } ||
      detail.initialOpinions.map { it.participant }.toSet() != PARTICIPANTS ||
      detail.initialOpinions.size != 3 ||
      detail.finalProposals.map { it.participant }.toSet() != PARTICIPANTS ||
      detail.finalProposals.size != 3 ||
      detail.initialOpinions.any { it.summary.isBlank() || it.proposal.isBlank() } ||
      detail.finalProposals.any { it.title.isBlank() || it.proposal.isBlank() }) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val winner = detail.participants.first { it.slot == detail.result.winner }
    if (winner.displayName.isBlank()) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    val voting = mapVoting(detail)
    return RecordReadResult.Found(
      RecordPreview(detail.question, detail.finalDecision.decision, winner.displayName,
        detail.participants.map { participant ->
          val initial = detail.initialOpinions.first { it.participant == participant.slot }
          val final = detail.finalProposals.first { it.participant == participant.slot }
          RecordOpinion(participant.displayName, initial.summary, initial.proposal,
            final.title, final.proposal)
        }, voting))
  }

  private fun mapVoting(detail: RecordDetail): RecordVoting {
    val names = detail.participants.associate { it.slot to it.displayName }
    val votes = detail.votes
    val counts = detail.result.voteCounts
    val legacyTieBreakApplied = detail.result.tieBreakApplied
    if (votes.size != 3 || votes.map { it.voter }.toSet() != PARTICIPANTS ||
      counts == null || counts.size != 3 || counts.map { it.participant }.toSet() != PARTICIPANTS ||
      legacyTieBreakApplied == null) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val decidedBy = when (detail.voting?.decidedBy) {
      null -> null
      "majority" -> VoteDecisionMethod.MAJORITY
      "composite_score" -> VoteDecisionMethod.COMPOSITE_SCORE
      "tie_lottery" -> VoteDecisionMethod.TIE_LOTTERY
      else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    if (detail.voting != null && detail.voting.rulesVersion != "entertainment-v1") {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val tallies = PARTICIPANTS.associateWith { slot -> votes.count { it.candidate == slot } }
    val leaders = tallies.filterValues { it == tallies.values.max() }.keys
    if (detail.result.winner !in leaders || legacyTieBreakApplied != (leaders.size > 1) ||
      votes.any { it.candidate !in PARTICIPANTS || it.candidate == it.voter ||
        it.reason.isBlank() || it.reason.length > 500 ||
        (it.assessments != null) != (decidedBy != null) } ||
      counts.any { it.count !in 0..3 || tallies[it.participant] != it.count }) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val mappedVotes = detail.participants.map { participant ->
      val vote = votes.first { it.voter == participant.slot }
      val assessments = vote.assessments?.also { items ->
        if (items.size != 2 || items.map { it.candidate }.toSet() != PARTICIPANTS - vote.voter ||
          items.any { it.reason.isBlank() || it.reason.length > 500 ||
            listOf(it.entertainment, it.character, it.originality,
            it.responsiveness, it.interaction).any { score -> score !in 0..5 } }) {
          throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
      }?.map { assessment ->
        RecordAssessment(names.getValue(assessment.candidate), assessment.entertainment,
          assessment.character, assessment.originality, assessment.responsiveness,
          assessment.interaction, assessment.reason, assessment.total)
      }
      RecordVote(participant.displayName, names.getValue(vote.candidate), vote.reason, assessments)
    }
    if (decidedBy != null) {
      var possibleWinners = leaders
      val expectedMethod = if (leaders.size == 1) VoteDecisionMethod.MAJORITY else {
        val scores = PARTICIPANTS.associateWith { slot ->
          votes.sumOf { vote -> vote.assessments.orEmpty()
            .filter { it.candidate == slot }.sumOf { it.total } }
        }
        val highestScore = leaders.maxOf { scores.getValue(it) }
        possibleWinners = leaders.filterTo(mutableSetOf()) { scores.getValue(it) == highestScore }
        if (possibleWinners.size == 1) VoteDecisionMethod.COMPOSITE_SCORE
        else VoteDecisionMethod.TIE_LOTTERY
      }
      if (decidedBy != expectedMethod || detail.result.winner !in possibleWinners) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
    }
    return RecordVoting(mappedVotes, detail.participants.map { participant ->
      RecordVoteCount(participant.displayName, counts.first { it.participant == participant.slot }.count)
    }, decidedBy, legacyTieBreakApplied)
  }

  suspend fun recentRecords(accessToken: String, cursor: String? = null): RecordListPage {
    if (!mobileOpaqueValue.matches(accessToken)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    if (cursor != null && !validCursor(cursor)) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val page: RecordListResponse = read("/api/v1/records?limit=12&sort=newest", accessToken, cursor)
    if (page.schemaVersion != 1 || page.items.size > 12 ||
      page.items.map { it.recordId }.toSet().size != page.items.size ||
      (page.nextCursor != null && (!validCursor(page.nextCursor) || page.nextCursor == cursor))) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    return try {
      RecordListPage(page.items.map { item ->
        if (item.schemaVersion != 1 || !mobileOpaqueValue.matches(item.recordId) ||
          item.questionPreview.isBlank() || item.requester.displayName.isBlank() ||
          item.participants.size != 3 ||
          item.participants.map { it.slot }.toSet() != PARTICIPANTS ||
          item.result.winner !in PARTICIPANTS) {
          throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
        val winner = item.participants.first { it.slot == item.result.winner }
        if (winner.displayName.isBlank()) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        val avatar = item.requester.avatar
        val avatarUrl = when (avatar.kind) {
          "placeholder" -> if (avatar.url == null) null else throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
          "image" -> avatar.url?.takeIf(::validAvatarUrl)
            ?: throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
          else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
        if (avatar.alt.isBlank() || avatar.fallbackVariant !in AVATAR_VARIANTS) {
          throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
        RecordListEntry(item.recordId, item.questionPreview, item.requester.displayName,
          RecordAvatar(avatarUrl, avatar.fallbackVariant),
          OffsetDateTime.parse(item.completedAt).toInstant(), winner.displayName)
      }, page.nextCursor)
    } catch (error: RecordReadException) {
      throw error
    } catch (_: Exception) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
  }

  private fun validCursor(value: String): Boolean =
    value.length <= 4096 && CURSOR_PATTERN.matches(value)

  private fun validAvatarUrl(raw: String): Boolean = try {
    val uri = URI(raw)
    uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null &&
      uri.fragment == null
  } catch (_: Exception) { false }

  private suspend inline fun <reified T> read(
    path: String, accessToken: String, cursor: String? = null,
  ): T {
    try {
      val response = client.get("$RECORDS_ORIGIN$path") {
        bearerAuth(accessToken)
        if (cursor != null) parameter("cursor", cursor)
        header(HttpHeaders.CacheControl, "no-store")
        accept(ContentType.Application.Json)
      }
      when (response.status.value) {
        200 -> Unit
        401 -> throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
        404 -> throw RecordReadException(RecordReadFailure.NOT_FOUND)
        400 -> {
          val error: RecordErrorResponse = response.body()
          throw RecordReadException(if (error.error.code == "CURSOR_INVALID")
            RecordReadFailure.CURSOR_INVALID else RecordReadFailure.INVALID_RESPONSE)
        }
        in 500..599 -> throw RecordReadException(RecordReadFailure.UNAVAILABLE)
        else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      if (response.contentType()?.withoutParameters() != ContentType.Application.Json) {
        throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      }
      return response.body()
    } catch (error: CancellationException) {
      throw error
    } catch (error: RecordReadException) {
      throw error
    } catch (_: IOException) {
      throw RecordReadException(RecordReadFailure.UNAVAILABLE)
    } catch (_: Exception) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
  }

  override fun close() {
    client.close()
    engine.close()
  }

  @Serializable private class RecordPage(val schemaVersion: Int, val items: List<RecordReference>)
  @Serializable private class RecordErrorResponse(val error: RecordErrorBody)
  @Serializable private class RecordErrorBody(val code: String)
  @Serializable private class RecordReference(val recordId: String)
  @Serializable private class RecordListResponse(
    val schemaVersion: Int,
    val items: List<RecordListItem>,
    val nextCursor: String? = null,
  )
  @Serializable private class RecordListItem(
    val schemaVersion: Int,
    val recordId: String,
    val questionPreview: String,
    val completedAt: String,
    val requester: RecordRequester,
    val participants: List<RecordParticipant>,
    val result: RecordResult,
  )
  @Serializable private class RecordRequester(val displayName: String, val avatar: RecordAvatarRef)
  @Serializable private class RecordAvatarRef(
    val kind: String,
    val url: String? = null,
    val alt: String,
    val fallbackVariant: String,
  )
  @Serializable private class RecordDetail(
    val schemaVersion: Int,
    val recordId: String,
    val question: String,
    val participants: List<RecordParticipant>,
    val initialOpinions: List<InitialOpinion>,
    val finalProposals: List<FinalProposal>,
    val votes: List<RecordVoteRef>,
    val result: RecordResult,
    val finalDecision: FinalDecision,
    val voting: VotingRef? = null,
  )
  @Serializable private class RecordParticipant(val slot: String, val displayName: String)
  @Serializable private class InitialOpinion(val participant: String, val summary: String, val proposal: String)
  @Serializable private class FinalProposal(val participant: String, val title: String, val proposal: String)
  @Serializable private class RecordVoteRef(
    val voter: String,
    val candidate: String,
    val reason: String,
    val assessments: List<RecordAssessmentRef>? = null,
  )
  @Serializable private class RecordAssessmentRef(
    val candidate: String,
    val entertainment: Int,
    val character: Int,
    val originality: Int,
    val responsiveness: Int,
    val interaction: Int,
    val reason: String,
  ) {
    val total: Int get() = entertainment * 5 + character * 5 + originality * 4 +
      responsiveness * 4 + interaction * 2
  }
  @Serializable private class VoteCountRef(val participant: String, val count: Int)
  @Serializable private class VotingRef(val rulesVersion: String, val decidedBy: String)
  @Serializable private class RecordResult(
    val winner: String,
    val voteCounts: List<VoteCountRef>? = null,
    val tieBreakApplied: Boolean? = null,
  )
  @Serializable private class FinalDecision(val winner: String, val decision: String)

  private companion object {
    val PARTICIPANTS = setOf("participant-a", "participant-b", "participant-c")
    val AVATAR_VARIANTS = setOf("cyan", "pink", "lavender")
    val CURSOR_PATTERN = Regex("[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+")
  }
}
