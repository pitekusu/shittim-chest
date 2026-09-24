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
    }
    assertEquals(listOf("/api/v1/records?limit=1&sort=newest", "/api/v1/records/$id"), paths)
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
      assertTrue(page.hasMore)
    }
    assertEquals(1, requests)
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
      "result":{"winner":"participant-a"}}],"nextCursor":"next"}"""

  private fun detail(recordId: String, winner: String = "participant-a"): String =
    """{"schemaVersion":2,"recordId":"$recordId","question":"夕飯は何がいい？",
      "participants":[{"slot":"participant-a","displayName":"アロナ"},
        {"slot":"participant-b","displayName":"プラナ"},
        {"slot":"participant-c","displayName":"安倍晋三AI"}],
      "result":{"winner":"$winner"},
      "finalDecision":{"winner":"$winner","decision":"今日は寿司にします。"}}"""
}
