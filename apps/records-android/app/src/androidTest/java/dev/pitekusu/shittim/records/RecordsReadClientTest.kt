package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class RecordsReadClientTest {
  private val id = "r".repeat(43)
  private val token = "t".repeat(43)
  private val jsonHeader = headersOf(HttpHeaders.ContentType, "application/json")

  @Test fun avatarTransportNeverSendsCredentialsAndRejectsOversizeOrRedirectedImages() = runBlocking {
    val url = "https://fictional.s3.ap-northeast-1.amazonaws.com/requesters/fictional/avatar.webp?signature=sample"
    var calls = 0
    RecordsReadClient(MockEngine { request ->
      calls++
      assertFalse(request.headers.contains(HttpHeaders.Authorization))
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      respond(ByteArray(262_145), headers = headersOf(HttpHeaders.ContentType, "image/webp"))
    }).use { client ->
      assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.avatar(url) }
      assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.avatar("https://example.invalid/avatar.png") }
    }
    assertEquals(1, calls)
    calls = 0
    RecordsReadClient(MockEngine {
      calls++
      respond("", HttpStatusCode.Found, headersOf(HttpHeaders.Location, "https://example.invalid/avatar.png"))
    }).use { client -> assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.avatar(url) } }
    assertEquals(1, calls)
    assertFalse(validStoredAvatarUrl(url.replace("/fictional/", "/../")))
  }

  @Test
  fun latestListFetchesOneDetailWithBearerAndKeepsServerWinner() = runBlocking {
    val paths = mutableListOf<String>()
    RecordsReadClient(MockEngine { request ->
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      paths += request.url.toString().removePrefix("https://shittim.pitekusu.dev")
      respond(if (paths.size == 1) page(id) else detail(id), headers = jsonHeader)
    }).use { client ->
      val result = client.firstRecord(token, "/") as RecordReadResult.Found
      assertEquals("夕飯は何がいい？", result.preview.question)
      assertEquals("アロナ", result.preview.winnerName)
      assertEquals("今日は寿司にします。", result.preview.decision)
      assertEquals(listOf("アロナ", "プラナ", "安倍晋三AI"),
        result.preview.opinions.map { it.participantName })
      assertEquals("アロナの案", result.preview.opinions.first().initialProposal)
      assertEquals("最終案A", result.preview.opinions.first().finalTitle)
      assertEquals(listOf(2, 1, 0), result.preview.voting?.counts?.map { it.count })
      assertEquals(null, result.preview.voting?.decidedBy)
      assertEquals(null, result.preview.voting?.votes?.first()?.assessments)
      assertEquals(null, result.preview.affection)
      assertEquals(null, result.preview.victoryMessage)
      assertTrue(result.preview.actions.isEmpty())
    }
    assertEquals(listOf("/api/v1/records?limit=1&sort=newest", "/api/v1/records/$id"), paths)
  }

  @Test
  fun modernVotesKeepTheSavedWinnerAndBothAssessments() = runBlocking {
    RecordsReadClient(MockEngine { respond(detail(id, modern = true), headers = jsonHeader) }).use { client ->
      val preview = (client.firstRecord(token, "/records/$id") as RecordReadResult.Found).preview
      assertEquals("アロナ", preview.winnerName)
      assertEquals(VoteDecisionMethod.MAJORITY, preview.voting?.decidedBy)
      assertEquals("アロナ", preview.voting?.votes?.get(1)?.candidateName)
      assertEquals(2, preview.voting?.votes?.first()?.assessments?.size)
      assertEquals(67, preview.voting?.votes?.first()?.assessments?.first()?.total)
    }
  }

  @Test
  fun tiedModernVotesRequireTheSavedWinnerAndMethodToMatchScores() = runBlocking {
    RecordsReadClient(MockEngine {
      respond(tiedModernDetail("participant-c", "composite_score"), headers = jsonHeader)
    }).use { client ->
      val preview = (client.firstRecord(token, "/records/$id") as RecordReadResult.Found).preview
      assertEquals("安倍晋三AI", preview.winnerName)
      assertEquals(VoteDecisionMethod.COMPOSITE_SCORE, preview.voting?.decidedBy)
    }
    for (payload in listOf(
      tiedModernDetail("participant-a", "composite_score"),
      tiedModernDetail("participant-c", "tie_lottery"),
    )) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
  }

  @Test
  fun inconsistentBallotOrAssessmentIsNotDisplayed() = runBlocking {
    for (payload in listOf(
      detail(id).replace("\"count\":2", "\"count\":1"),
      detail(id).replace("\"voter\":\"participant-c\"", "\"voter\":\"participant-a\""),
      detail(id).replace("\"candidate\":\"participant-b\"", "\"candidate\":\"participant-a\""),
      detail(id, winner = "participant-c"),
      detail(id).replace("\"tieBreakApplied\":false", "\"tieBreakApplied\":true"),
      detail(id, modern = true).replace("\"entertainment\":5", "\"entertainment\":6"),
      detail(id, modern = true).replaceFirst(
        "\"candidate\":\"participant-b\",\"entertainment\":5",
        "\"candidate\":\"participant-b\",\"entertainment\":0"),
      detail(id, modern = true).replaceFirst(
        "\"reason\":\"具体的な個性がある\"", "\"reason\":\"別の理由\""),
      detail(id, modern = true).replace("\"rulesVersion\":\"entertainment-v1\"",
        "\"rulesVersion\":\"unknown\""),
    )) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
  }

  @Test
  fun voteReasonsUseUnicodeCharacterLimits() = runBlocking {
    val validVote = detail(id).replaceFirst("具体的な投票理由", "😀".repeat(500))
    val invalidVote = detail(id).replaceFirst("具体的な投票理由", "😀".repeat(501))
    val validAssessment = detail(id, modern = true).replace("具体的な個性がある", "😀".repeat(500))
    val invalidAssessment = detail(id, modern = true).replace("具体的な個性がある", "😀".repeat(501))
    for (payload in listOf(validVote, validAssessment)) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertEquals("アロナ", (client.firstRecord(token, "/records/$id") as RecordReadResult.Found).preview.winnerName)
      }
    }
    for (payload in listOf(invalidVote, invalidAssessment)) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
  }

  @Test
  fun affectionKeepsQuestionScoreSeparateFromAppliedChangeAndShowsOptionalDecision() = runBlocking {
    val payload = detail(id)
      .replace("\"affection\":null", "\"affection\":${appliedAffection()}")
      .replace("\"actions\":[],\"caveats\":[]",
        "\"actions\":[\"まず確認する\"],\"caveats\":[\"無理をしない\"],\"victoryMessage\":\"ありがとう！\"")
    RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
      val preview = (client.firstRecord(token, "/records/$id") as RecordReadResult.Found).preview
      assertEquals("ありがとう！", preview.victoryMessage)
      assertEquals(listOf("まず確認する"), preview.actions)
      assertEquals(listOf("無理をしない"), preview.caveats)
      assertEquals(RecordAffectionStatus.APPLIED, preview.affection?.status)
      val first = preview.affection!!.changes.first()
      assertEquals("アロナ", first.participantName)
      assertEquals(50, first.questionScore)
      assertEquals(5, first.appliedDelta)
      assertEquals(1000, first.after)
    }
  }

  @Test
  fun unavailableAndInconsistentAffectionAreHandledWithoutInventingScores() = runBlocking {
    RecordsReadClient(MockEngine {
      respond(detail(id).replace("\"affection\":null", "\"affection\":${unavailableAffection()}"),
        headers = jsonHeader)
    }).use { client ->
      val affection = (client.firstRecord(token, "/records/$id") as RecordReadResult.Found).preview.affection!!
      assertEquals(RecordAffectionStatus.UNAVAILABLE, affection.status)
      assertTrue(affection.changes.all { it.questionScore == null && it.appliedDelta == 0 })
    }
    for (invalid in listOf(
      appliedAffection().replaceFirst("\"participant\":\"participant-c\"",
        "\"participant\":\"participant-a\""),
      appliedAffection().replaceFirst("\"after\":1000", "\"after\":999"),
      appliedAffection().replaceFirst("\"questionScore\":50", "\"questionScore\":null"),
      appliedAffection().replaceFirst("\"questionScore\":50", "\"questionScore\":101"),
      unavailableAffection().replaceFirst(
        "\"appliedDelta\":0,\"after\":995", "\"appliedDelta\":5,\"after\":1000"),
    )) {
      RecordsReadClient(MockEngine {
        respond(detail(id).replace("\"affection\":null", "\"affection\":$invalid"), headers = jsonHeader)
      }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
  }

  @Test
  fun blankDecisionExtrasAndOversizedVictoryMessageAreRejected() = runBlocking {
    for (payload in listOf(
      detail(id).replace("\"actions\":[]", "\"actions\":[\" \" ]"),
      detail(id).replace("\"caveats\":[]", "\"caveats\":[\" \" ]"),
      detail(id).replace("\"actions\":[]",
        "\"actions\":[],\"victoryMessage\":\"${"😀".repeat(501)}\""),
    )) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
  }

  @Test
  fun recentListUsesBearerAndReturnsRequesterCardsWithoutFetchingDetails() = runBlocking {
    var requests = 0
    RecordsReadClient(MockEngine { request ->
      requests++
      assertEquals("/api/v1/records?limit=12&sort=newest", request.url.toString()
        .removePrefix("https://shittim.pitekusu.dev"))
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      respond(listPage(), headers = jsonHeader)
    }).use { client ->
      val page = client.recentRecords(token)
      assertEquals(1, page.items.size)
      assertEquals("架空の依頼者", page.items.single().requesterName)
      assertEquals("アロナ", page.items.single().winnerName)
      assertEquals("cyan", page.items.single().requesterAvatar.fallbackVariant)
      assertEquals("next.cursor", page.nextCursor)
    }
    assertEquals(1, requests)
  }

  @Test
  fun nextPageUsesOpaqueCursorAndRejectsExpiredCursorCode() = runBlocking {
    val paths = mutableListOf<String>()
    RecordsReadClient(MockEngine { request ->
      paths += request.url.toString().removePrefix("https://shittim.pitekusu.dev")
      if (paths.size == 1) respond(listPage(), headers = jsonHeader)
      else respond("""{"error":{"code":"CURSOR_INVALID","message":"expired","requestId":"test"}}""",
        HttpStatusCode.BadRequest, jsonHeader)
    }).use { client ->
      assertEquals("next.cursor", client.recentRecords(token).nextCursor)
      assertFailure(RecordReadFailure.CURSOR_INVALID) { client.recentRecords(token, "next.cursor") }
    }
    assertEquals(listOf("/api/v1/records?limit=12&sort=newest",
      "/api/v1/records?limit=12&sort=newest&cursor=next.cursor"), paths)
  }

  @Test
  fun invalidListAvatarOrWinnerIsRejectedWithoutLeakingResponse() = runBlocking {
    for (payload in listOf(
      listPage().replace("\"kind\":\"placeholder\"", "\"kind\":\"unknown\""),
      listPage().replace("\"winner\":\"participant-a\"", "\"winner\":\"participant-x\""),
      listPage().replace("2026-09-24T00:00:00Z", "not-a-date"),
    )) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.recentRecords(token) }
      }
    }
  }

  @Test
  fun emptyListNeverRequestsDetailAndApprovedDestinationSkipsList() = runBlocking {
    var requests = 0
    RecordsReadClient(MockEngine {
      requests++
      respond(page(null), headers = jsonHeader)
    }).use { client ->
      assertEquals(RecordReadResult.Empty, client.firstRecord(token, "/"))
      assertEquals(1, requests)
    }
    RecordsReadClient(MockEngine { request ->
      assertEquals("/api/v1/records/$id", request.url.encodedPath)
      respond(detail(id), headers = jsonHeader)
    }).use { client ->
      assertTrue(client.firstRecord(token, "/records/$id") is RecordReadResult.Found)
    }
  }

  @Test
  fun mismatchedIdOrWinnerAndUnapprovedPathNeverAppearAsAValidRecord() = runBlocking {
    for (payload in listOf(
      detail("x".repeat(43)),
      detail(id, winner = "participant-c").replace(
        "\"finalDecision\":{\"winner\":\"participant-c\"",
        "\"finalDecision\":{\"winner\":\"participant-a\""),
      detail(id, winner = "participant-x"),
      detail(id).replace("\"schemaVersion\":2", "\"schemaVersion\":3"),
      detail(id).replace("\"participant\":\"participant-c\",\"title\"",
        "\"participant\":\"participant-a\",\"title\""),
    )) {
      RecordsReadClient(MockEngine { respond(payload, headers = jsonHeader) }).use { client ->
        assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/$id") }
      }
    }
    var requests = 0
    RecordsReadClient(MockEngine { requests++; respond(page(id), headers = jsonHeader) }).use { client ->
      assertFailure(RecordReadFailure.INVALID_RESPONSE) { client.firstRecord(token, "/records/../$id") }
    }
    assertEquals(0, requests)
  }

  @Test
  fun unauthorizedAndMissingRecordHaveFixedErrorsWithoutTokenOrBody() = runBlocking {
    for ((status, expected) in listOf(
      HttpStatusCode.Unauthorized to RecordReadFailure.AUTH_REQUIRED,
      HttpStatusCode.NotFound to RecordReadFailure.NOT_FOUND,
    )) {
      RecordsReadClient(MockEngine {
        respond("synthetic private response $token", status, jsonHeader)
      }).use { client ->
        try {
          client.firstRecord(token, "/records/$id")
          fail("expected fixed failure")
        } catch (error: RecordReadException) {
          assertEquals(expected, error.failure)
          assertFalse(error.message.orEmpty().contains(token))
          assertFalse(error.message.orEmpty().contains("synthetic private response"))
        }
      }
    }
  }

  private suspend fun assertFailure(expected: RecordReadFailure, call: suspend () -> Unit) {
    try {
      call()
      fail("expected invalid response")
    } catch (error: RecordReadException) {
      assertEquals(expected, error.failure)
    }
  }

  private fun page(recordId: String?): String =
    """{"schemaVersion":1,"items":${if (recordId == null) "[]" else "[{\"recordId\":\"$recordId\"}]"},"nextCursor":null}"""

  private fun listPage(): String =
    """{"schemaVersion":1,"items":[{"schemaVersion":1,"recordId":"$id",
      "questionPreview":"架空の議題","completedAt":"2026-09-24T00:00:00Z",
      "requester":{"displayName":"架空の依頼者","avatar":{"kind":"placeholder",
        "url":null,"alt":"架空の依頼者","fallbackVariant":"cyan"}},
      "participants":[{"slot":"participant-a","displayName":"アロナ"},
        {"slot":"participant-b","displayName":"プラナ"},
        {"slot":"participant-c","displayName":"安倍晋三AI"}],
      "result":{"winner":"participant-a"}}],"nextCursor":"next.cursor"}"""

  private fun detail(recordId: String, winner: String = "participant-a", modern: Boolean = false): String =
    """{"schemaVersion":2,"recordId":"$recordId","question":"夕飯は何がいい？",
      "participants":[{"slot":"participant-a","displayName":"アロナ"},
        {"slot":"participant-b","displayName":"プラナ"},
        {"slot":"participant-c","displayName":"安倍晋三AI"}],
      "initialOpinions":[{"participant":"participant-a","summary":"要約A","proposal":"アロナの案"},
        {"participant":"participant-b","summary":"要約B","proposal":"プラナの案"},
        {"participant":"participant-c","summary":"要約C","proposal":"安倍の案"}],
      "finalProposals":[{"participant":"participant-a","title":"最終案A","proposal":"決定案A"},
        {"participant":"participant-b","title":"最終案B","proposal":"決定案B"},
        {"participant":"participant-c","title":"最終案C","proposal":"決定案C"}],
      "votes":[${vote("participant-a", "participant-b", modern)},
        ${vote("participant-b", "participant-a", modern)},
        ${vote("participant-c", "participant-a", modern)}],
      "result":{"winner":"$winner","voteCounts":[{"participant":"participant-a","count":2},
        {"participant":"participant-b","count":1},{"participant":"participant-c","count":0}],
        "tieBreakApplied":false},
      "finalDecision":{"winner":"$winner","decision":"今日は寿司にします。",
        "actions":[],"caveats":[]},"affection":null
      ${if (modern) ",\"voting\":{\"rulesVersion\":\"entertainment-v1\",\"decidedBy\":\"majority\"}" else ""}}"""

  private fun appliedAffection(): String =
    """{"status":"applied","rubricVersion":"v1","participants":[
      {"participant":"participant-a","before":995,"questionScore":50,"appliedDelta":5,"after":1000},
      {"participant":"participant-b","before":500,"questionScore":-20,"appliedDelta":-20,"after":480},
      {"participant":"participant-c","before":100,"questionScore":0,"appliedDelta":0,"after":100}]}"""

  private fun unavailableAffection(): String =
    """{"status":"unavailable","rubricVersion":"v1","participants":[
      {"participant":"participant-a","before":995,"questionScore":null,"appliedDelta":0,"after":995},
      {"participant":"participant-b","before":500,"questionScore":null,"appliedDelta":0,"after":500},
      {"participant":"participant-c","before":100,"questionScore":null,"appliedDelta":0,"after":100}]}"""

  private fun vote(voter: String, candidate: String, modern: Boolean): String {
    val assessments = if (modern) {
      val otherCandidates = listOf("participant-a", "participant-b", "participant-c") - voter
      ",\"assessments\":[${otherCandidates.joinToString { assessed ->
        """{"candidate":"$assessed","entertainment":5,"character":4,"originality":3,
          "responsiveness":2,"interaction":1,"reason":"具体的な個性がある"}"""
      }}]"
    } else ""
    val reason = if (modern) "具体的な個性がある" else "具体的な投票理由"
    return """{"voter":"$voter","candidate":"$candidate","reason":"$reason"$assessments}"""
  }

  private fun tiedModernDetail(winner: String, method: String): String =
    detail(id, winner, modern = true)
      .replace(vote("participant-b", "participant-a", modern = true),
        vote("participant-b", "participant-c", modern = true).replace(
          "\"candidate\":\"participant-c\",\"entertainment\":5,\"character\":4",
          "\"candidate\":\"participant-c\",\"entertainment\":5,\"character\":5"))
      .replace("\"participant-a\",\"count\":2", "\"participant-a\",\"count\":1")
      .replace("\"participant-c\",\"count\":0", "\"participant-c\",\"count\":1")
      .replace("\"tieBreakApplied\":false", "\"tieBreakApplied\":true")
      .replace("\"decidedBy\":\"majority\"", "\"decidedBy\":\"$method\"")
}
