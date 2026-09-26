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
import io.ktor.client.request.prepareGet
import io.ktor.client.statement.bodyAsChannel
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
import kotlinx.serialization.Transient
import kotlinx.serialization.json.Json
import io.ktor.utils.io.readBuffer
import kotlinx.io.readByteArray
import okhttp3.CookieJar

internal enum class RecordReadFailure { AUTH_REQUIRED, NOT_FOUND, UNAVAILABLE, CURSOR_INVALID, INVALID_RESPONSE, STORAGE_UNAVAILABLE }

internal class RecordReadException(val failure: RecordReadFailure) :
  Exception("record_read_${failure.name.lowercase()}")

@Serializable
internal class RecordPreview(
  val question: String,
  val decision: String,
  val winnerName: String,
  val opinions: List<RecordOpinion> = emptyList(),
  val voting: RecordVoting? = null,
  val victoryMessage: String? = null,
  val actions: List<String> = emptyList(),
  val caveats: List<String> = emptyList(),
  val affection: RecordAffection? = null,
)

@Serializable
internal class RecordOpinion(
  val participantName: String,
  val summary: String,
  val initialProposal: String,
  val finalTitle: String,
  val finalProposal: String,
)

@Serializable
internal class RecordVoting(
  val votes: List<RecordVote>,
  val counts: List<RecordVoteCount>,
  val decidedBy: VoteDecisionMethod?,
  val legacyTieBreakApplied: Boolean,
)

@Serializable
internal class RecordVote(
  val voterName: String,
  val candidateName: String,
  val reason: String,
  val assessments: List<RecordAssessment>?,
)

@Serializable
internal class RecordVoteCount(val participantName: String, val count: Int)

@Serializable
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

@Serializable
internal enum class VoteDecisionMethod { MAJORITY, COMPOSITE_SCORE, TIE_LOTTERY }

@Serializable
internal enum class RecordAffectionStatus { APPLIED, UNAVAILABLE }

@Serializable
internal class RecordAffection(
  val status: RecordAffectionStatus,
  val changes: List<RecordAffectionChange>,
)

@Serializable
internal class RecordAffectionChange(
  val participantName: String,
  val before: Int,
  val questionScore: Int?,
  val appliedDelta: Int,
  val after: Int,
)

internal sealed interface RecordReadResult {
  data object Empty : RecordReadResult
  class Found(val preview: RecordPreview, val listEntry: RecordListEntry? = null) : RecordReadResult
}

@Serializable
internal class RecordListEntry(
  val recordId: String,
  val questionPreview: String,
  val requesterName: String,
  val requesterAvatar: RecordAvatar,
  @Serializable(with = CachedRecordInstantSerializer::class) val completedAt: Instant,
  val winnerName: String,
)

@Serializable
internal class RecordAvatar(val url: String?, val fallbackVariant: String,
  @Transient val bytes: ByteArray? = null, val revision: String? = null)

@Serializable
internal class RecordSyncReference(val recordId: String, val revision: String, val avatarRevision: String)

@Serializable
internal class RecordSyncIndex(val schemaVersion: Int, val items: List<RecordSyncReference>,
  val nextCursor: String? = null)

internal class RecordListPage(val items: List<RecordListEntry>, val nextCursor: String?)

private val recordCursorPattern = Regex("[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+")
internal fun validRecordCursor(value: String): Boolean =
  value.length <= 4096 && recordCursorPattern.matches(value)

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
      detail.finalProposals.any { it.title.isBlank() || it.proposal.isBlank() } ||
      detail.finalDecision.victoryMessage?.let { it.isBlank() || it.codePointCount(0, it.length) > 500 } == true ||
      detail.finalDecision.actions.any { it.isBlank() } ||
      detail.finalDecision.caveats.any { it.isBlank() }) {
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
        }, voting, detail.finalDecision.victoryMessage, detail.finalDecision.actions,
        detail.finalDecision.caveats, mapAffection(detail)),
      if (detail.requester != null && detail.completedAt != null) {
        try {
          val question = detail.question.replace(Regex("[\\s\\p{Z}]+"), " ").trim()
          val end = question.offsetByCodePoints(0, minOf(160, question.codePointCount(0, question.length)))
          RecordListEntry(recordId, question.substring(0, end),
            detail.requester.displayName.also { check(it.isNotBlank()) },
            mapAvatar(detail.requester.avatar), OffsetDateTime.parse(detail.completedAt).toInstant(), winner.displayName)
        } catch (_: Exception) { throw RecordReadException(RecordReadFailure.INVALID_RESPONSE) }
      } else null)
  }

  suspend fun syncIndex(accessToken: String, cursor: String?): RecordSyncIndex {
    if (!mobileOpaqueValue.matches(accessToken)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    if (cursor != null && !validRecordCursor(cursor)) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    val index: RecordSyncIndex = read("/api/v1/records/sync-index", accessToken, cursor)
    if (index.schemaVersion != 1 || index.items.size > 50 ||
      index.items.map { it.recordId }.distinct().size != index.items.size ||
      index.items.any { !mobileOpaqueValue.matches(it.recordId) || !mobileOpaqueValue.matches(it.revision) ||
        !mobileOpaqueValue.matches(it.avatarRevision) } ||
      index.nextCursor?.let { !validRecordCursor(it) || it == cursor } == true) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    return index
  }

  private fun mapAvatar(avatar: RecordAvatarRef): RecordAvatar {
    val url = when (avatar.kind) {
      "placeholder" -> if (avatar.url == null) null else throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      "image" -> avatar.url?.takeIf(::validAvatarUrl)
        ?: throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
      else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    if (avatar.alt.isBlank() || avatar.fallbackVariant !in AVATAR_VARIANTS) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    return RecordAvatar(url, avatar.fallbackVariant)
  }

  suspend fun avatar(url: String): ByteArray? {
    // S3 is a separate, credential-free transport boundary; never attach Bearer/Cookies.
    if (!validStoredAvatarUrl(url)) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    return try {
      client.prepareGet(url) { header(HttpHeaders.CacheControl, "no-store") }.execute { response ->
        when (response.status.value) {
          404 -> null // Persist a fallback until the profile revision changes.
          200 -> {
            if (response.contentType()?.contentType != "image") {
              throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
            }
            // Bounded streaming is intentional: Content-Length alone cannot cap allocation.
            val bytes = response.bodyAsChannel().readBuffer(262_145L).readByteArray()
            if (bytes.size !in 1..262_144) throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
            bytes
          }
          in 500..599, 403, 429 -> throw RecordReadException(RecordReadFailure.UNAVAILABLE)
          else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
      }
    } catch (error: CancellationException) { throw error }
    catch (error: RecordReadException) { throw error }
    catch (_: IOException) { throw RecordReadException(RecordReadFailure.UNAVAILABLE) }
    catch (_: Exception) { throw RecordReadException(RecordReadFailure.INVALID_RESPONSE) }
  }

  private fun mapAffection(detail: RecordDetail): RecordAffection? {
    val affection = detail.affection ?: return null
    val status = when (affection.status) {
      "applied" -> RecordAffectionStatus.APPLIED
      "unavailable" -> RecordAffectionStatus.UNAVAILABLE
      else -> throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    if (affection.rubricVersion.isBlank() || affection.participants.size != 3 ||
      affection.participants.map { it.participant }.toSet() != PARTICIPANTS ||
      affection.participants.any { change ->
        change.before !in 0..1000 || change.after !in 0..1000 ||
          change.appliedDelta !in -100..100 ||
          (change.questionScore != null && change.questionScore !in -100..100) ||
          change.after - change.before != change.appliedDelta ||
          (change.questionScore == null && change.appliedDelta != 0) ||
          (change.questionScore == null) != (status == RecordAffectionStatus.UNAVAILABLE)
      }) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val names = detail.participants.associate { it.slot to it.displayName }
    return RecordAffection(status, detail.participants.map { participant ->
      val change = affection.participants.first { it.participant == participant.slot }
      RecordAffectionChange(names.getValue(participant.slot), change.before,
        change.questionScore, change.appliedDelta, change.after)
    })
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
        !validVoteReason(it.reason) ||
        (it.assessments != null) != (decidedBy != null) } ||
      counts.any { it.count !in 0..3 || tallies[it.participant] != it.count }) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val mappedVotes = detail.participants.map { participant ->
      val vote = votes.first { it.voter == participant.slot }
      val assessments = vote.assessments?.also { items ->
        if (items.size != 2 || items.map { it.candidate }.toSet() != PARTICIPANTS - vote.voter ||
          items.any { !validVoteReason(it.reason) ||
            listOf(it.entertainment, it.character, it.originality,
            it.responsiveness, it.interaction).any { score -> score !in 0..5 } }) {
          throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
        }
        val bestScore = items.maxOf { it.total }
        if (items.none { it.candidate == vote.candidate && it.total == bestScore &&
            it.reason == vote.reason }) {
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

  private fun validVoteReason(reason: String): Boolean =
    reason.isNotBlank() && reason.codePointCount(0, reason.length) <= 500

  suspend fun recentRecords(accessToken: String, cursor: String? = null): RecordListPage {
    if (!mobileOpaqueValue.matches(accessToken)) throw RecordReadException(RecordReadFailure.AUTH_REQUIRED)
    if (cursor != null && !validRecordCursor(cursor)) {
      throw RecordReadException(RecordReadFailure.INVALID_RESPONSE)
    }
    val page: RecordListResponse = read("/api/v1/records?limit=12&sort=newest", accessToken, cursor)
    if (page.schemaVersion != 1 || page.items.size > 12 ||
      page.items.map { it.recordId }.toSet().size != page.items.size ||
      (page.nextCursor != null && (!validRecordCursor(page.nextCursor) || page.nextCursor == cursor))) {
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
    val completedAt: String? = null,
    val requester: RecordRequester? = null,
    val participants: List<RecordParticipant>,
    val initialOpinions: List<InitialOpinion>,
    val finalProposals: List<FinalProposal>,
    val votes: List<RecordVoteRef>,
    val result: RecordResult,
    val finalDecision: FinalDecision,
    val affection: AffectionRef?,
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
  @Serializable private class FinalDecision(
    val winner: String,
    val decision: String,
    val actions: List<String>,
    val caveats: List<String>,
    val victoryMessage: String? = null,
  )
  @Serializable private class AffectionRef(
    val status: String,
    val rubricVersion: String,
    val participants: List<AffectionChangeRef>,
  )
  @Serializable private class AffectionChangeRef(
    val participant: String,
    val before: Int,
    val questionScore: Int?,
    val appliedDelta: Int,
    val after: Int,
  )

  private companion object {
    val PARTICIPANTS = setOf("participant-a", "participant-b", "participant-c")
    val AVATAR_VARIANTS = setOf("cyan", "pink", "lavender")
  }
}
