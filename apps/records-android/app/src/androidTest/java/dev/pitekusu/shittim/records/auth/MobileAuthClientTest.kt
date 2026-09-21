package dev.pitekusu.shittim.records.auth

import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.toByteArray
import io.ktor.client.plugins.HttpRequestTimeoutException
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.OutgoingContent
import io.ktor.http.headersOf
import java.io.IOException
import java.time.Instant
import kotlin.coroutines.cancellation.CancellationException
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileAuthClientTest {
  private val transaction = "t".repeat(43)
  private val token = "a".repeat(43)
  private val start = MobileStartRequest("c".repeat(43), "s".repeat(43))
  private val exchange = MobileExchangeRequest(transaction, "g".repeat(43), "v".repeat(43))
  private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json; charset=utf-8")
  private val startBody = """{"schemaVersion":1,"transactionId":"$transaction",
    "authorizePath":"/api/v1/auth/mobile/authorize?transaction=$transaction",
    "expiresAt":"2030-01-01T09:00:00+09:00"}"""
  private val sessionBody = """{"schemaVersion":1,"isAdmin":false,"expiresAt":"2030-01-01T00:00:00Z",
    "user":{"displayName":"テスト利用者","avatar":{"kind":"placeholder","alt":"テスト用","fallbackVariant":"cyan"}}}"""
  private val exchangeBody = sessionBody.dropLast(1) +
    """, "accessToken":"$token","tokenType":"Bearer","returnTo":"/"}"""

  @Test
  fun startExchangeSessionAndLogoutUseOnlyTheFixedOriginAndExplicitBearer() = runBlocking {
    val engine = MockEngine { request ->
      assertEquals("https", request.url.protocol.name)
      assertEquals("shittim.pitekusu.dev", request.url.host)
      assertEquals("", request.url.encodedQuery)
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      assertNull(request.headers[HttpHeaders.Cookie])
      assertNull(request.headers[HttpHeaders.Origin])
      val route = request.url.encodedPath.substringAfterLast('/')
      assertEquals(if (route == "session") HttpMethod.Get else HttpMethod.Post, request.method)
      assertEquals(if (route in setOf("session", "logout")) "Bearer $token" else null,
        request.headers[HttpHeaders.Authorization])
      if (route != "session") assertTrue(request.body is OutgoingContent.ReadChannelContent)
      when (route) {
        "start" -> {
          val body = Json.parseToJsonElement(request.body.toByteArray().decodeToString()).jsonObject
          assertEquals(setOf("codeChallenge", "codeChallengeMethod", "state", "returnTo"), body.keys)
          assertEquals(start.codeChallenge, body.getValue("codeChallenge").jsonPrimitive.content)
          assertEquals("S256", body.getValue("codeChallengeMethod").jsonPrimitive.content)
          assertEquals(start.state, body.getValue("state").jsonPrimitive.content)
          assertEquals("/", body.getValue("returnTo").jsonPrimitive.content)
          respond(startBody, headers = jsonHeaders)
        }
        "exchange" -> {
          val body = Json.parseToJsonElement(request.body.toByteArray().decodeToString()).jsonObject
          assertEquals(setOf("transactionId", "code", "codeVerifier"), body.keys)
          assertEquals(exchange.code, body.getValue("code").jsonPrimitive.content)
          assertEquals(exchange.codeVerifier, body.getValue("codeVerifier").jsonPrimitive.content)
          respond(exchangeBody, headers = jsonHeaders)
        }
        "session" -> respond(sessionBody, headers = headersOf(
          HttpHeaders.ContentType to listOf("application/json"),
          HttpHeaders.SetCookie to listOf("should_not_be_sent=private; Secure"),
        ))
        "logout" -> {
          assertTrue(request.body.toByteArray().isEmpty())
          respond("", HttpStatusCode.NoContent)
        }
        else -> error("unexpected_test_route")
      }
    }
    MobileAuthClient(engine).use { client ->
      assertTrue(engine.requestHistory.isEmpty()) // Construction never starts a login.
      val begun = client.start(start)
      assertEquals(transaction, begun.transactionId)
      assertEquals(Instant.parse("2030-01-01T00:00:00Z"), begun.expiresAt)
      val issued = client.exchange(exchange)
      assertEquals(token, issued.accessToken)
      assertEquals("/", issued.returnTo)
      assertFalse(issued.isAdmin)
      assertEquals("テスト利用者", issued.user.displayName)
      assertEquals(issued.expiresAt, StoredToken(issued.accessToken, issued.expiresAt).expiresAt)
      assertEquals("placeholder", client.session(token).user.avatar.kind)
      client.logout(token)
      failure(MobileAuthFailure.REQUEST_REJECTED) { client.session("private\r\ntoken") }
      for (value in listOf(start, exchange, begun, issued, issued.user)) {
        assertFalse(value.toString().contains(token))
        assertFalse(value.toString().contains("テスト利用者"))
      }
    }
    assertEquals(4, engine.requestHistory.size)
  }

  @Test
  fun httpErrorsAndRedirectsHaveSafeDistinctCategoriesWithoutRetry() = runBlocking {
    val cases = listOf(
      Triple(400, "MOBILE_GRANT_INVALID", MobileAuthFailure.GRANT_REJECTED),
      Triple(400, "REQUEST_INVALID", MobileAuthFailure.REQUEST_REJECTED),
      Triple(401, "AUTHENTICATION_REQUIRED", MobileAuthFailure.AUTHENTICATION_REQUIRED),
      Triple(403, "GUILD_MEMBERSHIP_REQUIRED", MobileAuthFailure.FORBIDDEN),
      Triple(429, "THROTTLED", MobileAuthFailure.THROTTLED),
      Triple(503, "RECORDS_UNAVAILABLE", MobileAuthFailure.UNAVAILABLE),
      Triple(302, "redirect", MobileAuthFailure.INVALID_RESPONSE),
    )
    for ((status, code, category) in cases) {
      val engine = MockEngine {
        respond("""{"error":{"code":"$code","message":"private $token","requestId":"private"}}""",
          HttpStatusCode.fromValue(status), headersOf(
            HttpHeaders.ContentType to listOf("application/json"),
            HttpHeaders.Location to listOf("https://untrusted.invalid/"),
            HttpHeaders.RetryAfter to listOf("0"),
          ))
      }
      MobileAuthClient(engine).use { client -> failure(category) { client.exchange(exchange) } }
      assertEquals(1, engine.requestHistory.size)
    }
  }

  @Test
  fun connectionFailureAndTimeoutDoNotBecomeAuthenticationFailure() = runBlocking {
    for (timeout in listOf(false, true)) {
      var attempts = 0
      val engine = MockEngine { request ->
        attempts++
        if (timeout) throw HttpRequestTimeoutException(request)
        throw IOException("private $token")
      }
      MobileAuthClient(engine).use { client -> failure(MobileAuthFailure.NETWORK) { client.exchange(exchange) } }
      assertEquals(1, attempts)
    }
  }

  @Test
  fun callerCancellationIsNotWrappedAsApiFailure() = runBlocking {
    MobileAuthClient(MockEngine { throw CancellationException("cancelled") }).use { client ->
      try {
        client.session(token)
        fail("expected cancellation")
      } catch (_: CancellationException) {
        // Preserve coroutine cancellation so C15/C16 can stop their own request scope.
      }
    }
  }

  @Test
  fun invalidSuccessPayloadsDoNotExposeSecretsOrAllowExternalNavigation() = runBlocking {
    val invalid = listOf(
      startBody.replace("\"schemaVersion\":1", "\"schemaVersion\":2"),
      """{"private":"$token"}""",
      "x".repeat(65_537),
    )
    for (body in invalid) {
      MobileAuthClient(MockEngine { respond(body, headers = jsonHeaders) }).use { client ->
        failure(MobileAuthFailure.INVALID_RESPONSE) { client.start(start) }
      }
    }
    MobileAuthClient(MockEngine { respond("<html>private $token</html>") }).use { client ->
      failure(MobileAuthFailure.INVALID_RESPONSE) { client.session(token) }
    }
    for (body in listOf(
      exchangeBody.replace("Bearer", "Basic"),
      exchangeBody.replace("\"returnTo\":\"/\"", "\"returnTo\":\"//untrusted.invalid\""),
    )) {
      MobileAuthClient(MockEngine { respond(body, headers = jsonHeaders) }).use { client ->
        failure(MobileAuthFailure.INVALID_RESPONSE) { client.exchange(exchange) }
      }
    }
  }

  private suspend fun failure(expected: MobileAuthFailure, operation: suspend () -> Unit) {
    try {
      operation()
      fail("expected safe API failure")
    } catch (error: MobileAuthException) {
      assertEquals(expected, error.failure)
      assertEquals("mobile_auth_${expected.name.lowercase()}", error.message)
      assertNull(error.cause)
      assertTrue(error.suppressed.isEmpty())
    }
  }
}
