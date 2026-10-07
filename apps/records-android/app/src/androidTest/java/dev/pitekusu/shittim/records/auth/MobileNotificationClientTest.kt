package dev.pitekusu.shittim.records.auth

import androidx.test.ext.junit.runners.AndroidJUnit4
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.toByteArray
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.OutgoingContent
import io.ktor.http.headersOf
import java.time.Instant
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class MobileNotificationClientTest {
  @Test fun registrationAndRemovalUseOnlyMobileBearerAndValidatedBody() = runBlocking {
    val bearer = "t".repeat(43)
    val binding = "b".repeat(43)
    val engine = MockEngine { request ->
      assertEquals("shittim.pitekusu.dev", request.url.host)
      assertEquals("/api/v1/auth/mobile/notifications/device", request.url.encodedPath)
      assertEquals("Bearer $bearer", request.headers[HttpHeaders.Authorization])
      assertNull(request.headers[HttpHeaders.Cookie])
      assertEquals("no-store", request.headers[HttpHeaders.CacheControl])
      assertTrue(request.body is OutgoingContent.ReadChannelContent)
      val body = Json.parseToJsonElement(request.body.toByteArray().decodeToString()).jsonObject
      assertEquals(setOf("token", "bindingId"), body.keys)
      assertEquals("fake-fcm-token", body.getValue("token").jsonPrimitive.content)
      assertEquals(binding, body.getValue("bindingId").jsonPrimitive.content)
      if (request.method == HttpMethod.Put) respond(
        """{"schemaVersion":1,"expiresAt":"2030-01-01T00:00:00Z"}""",
        headers = headersOf(HttpHeaders.ContentType, "application/json"))
      else {
        assertEquals(HttpMethod.Delete, request.method)
        respond("", HttpStatusCode.NoContent)
      }
    }
    MobileAuthClient(engine).use {
      assertEquals(Instant.parse("2030-01-01T00:00:00Z"),
        it.registerNotifications(bearer, "fake-fcm-token", binding))
      it.unregisterNotifications(bearer, "fake-fcm-token", binding)
      try {
        it.registerNotifications(bearer, "fake\ntoken", binding)
        throw AssertionError("expected rejected notification input")
      } catch (error: MobileAuthException) {
        assertEquals(MobileAuthFailure.REQUEST_REJECTED, error.failure)
        assertFalse(error.toString().contains(bearer))
      }
    }
    assertEquals(2, engine.requestHistory.size)
  }
}
