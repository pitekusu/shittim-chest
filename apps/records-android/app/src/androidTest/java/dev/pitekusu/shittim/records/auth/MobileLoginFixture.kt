package dev.pitekusu.shittim.records.auth

import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.toByteArray
import io.ktor.http.HttpHeaders
import io.ktor.http.headersOf
import java.io.IOException
import java.time.Instant
import kotlinx.coroutines.CompletableDeferred
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Synthetic protocol data only; no browser, production endpoint or existing token store. */
internal class MobileLoginFixture : AutoCloseable {
  val now = Instant.parse("2030-01-01T00:00:00Z")
  var expiry = now.plusSeconds(600)
  var elapsed = 0L
  var starts = 0
  var exchanges = 0
  var startBody = emptyMap<String, String>()
  var exchangeBody = emptyMap<String, String>()
  val transaction get() = starts.toString().padStart(43, 't')
  var exchangeGate: CompletableDeferred<Unit>? = null
  val exchanging = CompletableDeferred<Unit>()
  var failExchange = false
  val token = "a".repeat(43)
  val client = MobileAuthClient(MockEngine { request ->
    val body = Json.parseToJsonElement(request.body.toByteArray().decodeToString()).jsonObject
      .mapValues { it.value.jsonPrimitive.content }
    val response = when (request.url.encodedPath.substringAfterLast('/')) {
      "start" -> {
        starts++
        startBody = body
        """{"schemaVersion":1,"transactionId":"$transaction",
          "authorizePath":"/api/v1/auth/mobile/authorize?transaction=$transaction","expiresAt":"$expiry"}"""
      }
      "exchange" -> {
        exchanges++
        exchangeBody = body
        exchanging.complete(Unit)
        exchangeGate?.await()
        if (failExchange) throw IOException("synthetic private response")
        """{"schemaVersion":1,"accessToken":"$token","tokenType":"Bearer",
          "expiresAt":"2030-04-01T00:00:00Z","isAdmin":false,"returnTo":"${startBody.getValue("returnTo")}",
          "user":{"displayName":"テスト利用者","avatar":{"kind":"placeholder","alt":"テスト用","fallbackVariant":"cyan"}}}"""
      }
      else -> error("unexpected_test_request")
    }
    respond(response, headers = headersOf(HttpHeaders.ContentType, "application/json"))
  })

  fun callback(): String = "$MOBILE_CALLBACK_URL?transaction=$transaction&code=${"c".repeat(43)}&state=${startBody.getValue("state")}"
  override fun close() { client.close() }
}
