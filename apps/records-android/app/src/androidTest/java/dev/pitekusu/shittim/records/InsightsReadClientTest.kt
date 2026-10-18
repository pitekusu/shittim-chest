package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.math.BigDecimal
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class InsightsReadClientTest {
  private val token = "t".repeat(43)
  private val header = headersOf(HttpHeaders.ContentType, "application/json")
  private val json = Json { encodeDefaults = true }

  @Test fun endpointsUseBearerFixedPageSizesAndPreserveSameNameRequestersAndExactAmounts() = runBlocking {
    val paths = mutableListOf<String>()
    RecordsReadClient(MockEngine { request ->
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      paths += request.url.encodedPath + "?" + request.url.encodedQuery
      val payload = when {
        request.url.encodedPath.endsWith("/rankings") -> json.encodeToString(insightTestRankings())
        request.url.encodedPath.endsWith("/costs") -> json.encodeToString(insightTestCosts())
        request.url.parameters["view"] == "requesters" -> json.encodeToString(insightTestRequesters())
        else -> json.encodeToString(insightTestParticipants())
      }
      respond(payload, headers = header)
    }).use { client ->
      assertEquals(2L, client.insightsRankings(token).wins.first().count)
      assertEquals(3, client.insightsParticipants(token).rankings.size)
      assertEquals(2, client.insightsRequesters(token).requesters.size)
      assertEquals(BigDecimal("12.000001"), client.insightsCosts(token, InsightsPeriod.Week).total)
    }
    assertEquals(listOf("/api/v1/insights/rankings?", "/api/v1/insights/affection-rankings?limit=50",
      "/api/v1/insights/affection-rankings?view=requesters&limit=10", "/api/v1/insights/costs?period=week"), paths)
  }

  @Test fun distinguishesPreparationAuthenticationCursorExpiryAndTransportFailure() = runBlocking {
    listOf(
      Triple(HttpStatusCode.ServiceUnavailable, "INSIGHTS_UNAVAILABLE", InsightsFailure.PREPARING),
      Triple(HttpStatusCode.Unauthorized, "AUTH_REQUIRED", InsightsFailure.AUTH_REQUIRED),
      Triple(HttpStatusCode.BadRequest, "CURSOR_INVALID", InsightsFailure.CURSOR_INVALID),
      Triple(HttpStatusCode.ServiceUnavailable, "UPSTREAM_UNAVAILABLE", InsightsFailure.UNAVAILABLE),
    ).forEach { (status, code, expected) ->
      RecordsReadClient(MockEngine { respond("""{"error":{"code":"$code"}}""", status, header) }).use { client ->
        assertInsightsFailure(expected) { client.insightsParticipants(token) }
      }
    }
  }

  @Test fun rejectsMalformedRanksSlotsMoneyAvatarAndWrongCostPeriod() = runBlocking {
    val candidates = listOf(
      json.encodeToString(insightTestParticipants()).replace("\"rank\":1", "\"rank\":0"),
      json.encodeToString(insightTestParticipants()).replace("participant-c", "participant-b"),
      json.encodeToString(insightTestParticipants()).replace("\"schemaVersion\":1,", ""),
      json.encodeToString(insightTestParticipants()).replace("\"url\":null", "\"url\":\"https://example.invalid/avatar.png\""),
    )
    candidates.forEach { payload ->
      RecordsReadClient(MockEngine { respond(payload, headers = header) }).use { client ->
        assertInsightsFailure(InsightsFailure.INVALID_RESPONSE) { client.insightsParticipants(token) }
      }
    }
    listOf(json.encodeToString(insightTestCosts()).replace("12.000001", "1e2"),
      json.encodeToString(insightTestCosts()).replace("\"week\"", "\"today\"")).forEach { payload ->
      RecordsReadClient(MockEngine { respond(payload, headers = header) }).use { client ->
        assertInsightsFailure(InsightsFailure.INVALID_RESPONSE) { client.insightsCosts(token, InsightsPeriod.Week) }
      }
    }
  }

  @Test fun rejectsLoopingCursorWithoutIssuingAnotherRequest() = runBlocking {
    val value = insightTestParticipants(nextCursor = "cursor+/=")
    RecordsReadClient(MockEngine { request ->
      assertEquals("cursor+/=", request.url.parameters["cursor"])
      respond(json.encodeToString(value), headers = header)
    }).use { client -> assertInsightsFailure(InsightsFailure.INVALID_RESPONSE) { client.insightsParticipants(token, "cursor+/=") } }
  }
}

internal fun insightTestRankings(avatar: RecordAvatar = RecordAvatar(null, "cyan")) = InsightsRankings(
  wins = listOf(InsightsCountEntry(1, "アロナ", avatar, 2), InsightsCountEntry(1, "プラナ", avatar, 2)),
  requests = listOf(InsightsCountEntry(1, "同じ名前", avatar, 3), InsightsCountEntry(2, "同じ名前", avatar, 1)),
  generatedAt = "2026-10-18T00:00:00Z")
internal fun insightTestParticipants(generatedAt: String = "2026-10-18T00:00:00Z", rank: Int = 1,
  score: Int = 500, nextCursor: String? = null, name: String = "架空の依頼者") = InsightsAffectionPage(
  generatedAt = generatedAt, rankings = INSIGHTS_PARTICIPANTS.map { slot -> InsightsParticipantRanking(slot, slot,
    listOf(InsightsAffectionEntry(rank, name, RecordAvatar(null, "cyan"), score, 2))) }, nextCursor = nextCursor)
internal fun insightTestRequesters(generatedAt: String = "2026-10-18T00:00:00Z", nextCursor: String? = null) = InsightsRequesterPage(
  generatedAt = generatedAt, requesters = List(2) { InsightsRequester("同じ名前", RecordAvatar(null, "pink"),
    INSIGHTS_PARTICIPANTS.map { InsightsRequesterParticipant(it, it, 500, 0) }) }, nextCursor = nextCursor)
internal fun insightTestCosts(period: InsightsPeriod = InsightsPeriod.Week) = InsightsCosts(
  period = period, timeZone = "Asia/Tokyo", startDate = "2026-10-12", endDate = "2026-10-18", currency = "JPY",
  total = BigDecimal("12.000001"), breakdown = InsightsCostBreakdown(BigDecimal("1.000000"), BigDecimal("2.000000"),
    BigDecimal("9.000001"), BigDecimal("0.000000")), conversion = InsightsCostConversion("frankfurter-v2",
    "daily-reference-rate", "USD", "2026-10-18T00:00:00Z"), updatedAt = "2026-10-18T00:00:00Z", status = "partial")
internal suspend fun assertInsightsFailure(expected: InsightsFailure, block: suspend () -> Unit) {
  try { block(); fail("Expected $expected") }
  catch (error: InsightsException) { assertEquals(expected, error.failure) }
}
