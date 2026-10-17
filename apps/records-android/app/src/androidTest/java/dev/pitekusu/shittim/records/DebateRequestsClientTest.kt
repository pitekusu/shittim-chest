package dev.pitekusu.shittim.records

import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import io.ktor.http.content.OutgoingContent
import kotlinx.coroutines.runBlocking
import kotlinx.io.readByteArray
import io.ktor.utils.io.readBuffer
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.IOException

@RunWith(AndroidJUnit4::class)
class DebateRequestsClientTest {
  private val id = "11111111-2222-4333-8444-abcdefabcdef"
  private val token = "t".repeat(43)
  private val operation = "99999999-2222-4333-8444-abcdefabcdef"
  private val attempt = "a".repeat(64)
  private val headers = headersOf(HttpHeaders.ContentType, "application/json")
  private fun receipt(status: String = "queued") = """{"requestId":"$id","question":"架空の相談","status":"$status","phase":null,"createdAt":"2026-10-04T00:00:00Z","updatedAt":"2026-10-04T00:00:00Z","recordId":null,"errorCode":null}"""

  @Test fun nativePostHasOneShotJsonAndOnlySameOriginBearer() = runBlocking {
    var calls = 0
    DebateRequestsClient(MockEngine { request ->
      calls++
      assertEquals("https://shittim.pitekusu.dev/api/v1/debate-requests", request.url.toString())
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      val content = request.body as OutgoingContent.ReadChannelContent
      val body = content.readFrom().readBuffer().readByteArray().decodeToString()
      assertTrue(body.contains(id))
      assertTrue(body.contains("架空の相談"))
      respond(receipt(), HttpStatusCode.Accepted, headers)
    }).use { client -> assertEquals("queued", client.submit(token, id, "架空の相談").status) }
    assertEquals(1, calls)
  }

  @Test fun legacyPermissionAndQueueLimitsRemainDifferentFromMissingRequest() = runBlocking {
    val responses = listOf(
      HttpStatusCode.Forbidden to "DEBATE_START_REAUTH_REQUIRED",
      HttpStatusCode.TooManyRequests to "DEBATE_QUEUE_FULL",
      HttpStatusCode.NotFound to "DEBATE_REQUEST_NOT_FOUND")
    var calls = 0
    DebateRequestsClient(MockEngine {
      val (status, code) = responses[calls++]
      respond("""{"schemaVersion":1,"error":{"code":"$code","requestId":"synthetic"}}""", status, headers)
    }).use { client ->
      try { client.submit(token, id, "架空の相談"); fail("legacy token accepted") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.REAUTH_REQUIRED, error.failure) }
      try { client.submit(token, id, "架空の相談"); fail("queue ignored") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.QUEUE_FULL, error.failure) }
      assertNull(client.find(token, id))
    }
    assertEquals(3, calls)
  }

  @Test fun malformedOrMismatchedResponsesAreNotTrustedAndUnicodeLimitCountsCodepoints() = runBlocking {
    assertTrue(validDebateQuestion("😀".repeat(1000)))
    assertFalse(validDebateQuestion("😀".repeat(1001)))
    assertFalse(validDebateQuestion(" \n "))
    assertFalse(validDebateQuestion("\uD800"))
    var calls = 0
    DebateRequestsClient(MockEngine {
      calls++
      respond(receipt().replace(id, "99999999-2222-4333-8444-abcdefabcdef"), headers = headers)
    }).use { client ->
      try { client.submit(token, id, "架空の相談"); fail("mismatched receipt") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.INVALID_RESPONSE, error.failure) }
    }
    assertEquals(1, calls)
  }

  @Test fun onlyDefinitiveAuthenticationOrGuildDenialInvalidatesTheSession() = runBlocking<Unit> {
    for ((status, code, expected) in listOf(
      Triple(HttpStatusCode.Unauthorized, "AUTHENTICATION_REQUIRED", DebateFailure.AUTH_REQUIRED),
      Triple(HttpStatusCode.Forbidden, "GUILD_MEMBERSHIP_REQUIRED", DebateFailure.AUTH_REQUIRED),
      Triple(HttpStatusCode.Forbidden, "CHANNEL_PERMISSION_REQUIRED", DebateFailure.FORBIDDEN),
      Triple(HttpStatusCode.Forbidden, "DEBATE_START_REAUTH_REQUIRED", DebateFailure.REAUTH_REQUIRED),
    )) {
      DebateRequestsClient(MockEngine {
        respond("""{"schemaVersion":1,"error":{"code":"$code","requestId":"synthetic"}}""", status, headers)
      }).use { client ->
        try { client.submit(token, id, "架空の相談"); fail("denial accepted") }
        catch (error: DebateRequestException) { assertEquals(expected, error.failure) }
      }
    }
  }

  @Test fun controlPostAndOperationLookupUseTheBoundAttemptAndOneShotBody() = runBlocking {
    var calls = 0
    DebateRequestsClient(MockEngine { request ->
      calls++
      assertEquals("Bearer $token", request.headers[HttpHeaders.Authorization])
      assertFalse(request.headers.contains(HttpHeaders.Cookie))
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      if (calls == 1) {
        assertEquals("POST", request.method.value)
        assertEquals("/api/v1/debate-requests/$id/cancel", request.url.encodedPath)
        val body = (request.body as OutgoingContent.ReadChannelContent).readFrom()
          .readBuffer().readByteArray().decodeToString()
        assertEquals("""{"operationId":"$operation","attemptToken":"$attempt"}""", body)
      } else {
        assertEquals("GET", request.method.value)
        assertEquals("/api/v1/debate-requests/$id/operations/$operation", request.url.encodedPath)
      }
      respond("""{"operationId":"$operation","action":"cancel","status":"accepted","errorCode":null,"request":${receipt()}}""",
        HttpStatusCode.Accepted, headers)
    }).use { client ->
      assertEquals("accepted", client.control(token, id, "cancel", operation, attempt).status)
      assertEquals(operation, client.findControl(token, id, operation)?.operationId)
    }
    assertEquals(2, calls)
  }

  @Test fun controlTransportFailureDoesNotReplayAndMismatchedOperationIsRejected() = runBlocking<Unit> {
    var calls = 0
    DebateRequestsClient(MockEngine { calls++; throw IOException("synthetic_connection_failure") }).use { client ->
      try { client.control(token, id, "cancel", operation, attempt); fail("transport accepted") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.UNAVAILABLE, error.failure) }
    }
    assertEquals(1, calls)
    DebateRequestsClient(MockEngine {
      respond("""{"operationId":"$id","action":"retry","status":"completed","request":${receipt()}}""",
        headers = headers)
    }).use { client ->
      try { client.control(token, id, "cancel", operation, attempt); fail("mismatched operation accepted") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.INVALID_RESPONSE, error.failure) }
    }
  }

  @Test fun pendingOperationLookupDistinguishesMissingFromPermissionDenial() = runBlocking<Unit> {
    var calls = 0
    DebateRequestsClient(MockEngine {
      val missing = calls++ == 0
      respond("""{"error":{"code":"${if (missing) "DEBATE_OPERATION_NOT_FOUND" else "DEBATE_CONTROL_REAUTH_REQUIRED"}"}}""",
        if (missing) HttpStatusCode.NotFound else HttpStatusCode.Forbidden, headers)
    }).use { client ->
      assertNull(client.findControl(token, id, operation))
      try { client.control(token, id, "retry", operation, attempt); fail("permission denial accepted") }
      catch (error: DebateRequestException) { assertEquals(DebateFailure.REAUTH_REQUIRED, error.failure) }
    }
  }
}
